"""Nominatim /lookup, for the boundary polygon the map draws.

geo.NominatimClient covers /search and must not be modified, so the second
endpoint lives here. The same one-request-per-second policy applies, so the
rate limiter is reused rather than reinvented.
"""

from __future__ import annotations

from typing import Any

from geo import NOMINATIM_URL, RateLimiter, SessionLike

PREFIXES = {"relation": "R", "way": "W", "node": "N"}


class GeometryClient:
    def __init__(
        self,
        session: SessionLike,
        user_agent: str,
        rate_limiter: RateLimiter | None = None,
        base_url: str = NOMINATIM_URL,
        timeout: float = 30.0,
    ) -> None:
        self.session = session
        self.user_agent = user_agent
        self.rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def lookup(self, osm_type: str, osm_id: int) -> dict[str, Any] | None:
        prefix = PREFIXES.get(osm_type)
        if prefix is None:
            raise ValueError(f"osm_type {osm_type!r} cannot be looked up")

        self.rate_limiter.wait()
        response = self.session.get(
            f"{self.base_url}/lookup",
            params={
                "osm_ids": f"{prefix}{osm_id}",
                "format": "jsonv2",
                "polygon_geojson": 1,
            },
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            return None

        result = payload[0]
        geojson = result.get("geojson")
        if not isinstance(geojson, dict):
            return None

        return {
            "geojson": geojson,
            "bbox": _bbox(result.get("boundingbox")),
            "display_name": str(result.get("display_name", "")),
        }


def _bbox(raw: Any) -> list[float] | None:
    """Nominatim gives [south, north, west, east]; we want [south, west, north, east]."""
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    try:
        south, north, west, east = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None
    return [south, west, north, east]
