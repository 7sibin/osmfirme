"""Reading the websites of a job's rows, as a cancellable background pass.

The opposite shape to `webapp.enrich_runner`. That one is serial because every
search engine rate-limits a burst; this one talks to a different host per
business, so there is nobody to be rude to by going wide. Eight at a time turns
what would be twenty minutes of waiting on other people's servers into two.

Politeness is per host, and one host gets at most `MAX_PAGES` requests from a
whole pass - a homepage and a couple of `Kontakt` links, with an identifying
User-Agent. Businesses sharing a website (a chain, or a franchise's landing
page) are read once and the answer reused, because the cache is keyed by the
site rather than by the business.

Partial failure is normal and never fails the pass: an unreachable host is one
row's answer, not the run's.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from osm_businesses import Row

from webapp.contacts import (
    CONTACT_DEAD,
    CONTACT_OK,
    ContactHit,
    ContactScraper,
    pending_contacts,
    site_of,
)
from webapp.site_cache import ContactCache

logger = logging.getLogger(__name__)

#: Sites read at once. Eight is well short of anything a home connection
#: notices, and every one of them is a different host.
WORKERS = 8

#: How many businesses one pass will read for. Higher than the website search's
#: cap because this costs a fraction of a second each rather than two seconds:
#: 500 sites is a couple of minutes.
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000


@dataclass
class ContactProgress:
    """What the browser polls while a pass runs."""

    status: str = "idle"
    """"idle", "running", "done", "cancelled" or "error"."""
    checked: int = 0
    total: int = 0
    emails: int = 0
    """Addresses found - the column this pass exists for."""
    phones: int = 0
    """Numbers found for businesses OSM had none for."""
    dead: int = 0
    """Sites that answered nothing. A business whose site is gone is a lead again."""
    remaining: int = 0
    """Still unread of the rows this pass was handed, i.e. within its filters."""
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checked": self.checked,
            "total": self.total,
            "emails": self.emails,
            "phones": self.phones,
            "dead": self.dead,
            "remaining": self.remaining,
            "message": self.message,
        }


@dataclass
class ContactState:
    """A job's accumulated contact findings, keyed by OSM id."""

    hits: dict[int, ContactHit] = field(default_factory=dict)
    progress: ContactProgress = field(default_factory=ContactProgress)


def build_scraper(session, user_agent: str) -> ContactScraper:
    from webapp.contacts import http_fetch

    return ContactScraper(fetch=http_fetch(session, user_agent))


async def run_contacts(
    rows: list[Row],
    state: ContactState,
    *,
    cache: ContactCache,
    scraper_factory: Callable[[], ContactScraper],
    limit: int = DEFAULT_LIMIT,
    workers: int = WORKERS,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Read the sites of up to `limit` of `rows`, recording into `state`."""
    queue = pending_contacts(rows, state.hits)
    progress = state.progress
    progress.status = "running"
    progress.total = min(len(queue), limit)
    progress.checked = 0
    progress.emails = 0
    progress.phones = 0
    progress.dead = 0
    progress.remaining = max(0, len(queue) - progress.total)
    progress.message = "Citam sajtove."

    if not queue:
        progress.status = "done"
        progress.message = "Nema sajta za citanje."
        return

    batch = queue[:limit]
    scraper = scraper_factory()
    loop = asyncio.get_running_loop()

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            # Chunked rather than all at once so cancelling lands within a
            # chunk instead of after every site has been fetched.
            for start in range(0, len(batch), workers):
                if is_cancelled():
                    progress.status = "cancelled"
                    progress.message = "Otkazano."
                    return
                chunk = batch[start : start + workers]
                results = await asyncio.gather(
                    *(loop.run_in_executor(pool, _read_one, scraper, cache, row) for row in chunk)
                )
                for row, hit in zip(chunk, results):
                    _record(state, progress, row, hit)
    except Exception as exc:  # noqa: BLE001 - a broken pass keeps what it read
        logger.exception("contact pass failed")
        progress.status = "error"
        progress.message = str(exc) or "Citanje sajtova je puklo."
        return
    finally:
        progress.remaining = len(pending_contacts(rows, state.hits))
        cache.flush()

    progress.status = "done"
    progress.message = _summary(progress)


def _read_one(scraper: ContactScraper, cache: ContactCache, row: Row) -> ContactHit:
    """One business's site, from the cache when another row already read it.

    The cache is consulted inside the worker rather than before dispatch so that
    a chain whose branches share a site still only fetches once - the first
    branch through writes the entry the rest of them read.
    """
    url = site_of(row)
    cached = cache.get(url)
    if cached is not None:
        return cached
    hit = scraper.read(row)
    cache.put(url, hit)
    return hit


def _record(state: ContactState, progress: ContactProgress, row: Row, hit: ContactHit) -> None:
    progress.checked += 1
    state.hits[row.osm_id] = hit
    if hit.status == CONTACT_DEAD:
        progress.dead += 1
        return
    if hit.status != CONTACT_OK:
        return
    if hit.email and not row.email:
        progress.emails += 1
    if hit.phone and not row.phone:
        progress.phones += 1


def _summary(progress: ContactProgress) -> str:
    """What the pass did. What is left is the band's line, not this one."""
    parts = [f"Procitano {progress.checked}."]
    if progress.emails:
        parts.append(f"Nadjeno {progress.emails} email adresa.")
    if progress.phones:
        parts.append(f"{progress.phones} novih telefona.")
    if progress.dead:
        parts.append(f"{progress.dead} sajtova ne radi.")
    if not (progress.emails or progress.phones or progress.dead):
        parts.append("Nista novo.")
    return " ".join(parts)
