from __future__ import annotations

import pytest

from geo import RateLimiter
from webapp.nominatim import GeometryClient

PAYLOAD = [
    {
        "osm_type": "relation",
        "osm_id": 11538321,
        "display_name": "Nis, Grad Nis, Srbija",
        "boundingbox": ["43.2", "43.4", "21.8", "22.0"],
        "geojson": {"type": "Polygon", "coordinates": [[[21.8, 43.2], [22.0, 43.2], [22.0, 43.4], [21.8, 43.2]]]},
    }
]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return self.response


def make_client(response) -> tuple[GeometryClient, FakeSession]:
    session = FakeSession(response)
    limiter = RateLimiter(min_interval=0, monotonic=lambda: 0.0, sleep=lambda _: None)
    return GeometryClient(session, "test-agent", rate_limiter=limiter), session


def test_lookup_asks_for_the_polygon():
    client, session = make_client(FakeResponse(PAYLOAD))
    client.lookup("relation", 11538321)
    _, params = session.calls[0]
    assert params["osm_ids"] == "R11538321"
    assert params["polygon_geojson"] == 1


def test_lookup_returns_geojson_and_a_south_west_north_east_bbox():
    client, _ = make_client(FakeResponse(PAYLOAD))
    result = client.lookup("relation", 11538321)
    assert result is not None
    assert result["geojson"]["type"] == "Polygon"
    assert result["bbox"] == [43.2, 21.8, 43.4, 22.0]
    assert result["display_name"].startswith("Nis")


def test_way_and_node_get_their_own_prefix():
    client, session = make_client(FakeResponse(PAYLOAD))
    client.lookup("way", 5)
    client.lookup("node", 6)
    assert session.calls[0][1]["osm_ids"] == "W5"
    assert session.calls[1][1]["osm_ids"] == "N6"


def test_empty_response_returns_none():
    client, _ = make_client(FakeResponse([]))
    assert client.lookup("relation", 1) is None


def test_result_without_geojson_returns_none():
    client, _ = make_client(FakeResponse([{"osm_type": "relation", "osm_id": 1}]))
    assert client.lookup("relation", 1) is None


def test_unknown_osm_type_raises_value_error():
    client, _ = make_client(FakeResponse(PAYLOAD))
    with pytest.raises(ValueError):
        client.lookup("planet", 1)
