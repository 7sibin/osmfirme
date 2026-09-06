"""What a job actually does: check the cache, query Overpass, parse, cache.

The Overpass client is injected rather than constructed here, so tests can
hand in a fake and never touch the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from osm_businesses import (
    OverpassClient,
    OverpassError,
    build_query,
    parse_elements,
    sort_rows,
)

from webapp.cache import CachedResult, ResultCache, cache_key
from webapp.jobs import Job
from webapp.models import JobRequest

PHASE_MESSAGES = {
    "querying": "Saljem upit Overpass API-ju. Za veci grad ovo zna da traje nekoliko minuta.",
    "parsing": "Obradjujem rezultate.",
    "cached": "Ucitano iz kesa.",
}

BUSY_MARKERS = ("429", "504", "503", "busy", "overloaded", "timeout")


def friendly_overpass_error(exc: OverpassError) -> str:
    text = str(exc).lower()
    if any(marker in text for marker in BUSY_MARKERS):
        return "Overpass je trenutno zauzet. Pokusaj ponovo za koji minut."
    return "Overpass nije odgovorio. Proveri internet vezu pa pokusaj ponovo."


async def run_job(
    job: Job,
    request: JobRequest,
    *,
    cache: ResultCache,
    client_factory: Callable[[], OverpassClient],
) -> None:
    key = cache_key(request.area.cache_payload(), request.categories)

    cached = cache.load(key)
    if cached is not None:
        job.elements_found = cached.elements_found
        job.rows = cached.rows
        job.set_phase("parsing", PHASE_MESSAGES["cached"])
        return

    if job.is_cancelled():
        return

    job.set_phase("querying", PHASE_MESSAGES["querying"])
    query = build_query(request.area.to_spec(), keys=request.ordered_categories())
    client = client_factory()
    try:
        elements = await asyncio.to_thread(client.fetch, query)
    except OverpassError as exc:
        raise RuntimeError(friendly_overpass_error(exc)) from exc

    if job.is_cancelled():
        return

    job.set_phase("parsing", PHASE_MESSAGES["parsing"])
    job.elements_found = len(elements)
    job.rows = sort_rows(parse_elements(elements))

    cache.save(
        key,
        CachedResult(
            area_label=request.area.display_label(),
            categories=list(request.categories),
            elements_found=job.elements_found,
            rows=job.rows,
        ),
    )
