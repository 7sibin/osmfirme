"""Reading a business's own site for the contact details OSM never carries."""

from __future__ import annotations

import pytest

from osm_businesses import Row
from webapp.contacts import (
    CONTACT_DEAD,
    CONTACT_NONE,
    CONTACT_OK,
    ContactHit,
    ContactScraper,
    SiteUnreachable,
    apply_contact_hits,
    contact_page_links,
    emails_in,
    needs_contacts,
    phones_in,
    pick_email,
)


def row(name="Pekara Trpkovic", *, website="", found_website="", email="", phone="",
        facebook="", osm_id=1, found_confidence="", contact_status="") -> Row:
    return Row(
        osm_type="node", osm_id=osm_id, name=name, category="bakery", place="Nis",
        street="Nemanjina", housenumber="1", postcode="18000", phone=phone, phone_alt="",
        website=website, email=email, facebook=facebook, instagram="", opening_hours="",
        lat=43.32, lon=21.9, category_key="shop",
        found_website=found_website, found_confidence=found_confidence,
        contact_status=contact_status,
    )


# --- finding addresses in a page --------------------------------------------


def test_a_plain_address_and_a_mailto_are_both_found():
    html = '<p>Pisite nam na info@trpkovic.rs</p><a href="mailto:prodaja@trpkovic.rs">Prodaja</a>'
    assert set(emails_in(html)) == {"info@trpkovic.rs", "prodaja@trpkovic.rs"}


def test_addresses_are_lowercased_and_deduplicated():
    assert emails_in("INFO@Trpkovic.rs and info@trpkovic.RS") == ["info@trpkovic.rs"]


@pytest.mark.parametrize(
    "html",
    [
        '<img src="logo@2x.png">',                    # a retina asset, not an address
        '<img srcset="hero@3x.webp 3x">',
        "<p>sentry@o1234.ingest.sentry.io</p>",       # the site's error reporter
        "<p>you@example.com</p>",                     # placeholder left in a form
        "<p>name@yourdomain.com</p>",
        "<p>hello@wixpress.com</p>",                  # the platform, not the business
    ],
)
def test_what_looks_like_an_address_but_is_not(html):
    assert emails_in(html) == []


def test_script_and_style_bodies_are_not_searched():
    """Analytics blobs are full of things shaped like addresses and phone numbers."""
    html = '<script>var t="track@googletagmanager.com";</script><p>info@trpkovic.rs</p>'
    assert emails_in(html) == ["info@trpkovic.rs"]


# --- choosing between them --------------------------------------------------


def test_an_address_on_the_business_own_domain_wins():
    found = ["office@nekaagencija.rs", "info@trpkovic.rs", "pekaratrpkovic@gmail.com"]
    assert pick_email(found, "https://trpkovic.rs") == "info@trpkovic.rs"


def test_a_free_provider_beats_an_unrelated_domain():
    """Half the small businesses in Serbia publish a gmail address and nothing else."""
    found = ["support@nekicms.com", "pekaratrpkovic@gmail.com"]
    assert pick_email(found, "https://trpkovic.rs") == "pekaratrpkovic@gmail.com"


def test_a_role_address_is_preferred_over_a_personal_one_on_the_same_domain():
    found = ["marko.markovic@trpkovic.rs", "info@trpkovic.rs"]
    assert pick_email(found, "https://trpkovic.rs") == "info@trpkovic.rs"


def test_nothing_usable_is_no_address_rather_than_a_bad_guess():
    assert pick_email(["noreply@mailchimp.com", "abuse@cloudflare.com"], "https://trpkovic.rs") == ""


def test_picking_from_an_empty_page_is_empty():
    assert pick_email([], "https://trpkovic.rs") == ""


# --- phone numbers ----------------------------------------------------------


def test_serbian_numbers_in_their_usual_shapes():
    numbers = phones_in("Tel: 018/512-345, mob. 064 123 4567, ili +381 18 512 345")
    assert "018512345" in numbers
    assert "0641234567" in numbers
    assert "+38118512345" in numbers


def test_a_tel_link_counts_too():
    assert "+381641234567" in phones_in('<a href="tel:+381641234567">Pozovi</a>')


@pytest.mark.parametrize(
    "html",
    [
        "<p>Radno vreme 08:00 - 20:00</p>",
        "<p>PIB 123456789</p>",           # nine digits, but no dialling shape
        "<p>Cena 1.250,00 RSD</p>",
        "<p>2024-01-15</p>",
    ],
)
def test_numbers_that_are_not_phone_numbers(html):
    assert phones_in(html) == []


# --- the contact page -------------------------------------------------------


def test_a_kontakt_link_is_recognised_by_href_or_by_text():
    html = ('<a href="/kontakt">Pisite nam</a>'
            '<a href="/informacije">Kontakt</a>'
            '<a href="/proizvodi">Proizvodi</a>')
    assert contact_page_links(html, "https://trpkovic.rs") == [
        "https://trpkovic.rs/kontakt",
        "https://trpkovic.rs/informacije",
    ]


def test_contact_links_off_the_site_own_host_are_ignored():
    """Following those is how a scraper wanders off into a directory."""
    html = '<a href="https://companywall.rs/firma/trpkovic">Kontakt</a><a href="/kontakt">x</a>'
    assert contact_page_links(html, "https://trpkovic.rs") == ["https://trpkovic.rs/kontakt"]


# --- the scraper ------------------------------------------------------------


def scraper(pages, **kwargs):
    """A scraper wired to canned pages and no network.

    An address not in `pages` is one nothing answers at, which is what the real
    fetcher signals with SiteUnreachable - as opposed to answering with nothing
    usable, which the tests below model by handing back an empty string.
    """
    def fetch(url):
        if url not in pages:
            raise SiteUnreachable(url)
        return pages[url]

    return ContactScraper(fetch=fetch, **kwargs)


def test_the_homepage_alone_is_enough_when_it_carries_an_address():
    pages = {"https://trpkovic.rs": '<p>info@trpkovic.rs</p><a href="/kontakt">Kontakt</a>'}
    hit = scraper(pages).read(row(website="https://trpkovic.rs"))
    assert hit.email == "info@trpkovic.rs"
    assert hit.status == CONTACT_OK


def test_the_contact_page_is_followed_when_the_homepage_has_nothing():
    pages = {
        "https://trpkovic.rs": '<a href="/kontakt">Kontakt</a>',
        "https://trpkovic.rs/kontakt": "<p>info@trpkovic.rs</p><p>Tel: 018/512-345</p>",
    }
    hit = scraper(pages).read(row(website="https://trpkovic.rs"))
    assert hit.email == "info@trpkovic.rs"
    assert hit.phone == "018512345"


def test_a_site_that_answers_nothing_is_dead_not_merely_empty():
    hit = scraper({}).read(row(website="https://trpkovic.rs"))
    assert hit.status == CONTACT_DEAD
    assert hit.email == ""


def test_a_site_that_answers_and_refuses_is_not_reported_as_dead():
    """A 403 from a bot filter is a live business behind a wall, not a dead one.

    Getting this wrong is expensive in one direction only: telling the user a
    working business has no website loses a lead and looks like a bug.
    """
    hit = ContactScraper(fetch=lambda url: "").read(row(website="https://srbijanka.shop"))
    assert hit.status == CONTACT_NONE


def test_a_kontakt_link_that_is_gone_says_nothing_about_the_site():
    pages = {"https://trpkovic.rs": '<p>info@trpkovic.rs</p><a href="/kontakt">Kontakt</a>'}
    hit = scraper(pages).read(row(website="https://trpkovic.rs"))
    assert hit.status == CONTACT_OK


def test_a_live_site_with_no_address_anywhere_is_a_clean_miss():
    pages = {"https://trpkovic.rs": "<p>Dobrodosli</p>"}
    assert scraper(pages).read(row(website="https://trpkovic.rs")).status == CONTACT_NONE


def test_socials_are_picked_up_off_the_page():
    pages = {"https://trpkovic.rs": (
        '<a href="https://www.facebook.com/pekaratrpkovic">fb</a>'
        '<a href="https://instagram.com/pekara_trpkovic">ig</a><p>info@trpkovic.rs</p>')}
    hit = scraper(pages).read(row(website="https://trpkovic.rs"))
    assert hit.facebook == "https://www.facebook.com/pekaratrpkovic"
    assert hit.instagram == "https://instagram.com/pekara_trpkovic"


def test_a_site_the_search_found_is_read_when_osm_has_none():
    pages = {"https://trpkovic.rs": "<p>info@trpkovic.rs</p>"}
    business = row(found_website="https://trpkovic.rs", found_confidence="strong")
    assert scraper(pages).read(business).email == "info@trpkovic.rs"


def test_a_weak_guess_at_a_site_is_not_read():
    """Reading a page that probably is not theirs would attach a stranger's address."""
    pages = {"https://maybe.rs": "<p>info@maybe.rs</p>"}
    business = row(found_website="https://maybe.rs", found_confidence="weak")
    hit = scraper(pages).read(business)
    assert hit.email == ""
    assert hit.status == CONTACT_NONE


def test_only_the_capped_number_of_pages_is_fetched():
    homepage = "".join(f'<a href="/kontakt{n}">Kontakt</a>' for n in range(10))
    seen: list[str] = []

    def fetch(url):
        seen.append(url)
        return homepage if url == "https://trpkovic.rs" else "<p>prazno</p>"

    ContactScraper(fetch=fetch, max_pages=3).read(row(website="https://trpkovic.rs"))
    assert len(seen) == 3, "the homepage plus two contact pages, not ten"


def test_an_unreachable_homepage_is_dead_however_it_failed():
    def fetch(url):
        raise SiteUnreachable("dns")

    assert ContactScraper(fetch=fetch).read(row(website="https://x.rs")).status == CONTACT_DEAD


def test_a_fetch_that_blows_up_is_a_dead_site_not_a_crash():
    def fetch(url):
        raise RuntimeError("connection reset")

    assert ContactScraper(fetch=fetch).read(row(website="https://trpkovic.rs")).status == CONTACT_DEAD


def test_reading_stops_as_soon_as_everything_wanted_is_in_hand():
    pages = {"https://trpkovic.rs": '<p>info@trpkovic.rs Tel 018/512-345</p><a href="/kontakt">k</a>'}
    seen: list[str] = []

    def fetch(url):
        seen.append(url)
        return pages.get(url, "")

    ContactScraper(fetch=fetch).read(row(website="https://trpkovic.rs"))
    assert seen == ["https://trpkovic.rs"], "no reason to open the contact page"


# --- who is worth reading ---------------------------------------------------


def test_needs_contacts_wants_a_site_and_something_still_missing():
    assert needs_contacts(row(website="https://a.rs"))
    assert needs_contacts(row(found_website="https://a.rs", found_confidence="strong"))


def test_needs_contacts_skips_the_rest():
    assert not needs_contacts(row()), "no site to read"
    assert not needs_contacts(
        row(website="https://a.rs", email="a@a.rs", phone="018111")
    ), "nothing left to learn"
    assert not needs_contacts(
        row(website="https://a.rs", contact_status=CONTACT_NONE)
    ), "already read"


# --- applying the overlay ---------------------------------------------------


def test_apply_contact_hits_fills_the_found_fields():
    rows = [row(osm_id=1), row(osm_id=2)]
    hits = {1: ContactHit(email="info@trpkovic.rs", phone="018512345", status=CONTACT_OK)}
    filled = apply_contact_hits(rows, hits)
    assert filled[0].found_email == "info@trpkovic.rs"
    assert filled[0].found_phone == "018512345"
    assert filled[0].contact_status == CONTACT_OK
    assert filled[1].contact_status == ""


def test_what_osm_already_carries_is_never_overwritten():
    rows = [row(osm_id=1, email="osm@trpkovic.rs", phone="018999", facebook="https://fb.com/osm")]
    hits = {1: ContactHit(email="web@trpkovic.rs", phone="018111",
                          facebook="https://fb.com/web", status=CONTACT_OK)}
    filled = apply_contact_hits(rows, hits)
    assert filled[0].email == "osm@trpkovic.rs"
    assert filled[0].phone == "018999"
    assert filled[0].facebook == "https://fb.com/osm"
    # what was read off the site is still reported, in its own field
    assert filled[0].found_email == "web@trpkovic.rs"


def test_apply_contact_hits_with_nothing_read_is_a_no_op():
    rows = [row(osm_id=1)]
    assert apply_contact_hits(rows, {}) == rows


def test_a_percent_encoded_mailto_is_decoded():
    """Real case: `mailto:%20webshop@diopta.rs` was landing in the sheet verbatim."""
    assert emails_in('<a href="mailto:%20webshop@diopta.rs">Pisite</a>') == ["webshop@diopta.rs"]


def test_a_mailto_subject_is_not_part_of_the_address():
    html = '<a href="mailto:office@trpkovic.rs?subject=Upit%20o%20ceni">Upit</a>'
    assert emails_in(html) == ["office@trpkovic.rs"]


def test_several_addresses_in_one_mailto_are_all_read():
    html = '<a href="mailto:info@trpkovic.rs,prodaja@trpkovic.rs">Pisite</a>'
    assert emails_in(html) == ["info@trpkovic.rs", "prodaja@trpkovic.rs"]


def test_punctuation_around_an_address_is_trimmed():
    assert emails_in("<p>Pisite na (info@trpkovic.rs).</p>") == ["info@trpkovic.rs"]
