"""Running the website search over a job's rows, as a cancellable background pass.

Deliberately serial. Every search engine worth asking rate-limits a burst and
answers a trickle indefinitely, so the wall-clock cost is set by
`SEARCH_INTERVAL_S` and there is nothing to gain from concurrency - only a
blocked run. What keeps it bearable instead is doing less work:

- rows that already carry a website are skipped
- rows whose name identifies nothing ("Pekara") are skipped, since any result
  would belong to a different bakery
- rows checked before come back from the site cache for free
- a run is capped, and the next run picks up where it left off
- the caller hands over only the rows the panel's filters left, so a pass spends
  itself on the trade the user is actually working through

That last one costs nothing in total work: the cache is keyed by the business,
so a shop checked while the table was narrowed stays checked afterwards.

Partial failure is normal and never fails the pass: one unreachable engine or
one dead domain is one row's answer, not the run's.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

from geo import RateLimiter
from osm_businesses import Row

from webapp.enrich import SiteHit, WebsiteFinder, needs_check
from webapp.results import FOUND_STRONG, FOUND_WEAK
from webapp.site_cache import SiteCache

logger = logging.getLogger(__name__)

#: How many businesses one pass will search for. At SEARCH_INTERVAL_S apiece a
#: cap of 150 is about six minutes - long enough to be worth starting, short
#: enough that the user is not staring at a bar for half an hour. Running again
#: continues from where this stopped, and cached rows do not count against it.
DEFAULT_LIMIT = 150
MAX_LIMIT = 1000


@dataclass
class EnrichProgress:
    """What the browser polls while a pass runs."""

    status: str = "idle"
    """"idle", "running", "done", "cancelled" or "error"."""
    checked: int = 0
    """Businesses this pass has an answer for, cached ones included."""
    total: int = 0
    """How many it set out to answer."""
    found: int = 0
    """Sites found with confidence, i.e. leads the user can now skip."""
    maybe: int = 0
    """Weak hits: worth a look, not worth acting on unseen."""
    failed: int = 0
    """Searches that could not be run. These stay unchecked and retry next pass."""
    remaining: int = 0
    """Still unchecked of the rows this pass was handed - i.e. within the filters
    that started it, not across the area. The page reports both scopes itself."""
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checked": self.checked,
            "total": self.total,
            "found": self.found,
            "maybe": self.maybe,
            "failed": self.failed,
            "remaining": self.remaining,
            "message": self.message,
        }


@dataclass
class EnrichState:
    """A job's accumulated website findings, keyed by OSM id."""

    hits: dict[int, SiteHit] = field(default_factory=dict)
    progress: EnrichProgress = field(default_factory=EnrichProgress)


def build_finder(session, user_agent: str) -> WebsiteFinder:
    from webapp.enrich import SEARCH_INTERVAL_S, ddg_search, http_alive

    return WebsiteFinder(
        search=ddg_search,
        alive=http_alive(session, user_agent),
        rate_limiter=RateLimiter(min_interval=SEARCH_INTERVAL_S),
    )


def pending(rows: list[Row], hits: dict[int, SiteHit]) -> list[Row]:
    """Rows a pass could still learn something about."""
    return [row for row in rows if row.osm_id not in hits and needs_check(row)]


async def run_enrichment(
    rows: list[Row],
    state: EnrichState,
    *,
    cache: SiteCache,
    finder_factory: Callable[[], WebsiteFinder],
    limit: int = DEFAULT_LIMIT,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Search for the websites of up to `limit` of `rows`, recording into `state`.

    Runs the blocking search off the event loop one row at a time, so the page
    keeps polling and cancelling stays responsive.
    """
    queue = pending(rows, state.hits)
    progress = state.progress
    progress.status = "running"
    progress.total = min(len(queue), limit)
    progress.checked = 0
    progress.found = 0
    progress.maybe = 0
    progress.failed = 0
    progress.remaining = max(0, len(queue) - progress.total)
    progress.message = "Trazim sajtove."

    if not queue:
        progress.status = "done"
        progress.message = "Nema sta da se proveri."
        return

    finder: WebsiteFinder | None = None
    try:
        for row in queue[:limit]:
            if is_cancelled():
                progress.status = "cancelled"
                progress.message = "Otkazano."
                return

            hit = cache.get(row)
            if hit is None:
                if finder is None:
                    finder = finder_factory()
                hit = await asyncio.to_thread(finder.find, row)
                cache.put(row, hit)

            progress.checked += 1
            if not hit.confidence:
                # Unchecked: the search could not be run. Left out of `hits` so
                # the next pass retries it instead of writing the business off.
                progress.failed += 1
                continue

            state.hits[row.osm_id] = hit
            if hit.confidence == FOUND_STRONG:
                progress.found += 1
            elif hit.confidence == FOUND_WEAK:
                progress.maybe += 1
    except Exception as exc:  # noqa: BLE001 - a broken pass keeps what it found
        logger.exception("website search pass failed")
        progress.status = "error"
        progress.message = str(exc) or "Pretraga sajtova je pukla."
        return
    finally:
        progress.remaining = len(pending(rows, state.hits))
        cache.flush()

    progress.status = "done"
    progress.message = _summary(progress)


def _summary(progress: EnrichProgress) -> str:
    """What the pass did. What is *left* is the band's line, not this one.

    `remaining` counts what is left of the rows this pass was handed, which is
    now whatever the filters left. The page knows both that number and the
    area's, and says them together; repeating one here would read as a third,
    different total.
    """
    parts = [f"Provereno {progress.checked}."]
    if progress.found:
        parts.append(f"Nadjeno {progress.found} sajtova.")
    if progress.maybe:
        parts.append(f"{progress.maybe} za proveru.")
    if progress.failed:
        parts.append(f"{progress.failed} nije uspelo - probaj ponovo.")
    return " ".join(parts)
