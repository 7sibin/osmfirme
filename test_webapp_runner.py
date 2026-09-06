from __future__ import annotations

from osm_businesses import OverpassError
from webapp.cache import CachedResult, ResultCache, cache_key
from webapp.jobs import JobRegistry
from webapp.models import AreaInput, JobRequest
from webapp.runner import friendly_overpass_error, run_job

ELEMENTS = [
    {
        "type": "node", "id": 1, "lat": 43.32, "lon": 21.9,
        "tags": {"name": "Pekara", "shop": "bakery", "phone": "+381 18 111 222"},
    },
    {
        "type": "node", "id": 2, "lat": 43.33, "lon": 21.91,
        "tags": {"name": "Kafic", "amenity": "cafe"},
    },
]


class FakeClient:
    def __init__(self, elements=None, error=None):
        self.elements = elements or []
        self.error = error
        self.queries: list[str] = []

    def fetch(self, query: str):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.elements


def make_request() -> JobRequest:
    return JobRequest(area=AreaInput(kind="area", area_id=42, label="Nis"), categories=["shop", "amenity"])


async def test_successful_run_fills_rows_and_counts(tmp_path):
    registry, cache = JobRegistry(), ResultCache(tmp_path)
    job = registry.create("Nis")
    client = FakeClient(ELEMENTS)

    registry.start(job, lambda j: run_job(j, make_request(), cache=cache, client_factory=lambda: client))
    await registry.wait(job.job_id)

    assert job.status == "done"
    assert job.elements_found == 2
    assert [row.name for row in job.rows] == ["Pekara", "Kafic"]


async def test_query_only_asks_for_the_requested_categories(tmp_path):
    registry, cache = JobRegistry(), ResultCache(tmp_path)
    job = registry.create("Nis")
    client = FakeClient(ELEMENTS)

    registry.start(job, lambda j: run_job(j, make_request(), cache=cache, client_factory=lambda: client))
    await registry.wait(job.job_id)

    query = client.queries[0]
    assert 'nwr["shop"]' in query
    assert 'nwr["amenity"]' in query
    assert 'nwr["office"]' not in query


async def test_result_is_written_to_the_cache(tmp_path):
    registry, cache = JobRegistry(), ResultCache(tmp_path)
    job = registry.create("Nis")

    request = make_request()
    registry.start(job, lambda j: run_job(j, request, cache=cache, client_factory=lambda: FakeClient(ELEMENTS)))
    await registry.wait(job.job_id)

    key = cache_key(request.area.cache_payload(), request.categories)
    cached = cache.load(key)
    assert cached is not None
    assert len(cached.rows) == 2


async def test_cached_result_skips_the_network(tmp_path):
    registry, cache = JobRegistry(), ResultCache(tmp_path)
    request = make_request()
    key = cache_key(request.area.cache_payload(), request.categories)
    cache.save(key, CachedResult(area_label="Nis", categories=request.categories, elements_found=99, rows=[]))

    job = registry.create("Nis")
    client = FakeClient(ELEMENTS)
    registry.start(job, lambda j: run_job(j, request, cache=cache, client_factory=lambda: client))
    await registry.wait(job.job_id)

    assert client.queries == []
    assert job.elements_found == 99


async def test_overpass_failure_ends_in_error_with_a_serbian_sentence(tmp_path):
    registry, cache = JobRegistry(), ResultCache(tmp_path)
    job = registry.create("Nis")
    client = FakeClient(error=OverpassError("All Overpass endpoints failed. Last error - HTTP 429"))

    registry.start(job, lambda j: run_job(j, make_request(), cache=cache, client_factory=lambda: client))
    await registry.wait(job.job_id)

    assert job.status == "error"
    assert "zauzet" in (job.error or "")


async def test_cancelled_before_the_query_does_not_call_overpass(tmp_path):
    registry, cache = JobRegistry(), ResultCache(tmp_path)
    job = registry.create("Nis")
    job.cancel_event.set()
    client = FakeClient(ELEMENTS)

    registry.start(job, lambda j: run_job(j, make_request(), cache=cache, client_factory=lambda: client))
    await registry.wait(job.job_id)

    assert client.queries == []
    assert job.status == "cancelled"


def test_busy_and_generic_overpass_errors_read_differently():
    busy = friendly_overpass_error(OverpassError("HTTP 429 (server busy or overloaded)"))
    other = friendly_overpass_error(OverpassError("request failed: connection reset"))
    assert "zauzet" in busy
    assert busy != other
