"""The site cache: what it keys on, and when it stops believing itself."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from osm_businesses import Row
from webapp.enrich import SiteHit
from webapp.results import FOUND_NONE, FOUND_STRONG
from webapp.site_cache import SiteCache, site_key


def row(name, *, category="bakery", place="Nis", street="Nemanjina", osm_id=1) -> Row:
    return Row(
        osm_type="node", osm_id=osm_id, name=name, category=category, place=place,
        street=street, housenumber="1", postcode="18000", phone="", phone_alt="",
        website="", email="", facebook="", instagram="", opening_hours="",
        lat=43.32, lon=21.9, category_key="shop",
    )


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def hit(confidence=FOUND_STRONG, *, age_days=0) -> SiteHit:
    return SiteHit(website="https://trpkovic.rs", confidence=confidence,
                   source="search", checked_at=days_ago(age_days))


# --- the key ----------------------------------------------------------------


def test_the_key_ignores_case_diacritics_and_alphabet():
    assert site_key(row("Pekara Trpkovic")) == site_key(row("PEKARA TRPKOVIĆ"))
    assert site_key(row("Pekara Trpkovic")) == site_key(row("Пекара Трпковић"))


def test_the_key_ignores_the_street_so_a_shop_that_moved_still_hits():
    assert site_key(row("Pekara Trpkovic", street="A")) == site_key(row("Pekara Trpkovic", street="B"))


def test_the_key_separates_towns_and_trades():
    assert site_key(row("Trpkovic", place="Nis")) != site_key(row("Trpkovic", place="Novi Sad"))
    assert site_key(row("Trpkovic", category="bakery")) != site_key(row("Trpkovic", category="cafe"))


# --- the round trip ---------------------------------------------------------


def test_a_finding_survives_a_flush_and_a_fresh_reader(tmp_path):
    cache = SiteCache(tmp_path)
    cache.put(row("Pekara Trpkovic"), hit())
    cache.flush()
    assert SiteCache(tmp_path).get(row("Pekara Trpkovic")).website == "https://trpkovic.rs"


def test_an_unknown_business_is_a_miss_not_an_error(tmp_path):
    assert SiteCache(tmp_path).get(row("Nepoznata Firma")) is None


def test_a_failed_search_is_never_stored(tmp_path):
    """Empty confidence means the search could not run. Storing it would write off a lead."""
    cache = SiteCache(tmp_path)
    cache.put(row("Pekara Trpkovic"), SiteHit(confidence="", checked_at=days_ago(0)))
    cache.flush()
    assert SiteCache(tmp_path).get(row("Pekara Trpkovic")) is None


# --- ageing -----------------------------------------------------------------


def test_a_hit_is_trusted_for_much_longer_than_a_miss(tmp_path):
    """A domain does not evaporate; "no site yet" goes stale the week they build one."""
    cache = SiteCache(tmp_path)
    cache.put(row("Ima Sajt"), hit(FOUND_STRONG, age_days=30))
    cache.put(row("Nema Sajta"), hit(FOUND_NONE, age_days=30))
    cache.flush()

    fresh = SiteCache(tmp_path)
    assert fresh.get(row("Ima Sajt")) is not None
    assert fresh.get(row("Nema Sajta")) is None


def test_even_a_hit_expires_eventually(tmp_path):
    cache = SiteCache(tmp_path)
    cache.put(row("Ima Sajt"), hit(FOUND_STRONG, age_days=90))
    cache.flush()
    assert SiteCache(tmp_path).get(row("Ima Sajt")) is None


def test_an_entry_with_an_unreadable_date_is_a_miss(tmp_path):
    cache = SiteCache(tmp_path)
    cache.put(row("Pekara Trpkovic"), SiteHit(confidence=FOUND_STRONG, checked_at="juce"))
    cache.flush()
    assert SiteCache(tmp_path).get(row("Pekara Trpkovic")) is None


# --- surviving a bad disk ---------------------------------------------------


def test_a_corrupt_shard_reads_as_empty_rather_than_blowing_up(tmp_path):
    cache = SiteCache(tmp_path)
    cache.put(row("Pekara Trpkovic"), hit())
    cache.flush()
    for shard in tmp_path.glob("*.json"):
        shard.write_text("{ not json", encoding="utf-8")
    assert SiteCache(tmp_path).get(row("Pekara Trpkovic")) is None


def test_flushing_into_an_unwritable_place_is_survivable(tmp_path):
    """The findings are already in the job by then; a full disk must not lose the pass."""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("", encoding="utf-8")
    cache = SiteCache(blocked / "sites")
    cache.put(row("Pekara Trpkovic"), hit())
    cache.flush()  # must not raise
