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
