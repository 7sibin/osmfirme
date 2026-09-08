"""The website search: what counts as a business's own site, and what is noise."""

from __future__ import annotations

from dataclasses import replace

import pytest

from osm_businesses import Row
from webapp.enrich import (
    AGGREGATORS,
    SiteHit,
    WebsiteFinder,
    apply_site_hits,
    distinctive_tokens,
    domain_core,
    search_query,
)
from webapp.results import FOUND_NONE, FOUND_STRONG, FOUND_WEAK


def row(name, category="bakery", *, place="Nis", street="Nemanjina", website="", osm_id=1) -> Row:
    return Row(
        osm_type="node", osm_id=osm_id, name=name, category=category, place=place,
        street=street, housenumber="12", postcode="18000", phone="", phone_alt="",
        website=website, email="", facebook="", instagram="", opening_hours="",
        lat=43.32, lon=21.9, category_key="shop",
    )


# --- domain_core ------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://trpkovic.rs", "trpkovic"),
        ("https://www.trpkovic.rs/kontakt", "trpkovic"),
        ("http://TRPKOVIC.RS", "trpkovic"),
        ("https://pekara-trpkovic.co.rs", "pekara-trpkovic"),
        ("https://shop.trpkovic.com", "trpkovic"),
        ("https://trpkovic.org.rs/", "trpkovic"),
        ("not a url", ""),
        ("", ""),
    ],
)
def test_domain_core_strips_scheme_www_subdomain_and_suffix(url, expected):
    assert domain_core(url) == expected


# --- distinctive_tokens -----------------------------------------------------


def test_distinctive_tokens_drop_the_trade_word():
    assert distinctive_tokens("Pekara Trpkovic") == ["trpkovic"]
    assert distinctive_tokens("Apoteka Jankovic") == ["jankovic"]


def test_distinctive_tokens_drop_legal_forms():
    assert distinctive_tokens("Trpkovic DOO") == ["trpkovic"]
    assert distinctive_tokens("SZTR Petrovic d.o.o.") == ["petrovic"]


def test_distinctive_tokens_fold_diacritics_and_cyrillic():
    assert distinctive_tokens("Пекара Трпковић") == ["trpkovic"]
    assert distinctive_tokens("Pekara Trpković") == ["trpkovic"]


def test_a_name_that_is_only_a_trade_word_has_no_tokens():
    """`Pekara` alone identifies nothing, so it must never be searched for."""
    assert distinctive_tokens("Pekara") == []
    assert distinctive_tokens("Apoteka") == []


def test_distinctive_tokens_keep_multi_word_brands():
    assert distinctive_tokens("Zlatni Papagaj") == ["zlatni", "papagaj"]


# --- search_query -----------------------------------------------------------


def test_search_query_quotes_the_name_and_adds_the_place():
    assert search_query(row("Pekara Trpkovic")) == '"Pekara Trpkovic" Nis Nemanjina'


def test_search_query_survives_a_missing_street():
    assert search_query(row("Pekara Trpkovic", street="")) == '"Pekara Trpkovic" Nis'


# --- the finder -------------------------------------------------------------


def finder(results, *, alive=lambda url: url):
    """A finder wired to canned search results and no network."""
    return WebsiteFinder(search=lambda query: results, alive=alive, rate_limiter=None)


def result(href, title="", body=""):
    return {"href": href, "title": title, "body": body}


def test_a_domain_built_from_the_name_is_a_strong_hit():
    hit = finder([result("https://trpkovic.rs", "Pekara Trpkovic")]).find(row("Pekara Trpkovic"))
    assert hit.website == "https://trpkovic.rs"
    assert hit.confidence == FOUND_STRONG
    assert hit.source == "search"


def test_directories_and_catalogues_are_never_the_business_site():
    hits = [result(f"https://{host}/firme/pekara-trpkovic") for host in sorted(AGGREGATORS)[:5]]
    hit = finder(hits).find(row("Pekara Trpkovic"))
    assert hit.website == ""
    assert hit.confidence == FOUND_NONE


def test_a_site_mentioned_in_a_snippet_is_found_even_when_no_result_links_it():
    """The real case: the catalogues outrank the business, but quote its address."""
    results = [
        result("https://www.companywall.rs/firma/pekara-trpkovic",
               body="Kontakt telefon 0112415222 ili zvanicni sajt trpkovic.rs."),
    ]
    hit = finder(results).find(row("Pekara Trpkovic"))
    assert hit.website == "https://trpkovic.rs"
    assert hit.source == "mention"


def test_a_facebook_page_is_recorded_as_social_not_as_a_website():
    results = [result("https://www.facebook.com/pekaratrpkovic", "Pekara Trpkovic")]
    hit = finder(results).find(row("Pekara Trpkovic"))
    assert hit.website == ""
    assert hit.facebook == "https://www.facebook.com/pekaratrpkovic"
    assert hit.confidence == FOUND_WEAK
    assert hit.source == "social"


def test_an_unrelated_domain_is_rejected():
    results = [result("https://kupujemprodajem-nekretnine.rs", "Nesto sasvim deseto")]
    assert finder(results).find(row("Pekara Trpkovic")).confidence == FOUND_NONE


def test_a_partial_name_match_is_weak_not_strong():
    hit = finder([result("https://papagaj.rs")]).find(row("Zlatni Papagaj"))
    assert hit.confidence == FOUND_WEAK


def test_a_dead_domain_is_not_a_hit():
    results = [result("https://trpkovic.rs", "Pekara Trpkovic")]
    hit = finder(results, alive=lambda url: "").find(row("Pekara Trpkovic"))
    assert hit.confidence == FOUND_NONE


def test_the_alive_check_records_where_the_domain_redirected_to():
    results = [result("https://trpkovic.rs", "Pekara Trpkovic")]
    hit = finder(results, alive=lambda url: "https://www.trpkovic.rs/").find(row("Pekara Trpkovic"))
    assert hit.website == "https://www.trpkovic.rs/"


def test_a_name_with_no_distinctive_token_is_never_searched():
    def explode(query):
        raise AssertionError("must not search")

    hit = WebsiteFinder(search=explode, alive=lambda url: url, rate_limiter=None).find(row("Pekara"))
    assert hit.confidence == FOUND_NONE


def test_a_search_that_blows_up_leaves_the_row_unchecked_not_written_off():
    """A timeout says nothing about the business, so it must not cache as a miss."""

    def explode(query):
        raise RuntimeError("ddg is having a day")

    hit = WebsiteFinder(search=explode, alive=lambda url: url, rate_limiter=None).find(
        row("Pekara Trpkovic")
    )
    assert hit.confidence == ""
    assert hit.confidence != FOUND_NONE


# --- applying the overlay ---------------------------------------------------


def test_apply_site_hits_fills_the_found_fields_by_osm_id():
    rows = [row("Pekara Trpkovic", osm_id=1), row("Pekara Sunce", osm_id=2)]
    hits = {1: SiteHit(website="https://trpkovic.rs", confidence=FOUND_STRONG, source="search")}
    filled = apply_site_hits(rows, hits)
    assert filled[0].found_website == "https://trpkovic.rs"
    assert filled[0].found_confidence == FOUND_STRONG
    assert filled[1].found_confidence == ""


def test_apply_site_hits_leaves_the_osm_website_alone():
    rows = [row("Kafe Bar", website="https://osm-said-this.rs", osm_id=7)]
    hits = {7: SiteHit(website="https://search-said-this.rs", confidence=FOUND_STRONG)}
    assert apply_site_hits(rows, hits)[0].website == "https://osm-said-this.rs"


def test_apply_site_hits_fills_socials_only_when_osm_has_none():
    rows = [row("A", osm_id=1), replace(row("B", osm_id=2), facebook="https://fb.com/already")]
    hits = {
        1: SiteHit(confidence=FOUND_WEAK, facebook="https://fb.com/found"),
        2: SiteHit(confidence=FOUND_WEAK, facebook="https://fb.com/found"),
    }
    filled = apply_site_hits(rows, hits)
    assert filled[0].facebook == "https://fb.com/found"
    assert filled[1].facebook == "https://fb.com/already"


def test_apply_site_hits_with_nothing_checked_is_a_no_op():
    rows = [row("A", osm_id=1)]
    assert apply_site_hits(rows, {}) == rows


def test_a_directory_titled_with_the_business_name_is_still_not_its_site():
    """Every catalogue entry is titled with the business's name. The domain decides."""
    results = [result("https://bestofserbia.rs/kompanije/trgovina/pekara-trpkovic",
                      "Pekara Trpkovic - Best of Serbia")]
    assert finder(results).find(row("Pekara Trpkovic")).confidence == "none"


def test_an_unlisted_directory_is_rejected_by_the_domain_rule_not_the_blocklist():
    """The blocklist is an early-out, not the safety net: an unknown one fails too."""
    results = [result("https://neki-novi-katalog-firmi.rs/firme/pekara-trpkovic",
                      "Pekara Trpkovic | Katalog")]
    assert finder(results).find(row("Pekara Trpkovic")).confidence == "none"


def test_a_perfect_name_on_a_foreign_tld_is_only_weak():
    """`Restoran Zlatnik` in Nis matching restoranzlatnik.ba is a Bosnian namesake."""
    results = [result("https://restoranzlatnik.ba", "Restoran Zlatnik")]
    hit = finder(results).find(row("Restoran Zlatnik", place="Nis"))
    assert hit.website == "https://restoranzlatnik.ba"
    assert hit.confidence == FOUND_WEAK


def test_the_same_name_on_a_serbian_tld_stays_strong():
    results = [result("https://restoranzlatnik.rs", "Restoran Zlatnik")]
    assert finder(results).find(row("Restoran Zlatnik", place="Nis")).confidence == FOUND_STRONG


def test_an_instagram_browse_page_is_not_a_business_profile():
    results = [result("https://www.instagram.com/popular/p/12345/", "Pekara Trpkovic")]
    assert finder(results).find(row("Pekara Trpkovic")).instagram == ""


def test_an_instagram_handle_built_from_the_name_is_a_profile():
    results = [result("https://www.instagram.com/pekara_trpkovic/", "Pekara Trpkovic")]
    assert finder(results).find(row("Pekara Trpkovic")).instagram.endswith("pekara_trpkovic/")


def test_needs_check_skips_what_cannot_or_need_not_be_searched():
    from webapp.enrich import needs_check

    assert needs_check(row("Pekara Trpkovic"))
    assert not needs_check(row("Pekara Trpkovic", website="https://already.rs"))
    assert not needs_check(row("Pekara"))
    assert not needs_check(replace(row("Pekara Trpkovic"), found_confidence="none"))
