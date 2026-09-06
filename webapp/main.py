"""FastAPI application: JSON API plus the static frontend.

Every outbound client is provided through a dependency so tests can override
it and never touch the network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from geo import NominatimClient, RateLimiter
from osm_businesses import USER_AGENT, OverpassClient

from webapp.cache import ResultCache, cache_key, default_cache_dir
from webapp.export import build_workbook, export_filename
from webapp.jobs import Job, JobRegistry
from webapp.models import JobRequest
from webapp.nominatim import GeometryClient
from webapp.results import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ResultFilters,
    facets,
    filter_rows,
    paginate,
    sort_filtered,
)
from webapp.runner import run_job

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_TRACKED_JOBS = 20
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

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


@app.post("/api/jobs")
async def create_job(
    request: JobRequest = Body(),
    registry: JobRegistry = Depends(get_registry),
    cache: ResultCache = Depends(get_cache),
    overpass_factory: Callable[[], OverpassClient] = Depends(get_overpass_factory),
) -> dict:
    registry.prune(MAX_TRACKED_JOBS)
    cached = cache.load(cache_key(request.area.cache_payload(), request.categories)) is not None

    job = registry.create(request.area.display_label())
    registry.start(
        job,
        lambda running: run_job(running, request, cache=cache, client_factory=overpass_factory),
    )
    return {"job_id": job.job_id, "cached": cached}


def _require_job(registry: JobRegistry, job_id: str) -> Job:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Taj posao ne postoji ili je istekao.")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, registry: JobRegistry = Depends(get_registry)) -> dict:
    return _require_job(registry, job_id).to_status_dict()


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str, registry: JobRegistry = Depends(get_registry)) -> dict:
    job = _require_job(registry, job_id)
    registry.cancel(job.job_id)
    return job.to_status_dict()


def _filters(
    categories: list[str] = Query(default=[]),
    require_contact: bool = Query(default=False),
    q: str = Query(default=""),
    sort: str = Query(default="name"),
    order: str = Query(default="asc"),
) -> ResultFilters:
    return ResultFilters(
        categories=categories, require_contact=require_contact, q=q, sort=sort, order=order,
    )


def _finished_job(registry: JobRegistry, job_id: str) -> Job:
    job = _require_job(registry, job_id)
    if job.status != "done":
        raise HTTPException(status_code=409, detail="Posao jos nije zavrsen.")
    return job


@app.get("/api/jobs/{job_id}/results")
def job_results(
    job_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    filters: ResultFilters = Depends(_filters),
    registry: JobRegistry = Depends(get_registry),
) -> dict:
    job = _finished_job(registry, job_id)
    kept = sort_filtered(filter_rows(job.rows, filters), filters.sort, filters.order)
    window, total = paginate(kept, page, page_size)
    return {
        "rows": [row.as_output_dict() for row in window],
        "total": total,
        "page": page,
        "page_size": page_size,
        "facets": facets(job.rows, filters),
        "area_label": job.area_label,
    }


@app.get("/api/jobs/{job_id}/export.xlsx")
def job_export(
    job_id: str,
    filters: ResultFilters = Depends(_filters),
    registry: JobRegistry = Depends(get_registry),
) -> StreamingResponse:
    job = _finished_job(registry, job_id)
    kept = sort_filtered(filter_rows(job.rows, filters), filters.sort, filters.order)
    buffer = build_workbook(kept, area_label=job.area_label, filters=filters)
    filename = export_filename(job.area_label)
    return StreamingResponse(
        buffer,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
