from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from osm_businesses import Row
from webapp.cache import CachedResult, ResultCache, cache_key


def make_row(name: str = "Pekara") -> Row:
    return Row(
        osm_type="node", osm_id=1, name=name, category="bakery", place="Nis",
        street="Obrenoviceva", housenumber="10", postcode="18000",
        phone="+38118111222", phone_alt="", website="https://example.com",
        email="", facebook="", instagram="", opening_hours="Mo-Fr 08:00-20:00",
        lat=43.32, lon=21.9, category_key="shop",
    )


def test_key_is_stable_regardless_of_category_order():
    payload = {"kind": "area", "area_id": 42}
    assert cache_key(payload, ["shop", "amenity"]) == cache_key(payload, ["amenity", "shop"])


def test_key_changes_with_the_area():
    assert cache_key({"kind": "area", "area_id": 42}, ["shop"]) != cache_key(
        {"kind": "area", "area_id": 43}, ["shop"]
    )


def test_missing_key_loads_as_none(tmp_path):
    assert ResultCache(tmp_path).load("nope") is None


def test_round_trip_preserves_rows(tmp_path):
    cache = ResultCache(tmp_path)
    result = CachedResult(
        area_label="Nis", categories=["shop"], elements_found=7, rows=[make_row()],
    )
    cache.save("k1", result)

    loaded = cache.load("k1")
    assert loaded is not None
    assert loaded.area_label == "Nis"
    assert loaded.elements_found == 7
    assert loaded.rows == [make_row()]


def test_stale_entry_is_ignored(tmp_path):
    cache = ResultCache(tmp_path)
    cache.save("k1", CachedResult(area_label="Nis", categories=["shop"], elements_found=1, rows=[make_row()]))

    path = tmp_path / "k1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.load("k1") is None


def test_entry_from_another_version_is_ignored(tmp_path):
    cache = ResultCache(tmp_path)
    cache.save("k1", CachedResult(area_label="Nis", categories=["shop"], elements_found=1, rows=[make_row()]))

    path = tmp_path / "k1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.load("k1") is None


def test_corrupt_entry_is_ignored_not_raised(tmp_path):
    (tmp_path / "k1.json").write_text("{not json", encoding="utf-8")
    assert ResultCache(tmp_path).load("k1") is None


def test_save_creates_the_directory(tmp_path):
    nested = tmp_path / "a" / "b"
    ResultCache(nested).save("k1", CachedResult(area_label="X", categories=[], elements_found=0, rows=[]))
    assert (nested / "k1.json").exists()
