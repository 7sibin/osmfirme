"""Disk cache for what the website search found, kept apart from the Overpass cache.

Two caches because the two answers age at different rates and for different
reasons. A map extract is good for a month; whether a business has a website is
a fact about the business, not the map, and re-running an area must not force a
re-search of every shop in it - nor the other way round.

Keyed by the business rather than by the area, so the same shop found again in
an overlapping area costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from osm_businesses import Row, fold_text

from webapp.enrich import SiteHit
from webapp.results import FOUND_NONE

logger = logging.getLogger(__name__)

CACHE_VERSION = 1

#: A site that was found stays found: domains do not evaporate, and the alive
#: check runs again the next time the row is looked at anyway.
FOUND_MAX_AGE_DAYS = 60

#: "No site" is the answer that goes stale. Small businesses put up a site every
#: day, so a miss is only worth trusting for a few weeks.
MISS_MAX_AGE_DAYS = 21

#: One JSON file per shard rather than per business: a city's worth of misses is
#: thousands of entries, and thousands of tiny files is slow to walk on Windows
#: and unpleasant to inspect.
SHARDS = 256


def default_site_cache_dir() -> Path:
    override = os.environ.get("OSM_SITE_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("OSM_CACHE_DIR")
    if base:
        return Path(base) / "sites"
    try:
        home = Path.home()
    except RuntimeError:
        return Path(tempfile.gettempdir()) / "osm_businesses" / "sites"
    return home / ".cache" / "osm_businesses" / "sites"


def site_key(row: Row) -> str:
    """What identifies a business for the purposes of "does it have a site".

    Name, trade and town. The street is left out on purpose: a shop that moved
    across town has the same website, and its OSM id changes when it is remapped
    anyway, so neither would be a stable key.
    """
    canonical = "|".join((fold_text(row.name), row.category, fold_text(row.place)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SiteCache:
    """Sharded JSON store of SiteHits, forgiving of anything it cannot read."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._shards: dict[str, dict[str, Any]] = {}

    def _shard_path(self, key: str) -> Path:
        return self.directory / f"{int(key[:2], 16) % SHARDS:03d}.json"

    def _shard(self, key: str) -> dict[str, Any]:
        path = self._shard_path(key)
        name = path.name
        if name not in self._shards:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
                payload = {"version": CACHE_VERSION, "entries": {}}
            payload.setdefault("entries", {})
            self._shards[name] = payload
        return self._shards[name]

    def get(self, row: Row) -> SiteHit | None:
        key = site_key(row)
        entry = self._shard(key)["entries"].get(key)
        if not isinstance(entry, dict):
            return None
        try:
            hit = SiteHit.from_dict(entry)
        except (TypeError, ValueError):
            return None
        if not hit.confidence or self._is_stale(hit):
            return None
        return hit

    @staticmethod
    def _is_stale(hit: SiteHit) -> bool:
        try:
            written = datetime.fromisoformat(hit.checked_at)
        except ValueError:
            return True
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        days = MISS_MAX_AGE_DAYS if hit.confidence == FOUND_NONE else FOUND_MAX_AGE_DAYS
        return _now() - written > timedelta(days=days)

    def put(self, row: Row, hit: SiteHit) -> None:
        """Remember one finding. A hit with no confidence is a failed search, so it is dropped."""
        if not hit.confidence:
            return
        key = site_key(row)
        self._shard(key)["entries"][key] = hit.to_dict()

    def flush(self) -> None:
        """Write the touched shards, or give up quietly.

        Same bargain as the result cache: the findings are already in the job
        and on their way to the browser, so a read-only disk must not turn a
        finished run into an error.
        """
        for name, payload in self._shards.items():
            path = self.directory / name
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            except OSError:
                logger.warning("could not cache site hits under %s", self.directory, exc_info=True)
                return
