"""Tests for area resolution. The HTTP layer is mocked; nothing touches the network."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

import geo
from geo import (
    AreaCache,
    AreaCandidate,
    NominatimClient,
    PlaceNotFoundError,
    RateLimiter,
    format_candidates,
    resolve_place,
    to_area_id,
)


# --- fakes ------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Stands in for requests.Session; records every call."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payload)


def nominatim_result(
    osm_type: str = "relation",
    osm_id: int = 1234,
    display_name: str = "Nis, Nisavski okrug, Serbia",
    klass: str = "boundary",
    type_: str = "administrative",
    admin_level: int | None = 7,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "osm_type": osm_type,
        "osm_id": osm_id,
        "display_name": display_name,
        "class": klass,
        "type": type_,
    }
    if admin_level is not None:
        result["address"] = {"admin_level": admin_level}
        result["extratags"] = {"admin_level": str(admin_level)}
    return result


def client_for(payload: Any) -> tuple[NominatimClient, FakeSession]:
    session = FakeSession(payload)
    client = NominatimClient(
        session=session,
        user_agent="osm_businesses-test/0.0 (+test)",
        rate_limiter=RateLimiter(min_interval=0.0),
    )
    return client, session


# --- area id arithmetic -----------------------------------------------------


def test_relation_osm_id_becomes_area_id() -> None:
    assert to_area_id("relation", 1234) == 3600001234


def test_way_osm_id_becomes_area_id() -> None:
    assert to_area_id("way", 1234) == 2400001234


def test_node_has_no_area_id() -> None:
    with pytest.raises(ValueError):
        to_area_id("node", 1234)


def test_candidate_exposes_its_area_id() -> None:
    candidate = AreaCandidate(
        osm_type="relation",
        osm_id=1234,
        display_name="Nis",
        category="boundary",
        place_type="administrative",
        admin_level="7",
    )
    assert candidate.area_id == 3600001234


# --- Nominatim query --------------------------------------------------------


def test_search_sends_required_parameters_and_user_agent() -> None:
    client, session = client_for([nominatim_result()])
    client.search("Nis", country="RS")

    call = session.calls[0]
    assert call["url"].endswith("/search")
    assert call["params"]["q"] == "Nis"
    assert call["params"]["format"] == "jsonv2"
    assert call["params"]["addressdetails"] == 1
    assert call["params"]["limit"] == 10
    assert call["params"]["countrycodes"] == "rs"
    assert "osm_businesses" in call["headers"]["User-Agent"]


def test_search_omits_countrycodes_when_no_country_given() -> None:
    client, session = client_for([nominatim_result()])
    client.search("Nis")
    assert "countrycodes" not in session.calls[0]["params"]


def test_non_relation_results_are_filtered_out() -> None:
    payload = [
        nominatim_result(osm_type="node", osm_id=1, display_name="Nis (node)"),
        nominatim_result(osm_type="way", osm_id=2, display_name="Nis (way)"),
        nominatim_result(osm_type="relation", osm_id=3, display_name="Nis (relation)"),
    ]
    client, _ = client_for(payload)
    candidates = client.search("Nis")
    assert [c.osm_id for c in candidates] == [3]


def test_results_of_wrong_class_are_filtered_out() -> None:
    payload = [
        nominatim_result(osm_id=1, klass="highway", type_="residential"),
        nominatim_result(osm_id=2, klass="boundary", type_="administrative"),
        nominatim_result(osm_id=3, klass="place", type_="city"),
        nominatim_result(osm_id=4, klass="building", type_="yes"),
    ]
    client, _ = client_for(payload)
    assert [c.osm_id for c in client.search("Nis")] == [2, 3]


def test_jsonv2_category_field_is_recognised() -> None:
    # format=jsonv2 returns the class under "category"; only the older
    # format=json calls it "class". Real Nominatim payloads have no "class" key.
    payload = [
        {
            "osm_type": "relation",
            "osm_id": 11538321,
            "display_name": "Nis, Grad Nis, Serbia",
            "category": "place",
            "type": "city",
            "address": {"ISO3166-2-lvl6": "RS-18", "city": "Nis"},
        }
    ]
    client, _ = client_for(payload)
    candidates = client.search("Nis")
    assert [c.osm_id for c in candidates] == [11538321]
    assert candidates[0].category == "place"


def test_jsonv2_category_is_filtered_on_too() -> None:
    payload = [
        {"osm_type": "relation", "osm_id": 1, "category": "highway", "type": "residential"},
        {"osm_type": "relation", "osm_id": 2, "category": "boundary", "type": "administrative"},
    ]
    client, _ = client_for(payload)
    assert [c.osm_id for c in client.search("Nis")] == [2]


def test_admin_level_read_from_iso3166_when_extratags_absent() -> None:
    payload = [
        {
            "osm_type": "relation",
            "osm_id": 7,
            "category": "boundary",
            "type": "administrative",
            "address": {"ISO3166-2-lvl6": "RS-18"},
        }
    ]
    client, _ = client_for(payload)
    assert client.search("Nis")[0].admin_level == "6"


def test_search_survives_results_missing_optional_fields() -> None:
    payload = [{"osm_type": "relation", "osm_id": 9, "class": "place", "type": "town"}]
    client, _ = client_for(payload)
    candidate = client.search("Nowhere")[0]
    assert candidate.display_name == ""
    assert candidate.admin_level == ""


# --- candidate table --------------------------------------------------------


def test_table_numbers_candidates_from_one_and_shows_all_columns() -> None:
    candidates = [
        AreaCandidate("relation", 1234, "Nis, Serbia", "boundary", "administrative", "7"),
        AreaCandidate("relation", 5678, "Nis, City of Nis, Serbia", "place", "city", "8"),
    ]
    table = format_candidates(candidates)
    lines = table.splitlines()

    header = lines[0]
    for column in ("#", "display_name", "type", "osm_id", "admin_level"):
        assert column in header

    assert lines[1].startswith("1")
    assert "Nis, Serbia" in lines[1]
    assert "administrative" in lines[1]
    assert "1234" in lines[1]
    assert lines[1].rstrip().endswith("7")

    assert lines[2].startswith("2")
    assert "5678" in lines[2]
    assert "city" in lines[2]


# --- resolution -------------------------------------------------------------


def test_single_match_is_used_and_reported_to_stderr() -> None:
    client, _ = client_for([nominatim_result(osm_id=1234)])
    stream = io.StringIO()

    candidate = resolve_place("Nis", client=client, stream=stream)

    assert candidate.area_id == 3600001234
    assert "1234" in stream.getvalue()
    assert "Nis" in stream.getvalue()


def test_multiple_candidates_print_table_and_pick_is_honored() -> None:
    payload = [
        nominatim_result(osm_id=1234, display_name="Nis, Nisavski okrug, Serbia", admin_level=7),
        nominatim_result(osm_id=5678, display_name="Nis, City of Nis, Serbia", klass="place", type_="city", admin_level=8),
    ]
    client, _ = client_for(payload)
    stream = io.StringIO()

    def no_input(prompt: str) -> str:
        raise AssertionError("--pick must not prompt")

    candidate = resolve_place("Nis", client=client, pick=2, stream=stream, input_fn=no_input)

    assert candidate.osm_id == 5678
    assert candidate.area_id == 3600005678
    printed = stream.getvalue()
    assert "Nis, Nisavski okrug, Serbia" in printed
    assert "Nis, City of Nis, Serbia" in printed


def test_pick_out_of_range_is_an_error() -> None:
    payload = [nominatim_result(osm_id=1), nominatim_result(osm_id=2)]
    client, _ = client_for(payload)
    with pytest.raises(PlaceNotFoundError):
        resolve_place("Nis", client=client, pick=5, stream=io.StringIO())


def test_yes_takes_the_top_match() -> None:
    payload = [nominatim_result(osm_id=1234), nominatim_result(osm_id=5678)]
    client, _ = client_for(payload)

    def no_input(prompt: str) -> str:
        raise AssertionError("--yes must not prompt")

    candidate = resolve_place("Nis", client=client, assume_yes=True, stream=io.StringIO(), input_fn=no_input)
    assert candidate.osm_id == 1234


def test_multiple_candidates_prompt_interactively() -> None:
    payload = [nominatim_result(osm_id=1234), nominatim_result(osm_id=5678)]
    client, _ = client_for(payload)
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return "2"

    candidate = resolve_place("Nis", client=client, stream=io.StringIO(), input_fn=fake_input)
    assert candidate.osm_id == 5678
    assert prompts, "user should have been prompted"


def test_zero_results_raises_clean_error_suggesting_bbox() -> None:
    client, _ = client_for([])
    with pytest.raises(PlaceNotFoundError) as excinfo:
        resolve_place("Atlantis", client=client, stream=io.StringIO())
    message = str(excinfo.value)
    assert "Atlantis" in message
    assert "--bbox" in message


def test_only_non_relation_results_also_raises_clean_error() -> None:
    client, _ = client_for([nominatim_result(osm_type="node", osm_id=1)])
    with pytest.raises(PlaceNotFoundError):
        resolve_place("Somewhere", client=client, stream=io.StringIO())


# --- cache ------------------------------------------------------------------


def test_cache_hit_skips_nominatim(tmp_path: Any) -> None:
    cache = AreaCache(tmp_path / "areas.json")
    client, session = client_for([nominatim_result(osm_id=1234)])

    first = resolve_place("Nis", client=client, cache=cache, stream=io.StringIO())
    second = resolve_place("Nis", client=client, cache=cache, stream=io.StringIO())

    assert first.area_id == second.area_id == 3600001234
    assert len(session.calls) == 1, "second resolution should be served from cache"


def test_cache_is_keyed_on_name_and_country(tmp_path: Any) -> None:
    cache = AreaCache(tmp_path / "areas.json")
    client, session = client_for([nominatim_result(osm_id=1234)])

    resolve_place("Nis", client=client, cache=cache, stream=io.StringIO())
    resolve_place("Nis", client=client, country="RS", cache=cache, stream=io.StringIO())

    assert len(session.calls) == 2


def test_cache_survives_a_corrupt_file(tmp_path: Any) -> None:
    path = tmp_path / "areas.json"
    path.write_text("{not json", encoding="utf-8")
    cache = AreaCache(path)
    assert cache.get("nis") is None

    client, _ = client_for([nominatim_result(osm_id=1234)])
    candidate = resolve_place("Nis", client=client, cache=cache, stream=io.StringIO())
    assert candidate.area_id == 3600001234


# --- rate limiting ----------------------------------------------------------


class FakeClock:
    """Deterministic stand-in for time.monotonic / time.sleep."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_delays_the_second_call() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=1.0, monotonic=clock.monotonic, sleep=clock.sleep)

    limiter.wait()
    assert clock.slept == [], "first call must not be delayed"

    clock.now += 0.25
    limiter.wait()

    assert len(clock.slept) == 1
    assert clock.slept[0] == pytest.approx(0.75)


def test_rate_limiter_does_not_delay_when_enough_time_passed() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=1.0, monotonic=clock.monotonic, sleep=clock.sleep)

    limiter.wait()
    clock.now += 5.0
    limiter.wait()

    assert clock.slept == []


def test_client_rate_limits_between_searches() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    session = FakeSession([nominatim_result()])
    client = NominatimClient(session=session, user_agent="test/0.0", rate_limiter=limiter)

    client.search("Nis")
    client.search("Beograd")

    assert len(session.calls) == 2
    assert clock.slept == [pytest.approx(1.0)]


def test_default_rate_limiter_is_one_per_second() -> None:
    assert geo.NOMINATIM_MIN_INTERVAL == 1.0
    assert RateLimiter().min_interval == 1.0
