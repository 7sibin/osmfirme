"""Tests for the pure parsing layer. No network, hand-built fixtures only."""

from __future__ import annotations

from typing import Any

import pytest

from osm_businesses import Row, parse_elements


def node(osm_id: int = 1, lat: float = 43.32, lon: float = 21.90, **tags: str) -> dict[str, Any]:
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": dict(tags)}


def way(osm_id: int = 2, lat: float = 43.32, lon: float = 21.90, **tags: str) -> dict[str, Any]:
    return {
        "type": "way",
        "id": osm_id,
        "center": {"lat": lat, "lon": lon},
        "tags": dict(tags),
    }


def only(rows: list[Row]) -> Row:
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}: {rows}"
    return rows[0]


# --- coordinate extraction --------------------------------------------------


def test_node_coordinates_come_from_top_level() -> None:
    row = only(parse_elements([node(lat=43.3209, lon=21.8958, name="Kafana", amenity="cafe")]))
    assert (row.lat, row.lon) == (43.3209, 21.8958)
    assert row.osm_type == "node"
    assert row.osm_id == 1


def test_way_coordinates_come_from_center() -> None:
    row = only(parse_elements([way(lat=43.4, lon=21.5, name="Pekara", shop="bakery")]))
    assert (row.lat, row.lon) == (43.4, 21.5)
    assert row.osm_type == "way"


def test_relation_coordinates_come_from_center() -> None:
    element = {
        "type": "relation",
        "id": 77,
        "center": {"lat": 44.0, "lon": 20.0},
        "tags": {"name": "Trzni centar", "shop": "mall"},
    }
    row = only(parse_elements([element]))
    assert (row.osm_type, row.osm_id) == ("relation", 77)
    assert (row.lat, row.lon) == (44.0, 20.0)


def test_element_without_usable_coordinate_is_dropped() -> None:
    element = {"type": "way", "id": 3, "tags": {"name": "Nowhere", "shop": "kiosk"}}
    assert parse_elements([element]) == []


# --- naming -----------------------------------------------------------------


def test_unnamed_element_is_dropped() -> None:
    assert parse_elements([node(shop="bakery")]) == []


def test_operator_used_when_name_missing() -> None:
    row = only(parse_elements([node(operator="Posta Srbije", amenity="post_office")]))
    assert row.name == "Posta Srbije"


def test_brand_used_when_name_and_operator_missing() -> None:
    row = only(parse_elements([node(brand="Maxi", shop="supermarket")]))
    assert row.name == "Maxi"


def test_name_wins_over_operator_and_brand() -> None:
    element = node(name="Maxi Nis", operator="Delhaize", brand="Maxi", shop="supermarket")
    assert only(parse_elements([element])).name == "Maxi Nis"


# --- category ---------------------------------------------------------------


def test_category_prefers_shop_over_amenity() -> None:
    row = only(parse_elements([node(name="Pekara Branko", shop="bakery", amenity="cafe")]))
    assert row.category == "bakery"


def test_category_falls_back_to_amenity() -> None:
    row = only(parse_elements([node(name="Apoteka", amenity="pharmacy")]))
    assert row.category == "pharmacy"


# --- street furniture exclusion ---------------------------------------------


def test_bench_is_dropped() -> None:
    assert parse_elements([node(name="Klupa", amenity="bench")]) == []


@pytest.mark.parametrize(
    "value",
    ["waste_basket", "atm", "parking", "bicycle_parking", "toilets", "vending_machine", "clock"],
)
def test_street_furniture_values_are_dropped(value: str) -> None:
    assert parse_elements([node(name="Thing", amenity=value)]) == []


def test_shop_with_furniture_amenity_survives() -> None:
    row = only(parse_elements([node(name="Kiosk", shop="kiosk", amenity="vending_machine")]))
    assert row.category == "kiosk"


def test_office_with_furniture_amenity_survives() -> None:
    row = only(parse_elements([node(name="Banka", office="financial", amenity="atm")]))
    assert row.category == "financial"


def test_craft_with_furniture_amenity_survives() -> None:
    row = only(parse_elements([node(name="Radionica", craft="carpenter", amenity="parking")]))
    assert row.category == "carpenter"


def test_furniture_amenity_dropped_when_only_tourism_present() -> None:
    # tourism is not one of shop/office/craft, so the exclusion still applies
    assert parse_elements([node(name="Parking", amenity="parking", tourism="information")]) == []


# --- dedupe -----------------------------------------------------------------


def test_node_and_way_duplicate_keeps_richer_row() -> None:
    thin = node(osm_id=10, lat=43.320011, lon=21.895011, name="Pekara Branko", shop="bakery")
    rich = way(
        osm_id=20,
        lat=43.320012,
        lon=21.895012,
        name="Pekara Branko",
        shop="bakery",
        phone="+381 18 123456",
        website="pekarabranko.rs",
    )
    row = only(parse_elements([thin, rich]))
    assert row.osm_type == "way"
    assert row.osm_id == 20
    assert row.phone == "+38118123456"


def test_duplicate_tie_prefers_the_node() -> None:
    n = node(osm_id=10, lat=43.32, lon=21.895, name="Apoteka", amenity="pharmacy")
    w = way(osm_id=20, lat=43.32, lon=21.895, name="Apoteka", amenity="pharmacy")
    row = only(parse_elements([w, n]))
    assert row.osm_type == "node"
    assert row.osm_id == 10


def test_dedupe_is_case_insensitive_on_name() -> None:
    a = node(osm_id=10, lat=43.32, lon=21.895, name="APOTEKA", amenity="pharmacy")
    b = way(osm_id=20, lat=43.32, lon=21.895, name="apoteka", amenity="pharmacy")
    assert len(parse_elements([a, b])) == 1


def test_node_and_way_tens_of_metres_apart_are_one_poi() -> None:
    """A building centre sits well off the node inside it, but it is one shop."""
    n = node(osm_id=10, lat=43.32, lon=21.895, name="Idea", shop="convenience")
    w = way(osm_id=20, lat=43.32027, lon=21.895, name="Idea", shop="convenience")  # ~30 m
    assert len(parse_elements([n, w])) == 1


def test_same_name_at_different_addresses_are_separate_branches() -> None:
    a = node(osm_id=10, lat=43.32, lon=21.895, name="Benu", amenity="pharmacy",
             **{"addr:street": "Obrenoviceva"})
    b = node(osm_id=11, lat=43.44, lon=21.995, name="Benu", amenity="pharmacy",
             **{"addr:street": "Vozdova"})
    assert len(parse_elements([a, b])) == 2


def test_same_name_and_different_phones_are_separate_branches() -> None:
    a = node(osm_id=10, lat=43.32, lon=21.895, name="Benu", amenity="pharmacy", phone="018111111")
    b = node(osm_id=11, lat=43.44, lon=21.995, name="Benu", amenity="pharmacy", phone="018222222")
    assert len(parse_elements([a, b])) == 2


def test_rows_nothing_can_tell_apart_collapse_however_far_apart() -> None:
    """Same name, same missing address, same central number: one row in an
    export, because there is nothing to act on differently."""
    a = node(osm_id=10, lat=43.32, lon=21.895, name="Erste Bank", amenity="bank", phone="0800111")
    b = node(osm_id=11, lat=43.44, lon=21.995, name="Erste Bank", amenity="bank", phone="0800111")
    assert len(parse_elements([a, b])) == 1


def test_same_name_different_categories_are_kept_apart() -> None:
    """"Tvrdjava" the bakery and "Tvrdjava" the pharmacy are two businesses."""
    a = node(osm_id=10, lat=43.32, lon=21.895, name="Tvrdjava", shop="bakery")
    b = node(osm_id=11, lat=43.32045, lon=21.895, name="Tvrdjava", amenity="pharmacy")  # ~50 m
    assert len(parse_elements([a, b])) == 2


def test_same_name_different_categories_collapse_when_on_top_of_each_other() -> None:
    """One shop double-tagged: a cafe that also sells groceries, mapped twice."""
    a = node(osm_id=10, lat=43.32, lon=21.895, name="DriveCafe", amenity="cafe")
    b = node(osm_id=11, lat=43.320045, lon=21.895, name="DriveCafe", shop="convenience")  # ~5 m
    assert len(parse_elements([a, b])) == 1


# --- phone ------------------------------------------------------------------


def test_phone_normalized_keeps_leading_plus() -> None:
    row = only(parse_elements([node(name="A", shop="bakery", phone="+381 (18) 123-456")]))
    assert row.phone == "+38118123456"
    assert row.phone_alt == ""


def test_contact_phone_fallback() -> None:
    row = only(parse_elements([node(name="A", shop="bakery", **{"contact:phone": "018/123-456"})]))
    assert row.phone == "018123456"


def test_contact_mobile_fallback() -> None:
    row = only(parse_elements([node(name="A", shop="bakery", **{"contact:mobile": "064 111 2222"})]))
    assert row.phone == "0641112222"


def test_phone_preferred_over_contact_phone_and_mobile() -> None:
    tags = {"phone": "+381181111", "contact:phone": "+381182222", "contact:mobile": "+381643333"}
    row = only(parse_elements([node(name="A", shop="bakery", **tags)]))
    assert row.phone == "+381181111"


def test_multi_value_phone_split_into_phone_and_phone_alt() -> None:
    element = node(name="A", shop="bakery", phone="+381 18 111; +381 64 222 33;018-3333")
    row = only(parse_elements([element]))
    assert row.phone == "+38118111"
    assert row.phone_alt == "+3816422233;0183333"


# --- website ----------------------------------------------------------------


def test_website_without_scheme_gets_https() -> None:
    row = only(parse_elements([node(name="A", shop="bakery", website="pekarabranko.rs")]))
    assert row.website == "https://pekarabranko.rs"


def test_website_with_scheme_is_left_alone() -> None:
    element = node(name="A", shop="bakery", website="http://pekarabranko.rs/kontakt")
    assert only(parse_elements([element])).website == "http://pekarabranko.rs/kontakt"


def test_contact_website_and_url_fallbacks() -> None:
    a = node(osm_id=1, name="A", shop="bakery", **{"contact:website": "a.rs"})
    b = node(osm_id=2, lat=44.0, name="B", shop="bakery", **{"url": "b.rs"})
    rows = parse_elements([a, b])
    assert [r.website for r in rows] == ["https://a.rs", "https://b.rs"]


@pytest.mark.parametrize("value", ["yes", "no", "-", "none", "n/a", "http://", "not a url at all"])
def test_implausible_website_values_are_discarded(value: str) -> None:
    row = only(parse_elements([node(name="A", shop="bakery", website=value)]))
    assert row.website == ""


# --- address / place --------------------------------------------------------


def test_address_columns_are_split() -> None:
    tags = {
        "addr:street": "Obrenoviceva",
        "addr:housenumber": "10a",
        "addr:city": "Nis",
        "addr:postcode": "18000",
    }
    row = only(parse_elements([node(name="A", shop="bakery", **tags)]))
    assert (row.street, row.housenumber, row.postcode) == ("Obrenoviceva", "10a", "18000")
    assert row.place == "Nis"


def test_place_falls_back_to_suburb_then_place() -> None:
    a = node(osm_id=1, name="A", shop="bakery", **{"addr:suburb": "Medijana"})
    b = node(osm_id=2, lat=44.0, name="B", shop="bakery", **{"addr:place": "Niska Banja"})
    rows = parse_elements([a, b])
    assert [r.place for r in rows] == ["Medijana", "Niska Banja"]


# --- misc tags --------------------------------------------------------------


def test_email_facebook_instagram_and_opening_hours() -> None:
    tags = {
        "contact:email": "info@a.rs",
        "contact:facebook": "https://facebook.com/a",
        "contact:instagram": "a_rs",
        "opening_hours": "Mo-Fr 08:00-16:00",
    }
    row = only(parse_elements([node(name="A", shop="bakery", **tags)]))
    assert row.email == "info@a.rs"
    assert row.facebook == "https://facebook.com/a"
    assert row.instagram == "a_rs"
    assert row.opening_hours == "Mo-Fr 08:00-16:00"


def test_element_with_zero_optional_tags_yields_empty_strings() -> None:
    row = only(parse_elements([node(name="Gola prodavnica", shop="convenience")]))
    assert row.place == ""
    assert row.street == ""
    assert row.housenumber == ""
    assert row.postcode == ""
    assert row.phone == ""
    assert row.phone_alt == ""
    assert row.website == ""
    assert row.email == ""
    assert row.facebook == ""
    assert row.instagram == ""
    assert row.opening_hours == ""


def test_element_without_tags_key_does_not_raise() -> None:
    assert parse_elements([{"type": "node", "id": 1, "lat": 43.0, "lon": 21.0}]) == []
