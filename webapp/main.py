"""FastAPI application: JSON API plus the static frontend.

Every outbound client is provided through a dependency so tests can override
it and never touch the network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from geo import NominatimClient, RateLimiter
from osm_businesses import USER_AGENT, OverpassClient

from webapp.cache import ResultCache, default_cache_dir
from webapp.jobs import JobRegistry
from webapp.nominatim import GeometryClient

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="OSM firme")


# --- dependencies -----------------------------------------------------------


@lru_cache(maxsize=1)
def _session() -> requests.Session:
    return requests.Session()


@lru_cache(maxsize=1)
def _nominatim_rate_limiter() -> RateLimiter:
    """One limiter shared by /search and /lookup: the policy is per service, not per endpoint."""
    return RateLimiter()


def get_nominatim() -> NominatimClient:
    return NominatimClient(_session(), USER_AGENT, rate_limiter=_nominatim_rate_limiter())


def get_geometry() -> GeometryClient:
    return GeometryClient(_session(), USER_AGENT, rate_limiter=_nominatim_rate_limiter())


@lru_cache(maxsize=1)
def _registry() -> JobRegistry:
    return JobRegistry()


def get_registry() -> JobRegistry:
    return _registry()


@lru_cache(maxsize=1)
def _cache() -> ResultCache:
    return ResultCache(default_cache_dir())


def get_cache() -> ResultCache:
    return _cache()


def get_overpass_factory() -> Callable[[], OverpassClient]:
    return lambda: OverpassClient()


# --- routes -----------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/places")
def places(
    q: str = Query(min_length=2),
    country: str | None = Query(default=None, max_length=2),
    nominatim: NominatimClient = Depends(get_nominatim),
) -> list[dict]:
    try:
        candidates = nominatim.search(q, country=country)
    except Exception:
        logger.exception("Nominatim search failed for %r", q)
        raise HTTPException(
            status_code=503,
            detail="Pretraga mesta trenutno ne radi. Pokusaj ponovo za koji trenutak.",
        ) from None
    return [{**candidate.to_dict(), "area_id": candidate.area_id} for candidate in candidates]


@app.get("/api/places/{osm_type}/{osm_id}/geometry")
def place_geometry(
    osm_type: str,
    osm_id: int,
    geometry: GeometryClient = Depends(get_geometry),
) -> dict:
    try:
        result = geometry.lookup(osm_type, osm_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("Nominatim lookup failed for %s/%s", osm_type, osm_id)
        raise HTTPException(
            status_code=503,
            detail="Ne mogu da preuzmem granicu oblasti. Pokusaj ponovo.",
        ) from None
    if result is None:
        raise HTTPException(status_code=404, detail="Za ovu oblast nema granice na mapi.")
    return result


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
