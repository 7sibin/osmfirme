"""The contact pass: what it reads, what it reuses, and how it fails."""

from __future__ import annotations

import pytest

from osm_businesses import Row
from webapp.contacts import CONTACT_DEAD, CONTACT_NONE, CONTACT_OK, ContactHit
from webapp.contact_runner import ContactState, run_contacts
from webapp.site_cache import ContactCache


def row(name, *, osm_id, website="", email="", phone="") -> Row:
    return Row(
        osm_type="node", osm_id=osm_id, name=name, category="bakery", place="Nis",
        street="Nemanjina", housenumber="1", postcode="18000", phone=phone, phone_alt="",
        website=website, email=email, facebook="", instagram="", opening_hours="",
        lat=43.32, lon=21.9, category_key="shop",
    )


def scraper_for(answers):
    """A scraper whose verdict is looked up by the row's website, recording calls."""
    calls: list[str] = []

    class Fake:
        def read(self, r):
            calls.append(r.website)
            return answers[r.website]

    return (lambda: Fake()), calls


def hit(email="", phone="", status=CONTACT_OK) -> ContactHit:
    return ContactHit(email=email, phone=phone, status=status,
                      checked_at="2026-09-09T00:00:00+00:00")


def cache_in(tmp_path) -> ContactCache:
    return ContactCache(tmp_path / "contacts")


@pytest.mark.asyncio
async def test_a_pass_records_what_it_reads(tmp_path):
    rows = [row("A", osm_id=1, website="https://a.rs"), row("B", osm_id=2, website="https://b.rs")]
    factory, _ = scraper_for({
        "https://a.rs": hit(email="info@a.rs", phone="018111111"),
        "https://b.rs": hit(status=CONTACT_NONE),
    })
    state = ContactState()
    await run_contacts(rows, state, cache=cache_in(tmp_path), scraper_factory=factory)

    assert state.progress.status == "done"
    assert state.progress.checked == 2
    assert state.progress.emails == 1
    assert state.progress.phones == 1
    assert state.hits[1].email == "info@a.rs"
    assert state.hits[2].status == CONTACT_NONE


@pytest.mark.asyncio
async def test_a_number_the_map_already_had_is_not_counted_as_new(tmp_path):
    rows = [row("A", osm_id=1, website="https://a.rs", phone="018999999")]
    factory, _ = scraper_for({"https://a.rs": hit(email="info@a.rs", phone="018111111")})
    state = ContactState()
    await run_contacts(rows, state, cache=cache_in(tmp_path), scraper_factory=factory)

    assert state.progress.emails == 1
    assert state.progress.phones == 0


@pytest.mark.asyncio
async def test_dead_sites_are_counted_separately(tmp_path):
    rows = [row("A", osm_id=1, website="https://gone.rs")]
    factory, _ = scraper_for({"https://gone.rs": hit(status=CONTACT_DEAD)})
    state = ContactState()
    await run_contacts(rows, state, cache=cache_in(tmp_path), scraper_factory=factory)

    assert state.progress.dead == 1
    assert state.hits[1].status == CONTACT_DEAD


@pytest.mark.asyncio
async def test_rows_with_no_site_are_never_visited(tmp_path):
    rows = [row("A", osm_id=1), row("B", osm_id=2, website="https://b.rs")]
    factory, calls = scraper_for({"https://b.rs": hit(email="info@b.rs")})
    state = ContactState()
    await run_contacts(rows, state, cache=cache_in(tmp_path), scraper_factory=factory)

    assert calls == ["https://b.rs"]
    assert 1 not in state.hits


@pytest.mark.asyncio
async def test_branches_sharing_one_website_are_read_once(tmp_path):
    """A chain points fifteen rows at one site. That is one fetch, not fifteen."""
    rows = [row(f"Maxi {n}", osm_id=n, website="https://maxi.rs") for n in range(1, 6)]
    factory, calls = scraper_for({"https://maxi.rs": hit(email="info@maxi.rs")})
    state = ContactState()
    await run_contacts(rows, state, cache=cache_in(tmp_path), scraper_factory=factory, workers=1)

    assert len(calls) == 1
    assert all(state.hits[n].email == "info@maxi.rs" for n in range(1, 6))


@pytest.mark.asyncio
async def test_a_pass_stops_at_the_limit_and_reports_the_rest(tmp_path):
    rows = [row(f"F{n}", osm_id=n, website=f"https://f{n}.rs") for n in range(1, 6)]
    factory, calls = scraper_for({f"https://f{n}.rs": hit(email=f"a@f{n}.rs") for n in range(1, 6)})
    state = ContactState()
    await run_contacts(rows, state, cache=cache_in(tmp_path), scraper_factory=factory, limit=2)

    assert len(calls) == 2
    assert state.progress.remaining == 3


@pytest.mark.asyncio
async def test_a_second_pass_continues_where_the_first_stopped(tmp_path):
    rows = [row(f"F{n}", osm_id=n, website=f"https://f{n}.rs") for n in range(1, 5)]
    answers = {f"https://f{n}.rs": hit(email=f"a@f{n}.rs") for n in range(1, 5)}
    factory, calls = scraper_for(answers)
    state = ContactState()
    cache = cache_in(tmp_path)

    await run_contacts(rows, state, cache=cache, scraper_factory=factory, limit=2, workers=1)
    await run_contacts(rows, state, cache=cache, scraper_factory=factory, limit=2, workers=1)

    assert len(calls) == 4
    assert state.progress.remaining == 0


@pytest.mark.asyncio
async def test_a_cached_site_costs_no_fetch(tmp_path):
    rows = [row("A", osm_id=1, website="https://a.rs")]
    factory, calls = scraper_for({"https://a.rs": hit(email="info@a.rs")})
    cache = cache_in(tmp_path)

    await run_contacts(rows, ContactState(), cache=cache, scraper_factory=factory)
    assert len(calls) == 1

    state = ContactState()
    await run_contacts(rows, state, cache=ContactCache(cache.directory), scraper_factory=factory)
    assert len(calls) == 1, "the second pass must read the cache, not fetch again"
    assert state.hits[1].email == "info@a.rs"


@pytest.mark.asyncio
async def test_cancelling_stops_the_pass_and_keeps_what_it_had(tmp_path):
    rows = [row(f"F{n}", osm_id=n, website=f"https://f{n}.rs") for n in range(1, 9)]
    answers = {f"https://f{n}.rs": hit(email=f"a@f{n}.rs") for n in range(1, 9)}
    factory, calls = scraper_for(answers)
    state = ContactState()
    await run_contacts(
        rows, state, cache=cache_in(tmp_path), scraper_factory=factory, workers=2,
        is_cancelled=lambda: len(calls) >= 4,
    )
    assert state.progress.status == "cancelled"
    assert 0 < len(state.hits) < 8


@pytest.mark.asyncio
async def test_one_broken_site_does_not_lose_the_whole_pass(tmp_path):
    rows = [row("A", osm_id=1, website="https://a.rs"), row("B", osm_id=2, website="https://b.rs")]

    class Exploding:
        def read(self, r):
            if r.osm_id == 2:
                raise RuntimeError("boom")
            return hit(email="info@a.rs")

    state = ContactState()
    await run_contacts(rows, state, cache=cache_in(tmp_path), scraper_factory=lambda: Exploding(),
                       workers=1)
    assert state.progress.status == "error"
    assert state.hits[1].email == "info@a.rs"


@pytest.mark.asyncio
async def test_a_pass_with_nothing_to_do_says_so(tmp_path):
    rows = [row("A", osm_id=1, website="https://a.rs", email="a@a.rs", phone="018111")]
    factory, calls = scraper_for({})
    state = ContactState()
    await run_contacts(rows, state, cache=cache_in(tmp_path), scraper_factory=factory)

    assert state.progress.status == "done"
    assert calls == []
