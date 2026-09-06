from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from geo import AreaCandidate
from webapp.main import app, get_geometry, get_nominatim


class FakeNominatim:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error
        self.calls = []

    def search(self, name, country=None, limit=10):
        self.calls.append((name, country))
        if self.error is not None:
            raise self.error
        return self.candidates


class FakeGeometry:
    def __init__(self, result=None):
        self.result = result

    def lookup(self, osm_type, osm_id):
        return self.result


CANDIDATE = AreaCandidate(
    osm_type="relation", osm_id=11538321, display_name="Nis, Grad Nis, Srbija",
    category="boundary", place_type="city", admin_level="6",
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health_is_ok(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_places_returns_candidates_with_area_ids(client):
    app.dependency_overrides[get_nominatim] = lambda: FakeNominatim([CANDIDATE])
    body = client.get("/api/places", params={"q": "Nis", "country": "RS"}).json()
    assert body[0]["area_id"] == 3611538321
    assert body[0]["display_name"].startswith("Nis")


def test_places_with_no_matches_is_an_empty_list_not_an_error(client):
    app.dependency_overrides[get_nominatim] = lambda: FakeNominatim([])
    response = client.get("/api/places", params={"q": "qqqq"})
    assert response.status_code == 200
    assert response.json() == []


def test_places_requires_a_query_of_at_least_two_characters(client):
    assert client.get("/api/places", params={"q": "a"}).status_code == 422


def test_places_reports_a_nominatim_failure_as_503_with_a_sentence(client):
    app.dependency_overrides[get_nominatim] = lambda: FakeNominatim(error=RuntimeError("down"))
    response = client.get("/api/places", params={"q": "Nis"})
    assert response.status_code == 503
    assert "down" not in response.json()["detail"]


def test_geometry_returns_the_polygon(client):
    payload = {"geojson": {"type": "Polygon", "coordinates": []}, "bbox": [1.0, 2.0, 3.0, 4.0], "display_name": "Nis"}
    app.dependency_overrides[get_geometry] = lambda: FakeGeometry(payload)
    assert client.get("/api/places/relation/11538321/geometry").json() == payload


def test_geometry_without_a_polygon_is_404(client):
    app.dependency_overrides[get_geometry] = lambda: FakeGeometry(None)
    assert client.get("/api/places/relation/1/geometry").status_code == 404


def test_root_serves_the_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# --- jobs -------------------------------------------------------------------

from webapp.cache import ResultCache  # noqa: E402
from webapp.jobs import JobRegistry  # noqa: E402
from webapp.main import get_cache, get_overpass_factory, get_registry  # noqa: E402

ELEMENTS = [
    {"type": "node", "id": 1, "lat": 43.32, "lon": 21.9,
     "tags": {"name": "Pekara", "shop": "bakery", "phone": "+38118111222"}},
    {"type": "node", "id": 2, "lat": 43.33, "lon": 21.91,
     "tags": {"name": "Kafic", "amenity": "cafe"}},
]


class StubOverpass:
    def __init__(self, elements):
        self.elements = elements

    def fetch(self, query):
        return self.elements


@pytest.fixture
def registry():
    """One registry for the whole test, so the test can inspect the jobs it created."""
    return JobRegistry()


@pytest.fixture
def wired(tmp_path, registry):
    """A client with a fresh registry, a temp cache and a stubbed Overpass."""
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_cache] = lambda: ResultCache(tmp_path)
    app.dependency_overrides[get_overpass_factory] = lambda: (lambda: StubOverpass(ELEMENTS))
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def post_job(client, **overrides):
    body = {"area": {"kind": "area", "area_id": 3611538321, "label": "Nis"},
            "categories": ["shop", "amenity"]}
    body.update(overrides)
    return client.post("/api/jobs", json=body)


def wait_for_job(client, job_id, tries=100):
    for _ in range(tries):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in {"done", "error", "cancelled"}:
            return body
    raise AssertionError(f"job {job_id} never finished")


def test_post_jobs_returns_a_job_id(wired):
    body = post_job(wired).json()
    assert body["job_id"]
    assert body["cached"] is False


def test_job_runs_to_done_and_reports_counts(wired):
    job_id = post_job(wired).json()["job_id"]
    body = wait_for_job(wired, job_id)
    assert body["status"] == "done"
    assert body["elements_found"] == 2
    assert body["rows"] == 2


def test_a_second_identical_job_is_served_from_the_cache(wired):
    wait_for_job(wired, post_job(wired).json()["job_id"])
    second = post_job(wired).json()
    assert second["cached"] is True


def test_invalid_area_is_422(wired):
    assert post_job(wired, area={"kind": "bbox", "bbox": [1, 2]}).status_code == 422


def test_unknown_category_is_422(wired):
    assert post_job(wired, categories=["nonsense"]).status_code == 422


def test_status_of_an_unknown_job_is_404(wired):
    assert wired.get("/api/jobs/nope").status_code == 404


def test_delete_cancels_a_running_job(wired):
    job_id = post_job(wired).json()["job_id"]
    wired.delete(f"/api/jobs/{job_id}")
    body = wired.get(f"/api/jobs/{job_id}").json()
    assert body["status"] in {"cancelled", "done"}  # a stub can finish before the cancel lands


def test_delete_of_an_unknown_job_is_404(wired):
    assert wired.delete("/api/jobs/nope").status_code == 404


# --- results and export -----------------------------------------------------


def test_results_return_rows_facets_and_total(wired):
    job_id = post_job(wired).json()["job_id"]
    wait_for_job(wired, job_id)

    body = wired.get(f"/api/jobs/{job_id}/results").json()
    assert body["total"] == 2
    assert {item["value"] for item in body["facets"]} == {"bakery", "cafe"}
    assert body["rows"][0]["name"] in {"Pekara", "Kafic"}


def test_results_apply_the_category_filter(wired):
    job_id = post_job(wired).json()["job_id"]
    wait_for_job(wired, job_id)

    body = wired.get(f"/api/jobs/{job_id}/results", params={"categories": "bakery"}).json()
    assert body["total"] == 1
    assert body["rows"][0]["name"] == "Pekara"


def test_results_apply_require_contact(wired):
    job_id = post_job(wired).json()["job_id"]
    wait_for_job(wired, job_id)

    body = wired.get(f"/api/jobs/{job_id}/results", params={"require_contact": "true"}).json()
    assert body["total"] == 1


def test_results_paginate(wired):
    job_id = post_job(wired).json()["job_id"]
    wait_for_job(wired, job_id)

    body = wired.get(f"/api/jobs/{job_id}/results", params={"page": 2, "page_size": 1}).json()
    assert body["page"] == 2
    assert len(body["rows"]) == 1
    assert body["total"] == 2


def test_results_of_an_unfinished_job_are_409(wired, registry):
    # A job that was never started stays "pending"; posting one and forcing its
    # status would race with the background task finishing it.
    job = registry.create("Nis")
    assert wired.get(f"/api/jobs/{job.job_id}/results").status_code == 409


def test_export_returns_an_xlsx_with_a_filename(wired):
    job_id = post_job(wired).json()["job_id"]
    wait_for_job(wired, job_id)

    response = wired.get(f"/api/jobs/{job_id}/export.xlsx")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "firme-" in response.headers["content-disposition"]
    assert response.content[:2] == b"PK"  # xlsx is a zip


def test_export_honours_the_same_filters(wired):
    from io import BytesIO

    from openpyxl import load_workbook

    job_id = post_job(wired).json()["job_id"]
    wait_for_job(wired, job_id)

    response = wired.get(f"/api/jobs/{job_id}/export.xlsx", params={"categories": "bakery"})
    sheet = load_workbook(BytesIO(response.content))["Firme"]
    assert sheet.max_row == 2  # header + one row
