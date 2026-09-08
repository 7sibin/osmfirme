from __future__ import annotations

from osm_businesses import Row
from webapp.results import ResultFilters, facets, filter_rows, paginate, sort_filtered


def row(name, category, *, phone="", website="", street="", category_key="shop") -> Row:
    return Row(
        osm_type="node", osm_id=abs(hash(name)) % 10_000, name=name, category=category,
        place="Nis", street=street, housenumber="", postcode="", phone=phone, phone_alt="",
        website=website, email="", facebook="", instagram="", opening_hours="",
        lat=43.32, lon=21.9, category_key=category_key,
    )


ROWS = [
    row("Pekara Sunce", "bakery", phone="+38118111"),
    row("Pekara Zvezda", "bakery"),
    row("Kafe Bar", "cafe", website="https://x.rs", category_key="amenity"),
    row("Zubar Nikolic", "dentist", phone="+38118222", category_key="healthcare"),
]


def test_no_filters_keeps_everything():
    assert filter_rows(ROWS, ResultFilters()) == ROWS


def test_category_filter_matches_the_fine_value():
    kept = filter_rows(ROWS, ResultFilters(categories=["bakery"]))
    assert [r.name for r in kept] == ["Pekara Sunce", "Pekara Zvezda"]


def test_multiple_categories_are_a_union():
    kept = filter_rows(ROWS, ResultFilters(categories=["bakery", "cafe"]))
    assert len(kept) == 3


def test_require_contact_drops_rows_without_phone_or_website():
    kept = filter_rows(ROWS, ResultFilters(require_contact=True))
    assert [r.name for r in kept] == ["Pekara Sunce", "Kafe Bar", "Zubar Nikolic"]


def test_text_search_is_case_insensitive_and_covers_name_and_street():
    assert len(filter_rows(ROWS, ResultFilters(q="pekara"))) == 2
    rows = [*ROWS, row("Apoteka", "pharmacy", street="Obrenoviceva")]
    assert len(filter_rows(rows, ResultFilters(q="obrenovi"))) == 1


def test_facets_count_every_category_present():
    result = facets(ROWS, ResultFilters())
    assert {item["value"]: item["count"] for item in result} == {"bakery": 2, "cafe": 1, "dentist": 1}


def test_facets_ignore_the_category_filter_but_honour_the_others():
    result = facets(ROWS, ResultFilters(categories=["bakery"], require_contact=True))
    assert {item["value"]: item["count"] for item in result} == {"bakery": 1, "cafe": 1, "dentist": 1}


def test_facets_sort_by_count_then_value():
    assert [item["value"] for item in facets(ROWS, ResultFilters())] == ["bakery", "cafe", "dentist"]


def test_sorting_by_name_ascending_and_descending():
    ascending = sort_filtered(ROWS, "name", "asc")
    assert ascending[0].name == "Kafe Bar"
    assert sort_filtered(ROWS, "name", "desc")[0].name == "Zubar Nikolic"


def test_sorting_by_an_unknown_column_falls_back_to_name():
    assert sort_filtered(ROWS, "nonsense", "asc")[0].name == "Kafe Bar"


def test_pagination_returns_the_slice_and_the_total():
    page, total = paginate(ROWS, page=2, page_size=2)
    assert total == 4
    assert [r.name for r in page] == ["Kafe Bar", "Zubar Nikolic"]


def test_pagination_past_the_end_is_empty_not_an_error():
    page, total = paginate(ROWS, page=99, page_size=2)
    assert page == []
    assert total == 4


def test_website_filter_yes_keeps_only_rows_with_a_site():
    kept = filter_rows(ROWS, ResultFilters(website="yes"))
    assert [r.name for r in kept] == ["Kafe Bar"]


def test_website_filter_no_keeps_only_rows_without_a_site():
    kept = filter_rows(ROWS, ResultFilters(website="no"))
    assert [r.name for r in kept] == ["Pekara Sunce", "Pekara Zvezda", "Zubar Nikolic"]


def test_website_filter_any_keeps_everything():
    assert filter_rows(ROWS, ResultFilters(website="any")) == ROWS
    assert filter_rows(ROWS, ResultFilters()) == ROWS


def test_unknown_website_value_is_treated_as_any():
    assert filter_rows(ROWS, ResultFilters(website="nonsense")) == ROWS


def test_website_filter_combines_with_the_others():
    kept = filter_rows(ROWS, ResultFilters(website="no", require_contact=True))
    assert [r.name for r in kept] == ["Pekara Sunce", "Zubar Nikolic"]


def test_facets_honour_the_website_filter():
    result = facets(ROWS, ResultFilters(website="yes"))
    assert {item["value"]: item["count"] for item in result} == {"cafe": 1}


# --- preparation: blocklist, chain collapsing, hiding what the search found ---

from dataclasses import replace  # noqa: E402

from webapp.results import (  # noqa: E402
    NON_COMMERCIAL,
    collapse_chains,
    drop_non_commercial,
    prepare_rows,
)


def public(name, category, category_key="amenity"):
    return row(name, category, category_key=category_key)


def test_blocklist_drops_banks_and_post_offices():
    rows = [*ROWS, public("Posta 18000", "post_office"), public("Banka Intesa", "bank")]
    assert drop_non_commercial(rows) == ROWS


def test_blocklist_drops_public_institutions():
    rows = [
        public("OS Vuk Karadzic", "school"),
        public("Vrtic Pcelica", "kindergarten"),
        public("Opstina Nis", "townhall"),
        public("Crkva Svetog Save", "place_of_worship"),
        public("Klinicki centar", "hospital"),
    ]
    assert drop_non_commercial(rows) == []


def test_blocklist_spares_private_businesses_in_neighbouring_categories():
    """A dentist, a private pharmacy and a driving school are exactly the leads wanted."""
    rows = [
        public("Zubar Nikolic", "dentist"),
        public("Apoteka Jankovic", "pharmacy"),
        public("Auto skola Volan", "driving_school"),
        row("Advokat Peric", "lawyer", category_key="office"),
    ]
    assert drop_non_commercial(rows) == rows


def test_blocklist_matches_on_the_key_too_not_just_the_value():
    """`office=insurance` goes; a shop that happened to be called `insurance` would not."""
    assert "office=insurance" in NON_COMMERCIAL
    assert "shop=insurance" not in NON_COMMERCIAL


def test_chain_collapse_keeps_one_row_per_name_and_category():
    rows = [
        row("Maxi", "supermarket", street="Bulevar"),
        row("Maxi", "supermarket", street="Nemanjina"),
        row("Maxi", "supermarket", street="Vozdova"),
        row("Lidl", "supermarket", street="Kralja Petra"),
    ]
    kept = collapse_chains(rows)
    assert [r.name for r in kept] == ["Maxi", "Lidl"]


def test_chain_collapse_keeps_the_richest_copy():
    thin = row("Maxi", "supermarket")
    rich = row("Maxi", "supermarket", street="Bulevar", phone="+38118333")
    assert collapse_chains([thin, rich]) == [rich]
    assert collapse_chains([rich, thin]) == [rich]


def test_chain_collapse_ignores_case_and_serbian_diacritics():
    rows = [
        row("Pekara Trpkovic", "bakery", street="A"),
        row("PEKARA TRPKOVIĆ", "bakery", street="B"),
        row("Пекара Трпковић", "bakery", street="C"),
    ]
    assert len(collapse_chains(rows)) == 1


def test_chain_collapse_keeps_same_name_different_trade_apart():
    """`Apoteka` the pharmacy and `Apoteka` the cafe are two businesses, not one."""
    rows = [row("Apoteka", "pharmacy"), row("Apoteka", "cafe", category_key="amenity")]
    assert len(collapse_chains(rows)) == 2


def test_chain_collapse_preserves_first_appearance_order():
    rows = [row("B", "cafe"), row("A", "bakery"), row("B", "cafe", street="X")]
    assert [r.name for r in collapse_chains(rows)] == ["B", "A"]


def test_prepare_rows_applies_both_by_default():
    rows = [
        row("Maxi", "supermarket", street="A"),
        row("Maxi", "supermarket", street="B"),
        public("Posta", "post_office"),
    ]
    assert [r.name for r in prepare_rows(rows, ResultFilters())] == ["Maxi"]


def test_prepare_rows_leaves_everything_alone_when_both_are_off():
    rows = [
        row("Maxi", "supermarket", street="A"),
        row("Maxi", "supermarket", street="B"),
        public("Posta", "post_office"),
    ]
    filters = ResultFilters(commercial_only=False, collapse=False)
    assert prepare_rows(rows, filters) == rows


def test_hide_found_drops_rows_the_search_turned_up_a_site_for():
    found = replace(row("Pekara Sunce", "bakery"), found_website="https://sunce.rs",
                    found_confidence="strong")
    weak = replace(row("Pekara Zvezda", "bakery"), found_website="https://zvezda.rs",
                   found_confidence="weak")
    plain = row("Pekara Mesec", "bakery")
    kept = prepare_rows([found, weak, plain], ResultFilters(hide_found=True))
    assert [r.name for r in kept] == ["Pekara Zvezda", "Pekara Mesec"]


def test_hide_found_is_off_by_default():
    found = replace(row("Pekara Sunce", "bakery"), found_website="https://sunce.rs",
                    found_confidence="strong")
    assert prepare_rows([found], ResultFilters()) == [found]


def test_facets_count_the_prepared_set_not_the_raw_one():
    """Three Maxis collapsed to one must read as one supermarket, not three."""
    rows = [
        row("Maxi", "supermarket", street="A"),
        row("Maxi", "supermarket", street="B"),
        row("Lidl", "supermarket", street="C"),
    ]
    prepared = prepare_rows(rows, ResultFilters())
    counts = {item["value"]: item["count"] for item in facets(prepared, ResultFilters())}
    assert counts == {"supermarket": 2}


# --- the businesses whose website has gone ----------------------------------

from webapp.results import CONTACT_DEAD, CONTACT_NONE, CONTACT_OK, WEBSITE_DEAD  # noqa: E402


def sited(name, *, status="", website="https://x.rs"):
    return replace(row(name, "bakery", website=website), contact_status=status)


DEAD_ROWS = [
    sited("Nekad Imali", status=CONTACT_DEAD),
    sited("Sajt Radi", status=CONTACT_OK),
    sited("Blokiran", status=CONTACT_NONE),
    sited("Nije Citan", status=""),
    replace(row("Nema Sajt", "bakery"), contact_status=""),
]


def test_the_dead_filter_keeps_only_businesses_whose_site_stopped_answering():
    kept = filter_rows(DEAD_ROWS, ResultFilters(website=WEBSITE_DEAD))
    assert [r.name for r in kept] == ["Nekad Imali"]


def test_a_site_that_merely_refused_to_be_read_is_not_dead():
    """A 403 from a bot filter is a working business. Listing it as dead is a lie."""
    kept = filter_rows(DEAD_ROWS, ResultFilters(website=WEBSITE_DEAD))
    assert "Blokiran" not in [r.name for r in kept]


def test_an_unread_site_is_not_dead_either():
    assert "Nije Citan" not in [
        r.name for r in filter_rows(DEAD_ROWS, ResultFilters(website=WEBSITE_DEAD))
    ]


def test_the_dead_filter_combines_with_the_others():
    """A dead site is not a way to reach anyone, so only the one with a phone survives."""
    rows = [*DEAD_ROWS, replace(sited("Nekad Imali Drugi", status=CONTACT_DEAD), phone="+38118111")]
    kept = filter_rows(rows, ResultFilters(website=WEBSITE_DEAD, require_contact=True))
    assert [r.name for r in kept] == ["Nekad Imali Drugi"]


def test_a_dead_website_is_not_a_way_to_reach_anyone():
    gone = sited("Nekad Imali", status=CONTACT_DEAD)
    assert not gone.has_contact
    assert sited("Sajt Radi", status=CONTACT_OK).has_contact
    assert replace(gone, phone="+38118111").has_contact
    assert replace(gone, found_phone="018512345").has_contact


def test_an_unread_website_still_counts_as_contact():
    """Nothing changes until a site has actually been read and failed."""
    assert sited("Nije Citan", status="").has_contact


def test_facets_honour_the_dead_filter():
    rows = [*DEAD_ROWS, replace(sited("Kafic Nestao", status=CONTACT_DEAD), category="cafe")]
    counts = {item["value"]: item["count"] for item in facets(rows, ResultFilters(website=WEBSITE_DEAD))}
    assert counts == {"bakery": 1, "cafe": 1}


def test_the_other_website_values_ignore_contact_status():
    """`no` still means "OSM has no website", not "the website does not work"."""
    kept = filter_rows(DEAD_ROWS, ResultFilters(website="no"))
    assert [r.name for r in kept] == ["Nema Sajt"]
