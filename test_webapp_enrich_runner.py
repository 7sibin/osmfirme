"""The website-search pass: what it skips, what it caches, and how it fails."""

from __future__ import annotations

import pytest

from osm_businesses import Row
from webapp.enrich import SiteHit, WebsiteFinder
from webapp.enrich_runner import EnrichState, pending, run_enrichment
from webapp.results import FOUND_NONE, FOUND_STRONG, FOUND_WEAK
from webapp.site_cache import SiteCache

def row(name, *, osm_id, website="", place="Nis") -> Row:
    return Row(
        osm_type="node", osm_id=osm_id, name=name, category="bakery", place=place,
        street="Nemanjina", housenumber="1", postcode="18000", phone="", phone_alt="",
        website=website, email="", facebook="", instagram="", opening_hours="",
        lat=43.32, lon=21.9, category_key="shop",
    )


def finder_for(answers):
    """A finder whose verdict is looked up by business name."""
    calls: list[str] = []

    class Fake(WebsiteFinder):
        def __init__(self):
            pass

        def find(self, r):
            calls.append(r.name)
            return answers[r.name]

    return Fake, calls


def cache_in(tmp_path) -> SiteCache:
    return SiteCache(tmp_path / "sites")


# --- what a pass takes on ---------------------------------------------------


def test_pending_skips_rows_that_already_have_a_site():
    rows = [row("Pekara Trpkovic", osm_id=1), row("Pekara Sunce", osm_id=2, website="https://a.rs")]
    assert [r.osm_id for r in pending(rows, {})] == [1]


def test_pending_skips_names_that_identify_nothing():
    rows = [row("Pekara Trpkovic", osm_id=1), row("Pekara", osm_id=2)]
    assert [r.osm_id for r in pending(rows, {})] == [1]


def test_pending_skips_what_a_previous_pass_already_answered():
    rows = [row("Pekara Trpkovic", osm_id=1), row("Pekara Sunce", osm_id=2)]
    assert [r.osm_id for r in pending(rows, {1: SiteHit(confidence=FOUND_NONE)})] == [2]


# --- a pass -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pass_records_what_it_finds(tmp_path):
    rows = [row("Pekara Trpkovic", osm_id=1), row("Pekara Sunce", osm_id=2)]
    Fake, _ = finder_for({
        "Pekara Trpkovic": SiteHit(website="https://trpkovic.rs", confidence=FOUND_STRONG,
                                   checked_at="2026-09-08T00:00:00+00:00"),
        "Pekara Sunce": SiteHit(confidence=FOUND_NONE, checked_at="2026-09-08T00:00:00+00:00"),
    })
    state = EnrichState()
    await run_enrichment(rows, state, cache=cache_in(tmp_path), finder_factory=Fake)

    assert state.progress.status == "done"
    assert state.progress.checked == 2
    assert state.progress.found == 1
    assert state.hits[1].website == "https://trpkovic.rs"
    assert state.hits[2].confidence == FOUND_NONE


@pytest.mark.asyncio
async def test_a_pass_stops_at_the_limit_and_reports_the_rest(tmp_path):
    rows = [row(f"Pekara Broj{n}", osm_id=n) for n in range(1, 6)]
    Fake, calls = finder_for({r.name: SiteHit(confidence=FOUND_NONE, checked_at="2026-09-08T00:00:00+00:00") for r in rows})
    state = EnrichState()
    await run_enrichment(rows, state, cache=cache_in(tmp_path), finder_factory=Fake, limit=2)

    assert len(calls) == 2
    assert state.progress.checked == 2
    assert state.progress.remaining == 3


@pytest.mark.asyncio
async def test_a_second_pass_continues_where_the_first_stopped(tmp_path):
    rows = [row(f"Pekara Broj{n}", osm_id=n) for n in range(1, 5)]
    answers = {r.name: SiteHit(confidence=FOUND_NONE, checked_at="2026-09-08T00:00:00+00:00") for r in rows}
    Fake, calls = finder_for(answers)
    state = EnrichState()
    cache = cache_in(tmp_path)

    await run_enrichment(rows, state, cache=cache, finder_factory=Fake, limit=2)
    await run_enrichment(rows, state, cache=cache, finder_factory=Fake, limit=2)

    assert calls == ["Pekara Broj1", "Pekara Broj2", "Pekara Broj3", "Pekara Broj4"]
    assert state.progress.remaining == 0


@pytest.mark.asyncio
async def test_a_failed_search_is_retried_by_the_next_pass(tmp_path):
    """A timeout must not retire a business: it stays unchecked, not "no site"."""
    rows = [row("Pekara Trpkovic", osm_id=1)]
    answers = {"Pekara Trpkovic": SiteHit(confidence="", checked_at="2026-09-08T00:00:00+00:00")}
    Fake, calls = finder_for(answers)
    state = EnrichState()
    cache = cache_in(tmp_path)

    await run_enrichment(rows, state, cache=cache, finder_factory=Fake)
    assert state.progress.failed == 1
    assert 1 not in state.hits

    answers["Pekara Trpkovic"] = SiteHit(website="https://trpkovic.rs", confidence=FOUND_STRONG,
                                         checked_at="2026-09-08T00:00:00+00:00")
    await run_enrichment(rows, state, cache=cache, finder_factory=Fake)
    assert calls == ["Pekara Trpkovic", "Pekara Trpkovic"]
    assert state.hits[1].confidence == FOUND_STRONG


@pytest.mark.asyncio
async def test_a_cached_answer_costs_no_search(tmp_path):
    rows = [row("Pekara Trpkovic", osm_id=1)]
    hit = SiteHit(website="https://trpkovic.rs", confidence=FOUND_STRONG,
                  checked_at="2026-09-08T00:00:00+00:00")
    Fake, calls = finder_for({"Pekara Trpkovic": hit})
    cache = cache_in(tmp_path)

    await run_enrichment(rows, EnrichState(), cache=cache, finder_factory=Fake)
    assert calls == ["Pekara Trpkovic"]

    state = EnrichState()
    await run_enrichment(rows, state, cache=SiteCache(cache.directory), finder_factory=Fake)
    assert calls == ["Pekara Trpkovic"], "the second pass must read the cache, not search again"
    assert state.hits[1].website == "https://trpkovic.rs"


@pytest.mark.asyncio
async def test_cancelling_stops_the_pass_and_keeps_what_it_had(tmp_path):
    rows = [row(f"Pekara Broj{n}", osm_id=n) for n in range(1, 6)]
    Fake, calls = finder_for({r.name: SiteHit(confidence=FOUND_WEAK, website="https://x.rs",
                                              checked_at="2026-09-08T00:00:00+00:00") for r in rows})
    state = EnrichState()
    await run_enrichment(
        rows, state, cache=cache_in(tmp_path), finder_factory=Fake,
        is_cancelled=lambda: len(calls) >= 2,
    )
    assert state.progress.status == "cancelled"
    assert len(state.hits) == 2


@pytest.mark.asyncio
async def test_one_broken_row_does_not_lose_the_whole_pass(tmp_path):
    rows = [row("Pekara Trpkovic", osm_id=1), row("Pekara Sunce", osm_id=2)]

    class Exploding(WebsiteFinder):
        def __init__(self):
            pass

        def find(self, r):
            if r.osm_id == 2:
                raise RuntimeError("boom")
            return SiteHit(website="https://trpkovic.rs", confidence=FOUND_STRONG,
                           checked_at="2026-09-08T00:00:00+00:00")

    state = EnrichState()
    await run_enrichment(rows, state, cache=cache_in(tmp_path), finder_factory=Exploding)
    assert state.progress.status == "error"
    assert state.hits[1].confidence == FOUND_STRONG


@pytest.mark.asyncio
async def test_a_pass_with_nothing_to_do_says_so(tmp_path):
    rows = [row("Pekara Sunce", osm_id=1, website="https://a.rs")]
    Fake, calls = finder_for({})
    state = EnrichState()
    await run_enrichment(rows, state, cache=cache_in(tmp_path), finder_factory=Fake)
    assert state.progress.status == "done"
    assert calls == []
