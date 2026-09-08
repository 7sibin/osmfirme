"""FastAPI application: JSON API plus the static frontend.

Every outbound client is provided through a dependency so tests can override
it and never touch the network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import requests
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from geo import NominatimClient, RateLimiter
from osm_businesses import USER_AGENT, OverpassClient, Row

from webapp.cache import ResultCache, cache_key, default_cache_dir
from webapp.enrich import WebsiteFinder, apply_site_hits
from webapp.enrich_runner import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    build_finder,
    pending,
    run_enrichment,
)
from webapp.export import build_workbook, export_filename
from webapp.jobs import Job, JobRegistry
from webapp.models import JobRequest
from webapp.nominatim import GeometryClient
from webapp.results import (
    DEFAULT_PAGE_SIZE,
    FOUND_STRONG,
    FOUND_WEAK,
    MAX_PAGE_SIZE,
    ResultFilters,
    drop_found,
    facets,
    filter_rows,
    paginate,
    prepare_rows,
    sort_filtered,
)
from webapp.runner import run_job
from webapp.site_cache import SiteCache, default_site_cache_dir

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_TRACKED_JOBS = 20
MAX_POINTS = 5000
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


@lru_cache(maxsize=1)
def _site_cache() -> SiteCache:
    return SiteCache(default_site_cache_dir())


def get_site_cache() -> SiteCache:
    return _site_cache()


def get_finder_factory() -> Callable[[], WebsiteFinder]:
    """Built per pass rather than once: the searcher carries its own rate limiter."""
    return lambda: build_finder(_session(), USER_AGENT)


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
    website: Literal["any", "yes", "no"] = Query(default="any"),
    q: str = Query(default=""),
    sort: str = Query(default="name"),
    order: str = Query(default="asc"),
    commercial_only: bool = Query(default=True),
    collapse: bool = Query(default=True),
    hide_found: bool = Query(default=False),
) -> ResultFilters:
    return ResultFilters(
        categories=categories,
        require_contact=require_contact,
        website=website,
        q=q,
        sort=sort,
        order=order,
        commercial_only=commercial_only,
        collapse=collapse,
        hide_found=hide_found,
    )


def _working_set(job: Job, filters: ResultFilters) -> tuple[list[Row], list[Row]]:
    """The rows this request is about, as (everything on the table, what is shown).

    Two lists because the page reports both. The headline counts describe the
    area - how many businesses, how many without a site, how many the search
    found one for - and must not move when the user ticks "hide the ones we
    found". Only the table below it moves.
    """
    overlaid = apply_site_hits(job.rows, job.enrich.hits)
    base = prepare_rows(overlaid, replace(filters, hide_found=False))
    shown = drop_found(base) if filters.hide_found else base
    return base, shown


def _counts(base: list[Row], matching: list[Row]) -> dict[str, int]:
    """Headline numbers for the area, plus the one number that follows the filters.

    `unchecked_here` is what the website search would take on right now, and it
    is the only count that moves with the filter bar - the button has to be able
    to say how many it will actually check.
    """
    return {
        "area_total": len(base),
        "area_without_site": sum(1 for row in base if not row.website),
        "found_sites": sum(1 for row in base if row.found_confidence == FOUND_STRONG),
        "maybe_sites": sum(1 for row in base if row.found_confidence == FOUND_WEAK),
        "unchecked": len(pending(base, {})),
        "unchecked_here": len(pending(matching, {})),
    }


def _row_payload(row: Row) -> dict:
    """The CSV columns plus what the website search added, kept clearly apart."""
    return {
        **row.as_output_dict(),
        "found_website": row.found_website,
        "found_confidence": row.found_confidence,
        "found_source": row.found_source,
    }


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
    base, shown = _working_set(job, filters)
    # `matching` feeds both the table and `unchecked_here`. Filtering `shown`
    # rather than `base` costs nothing extra: the rows `hide_found` removes are
    # by definition already checked, so neither count can see them anyway.
    matching = filter_rows(shown, filters)
    kept = sort_filtered(matching, filters.sort, filters.order)
    window, total = paginate(kept, page, page_size)
    return {
        "rows": [_row_payload(row) for row in window],
        "total": total,
        "page": page,
        "page_size": page_size,
        "facets": facets(shown, filters),
        "area_label": job.area_label,
        "export_filename": export_filename(job.area_label),
        # The area as a whole, unaffected by the table's own filters: the page
        # headline reports what the area holds and what the default view hides,
        # while `total` reports what the table is actually showing.
        "counts": _counts(base, matching),
        "enrich": job.enrich.progress.to_dict(),
    }


@app.get("/api/jobs/{job_id}/points")
def job_points(
    job_id: str,
    filters: ResultFilters = Depends(_filters),
    registry: JobRegistry = Depends(get_registry),
) -> dict:
    """Coordinates of every filtered row, so the map can show what the table lists.

    Capped: a city-sized area is thousands of businesses, and past a few
    thousand dots the canvas costs more than the picture is worth.
    """
    job = _finished_job(registry, job_id)
    _, shown = _working_set(job, filters)
    kept = filter_rows(shown, filters)
    return {
        "points": [[round(row.lat, 6), round(row.lon, 6)] for row in kept[:MAX_POINTS]],
        "total": len(kept),
        "truncated": len(kept) > MAX_POINTS,
    }


@app.get("/api/jobs/{job_id}/export.xlsx")
def job_export(
    job_id: str,
    filters: ResultFilters = Depends(_filters),
    registry: JobRegistry = Depends(get_registry),
) -> StreamingResponse:
    job = _finished_job(registry, job_id)
    _, shown = _working_set(job, filters)
    kept = sort_filtered(filter_rows(shown, filters), filters.sort, filters.order)
    buffer = build_workbook(kept, area_label=job.area_label, filters=filters)
    filename = export_filename(job.area_label)
    return StreamingResponse(
        buffer,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/jobs/{job_id}/enrich")
async def start_enrichment(
    job_id: str,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    filters: ResultFilters = Depends(_filters),
    registry: JobRegistry = Depends(get_registry),
    sites: SiteCache = Depends(get_site_cache),
    finder_factory: Callable[[], WebsiteFinder] = Depends(get_finder_factory),
) -> dict:
    """Search the web for the sites of businesses the map has none for.

    Runs over the prepared rows the panel's filters leave, not over everything.
    Two reasons, both about the clock: at two seconds a business a city is about
    an hour, and the queue is otherwise ordered by `sort_rows` - alphabetically
    by category - so a first pass spends itself on artwork and bus stations
    while every restaurant in the area waits for the seventh.

    Filtering costs nothing in total work, because the site cache is keyed by
    the business rather than by the job: a shop checked while the table was
    narrowed to bakeries stays checked once the filter comes off.
    """
    job = _finished_job(registry, job_id)
    if job.enrich.progress.status == "running":
        raise HTTPException(status_code=409, detail="Provera sajtova je vec u toku.")

    base, _ = _working_set(job, filters)
    queue = filter_rows(base, filters)
    job.enrich_cancel.clear()
    registry.start_side_task(
        job,
        run_enrichment(
            queue,
            job.enrich,
            cache=sites,
            finder_factory=finder_factory,
            limit=limit,
            is_cancelled=job.enrich_cancel.is_set,
        ),
    )
    return job.enrich.progress.to_dict()


@app.get("/api/jobs/{job_id}/enrich")
def enrichment_status(job_id: str, registry: JobRegistry = Depends(get_registry)) -> dict:
    return _require_job(registry, job_id).enrich.progress.to_dict()


@app.delete("/api/jobs/{job_id}/enrich")
def cancel_enrichment(job_id: str, registry: JobRegistry = Depends(get_registry)) -> dict:
    job = _require_job(registry, job_id)
    job.enrich_cancel.set()
    return job.enrich.progress.to_dict()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


class RevalidatingStaticFiles(StaticFiles):
    """Static files that the browser must revalidate instead of guessing.

    Without this Chrome heuristically caches app.js, and an edit to the frontend
    does not show up until a hard reload. `no-cache` still allows a 304, so an
    unchanged file costs one conditional request, not a re-download.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")
