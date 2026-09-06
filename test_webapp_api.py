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
