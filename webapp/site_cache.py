"""Disk caches for what the enrichment passes learn, kept apart from Overpass's.

Separate from the result cache because the answers age at different rates and
for different reasons. A map extract is good for a month; whether a business has
a website, or what address is printed on it, is a fact about the business rather
than about the map, and re-running an area must not force a re-search of every
shop in it - nor the other way round.

Both caches here are keyed by the thing that was looked up rather than by the
job or the area, so the same shop found again in an overlapping area, or two
branches of a chain sharing one website, cost nothing the second time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar
from urllib.parse import urlsplit

from osm_businesses import Row, fold_text, normalize_website

from webapp.contacts import CONTACT_OK, ContactHit
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

#: One JSON file per shard rather than per entry: a city's worth of misses is
#: thousands of them, and thousands of tiny files is slow to walk on Windows and
#: unpleasant to inspect.
SHARDS = 256

T = TypeVar("T")


def _cache_root() -> Path:
    base = os.environ.get("OSM_CACHE_DIR")
    if base:
        return Path(base)
    try:
        return Path.home() / ".cache" / "osm_businesses"
    except RuntimeError:
        return Path(tempfile.gettempdir()) / "osm_businesses"


def default_site_cache_dir() -> Path:
    override = os.environ.get("OSM_SITE_CACHE_DIR")
    return Path(override) if override else _cache_root() / "sites"


def default_contact_cache_dir() -> Path:
    override = os.environ.get("OSM_CONTACT_CACHE_DIR")
    return Path(override) if override else _cache_root() / "contacts"


def site_key(row: Row) -> str:
    """What identifies a business for the purposes of "does it have a site".

    Name, trade and town. The street is left out on purpose: a shop that moved
    across town has the same website, and its OSM id changes when it is remapped
    anyway, so neither would be a stable key.
    """
    canonical = "|".join((fold_text(row.name), row.category, fold_text(row.place)))
    return _hash(canonical)


def contact_key(url: str) -> str:
    """What identifies a *page* worth reading: the host, ignoring `www` and scheme.

    Keyed by the site rather than by the business, because that is where the
    details come from - so the fifteen branches of a chain that all point at one
    website cost exactly one fetch.
    """
    target = normalize_website(url)
    host = urlsplit(target).netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")
    return _hash(host) if host else ""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ShardedStore(Generic[T]):
    """Sharded JSON store of dataclass-ish entries, forgiving of anything unreadable.

    Every failure mode here - a missing directory, a half-written shard, a
    version bump, an entry with a date nobody can parse - reads as a miss. A
    miss costs one lookup; an exception would cost the whole pass.
    """

    def __init__(
        self,
        directory: Path,
        *,
        load: Callable[[dict[str, Any]], T],
        dump: Callable[[T], dict[str, Any]],
        age_of: Callable[[T], tuple[str, int]],
    ) -> None:
        self.directory = Path(directory)
        self._load = load
        self._dump = dump
        self._age_of = age_of
        self._shards: dict[str, dict[str, Any]] = {}

    def _shard(self, key: str) -> dict[str, Any]:
        name = f"{int(key[:2], 16) % SHARDS:03d}.json"
        if name not in self._shards:
            try:
                payload = json.loads((self.directory / name).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
                payload = {"version": CACHE_VERSION, "entries": {}}
            payload.setdefault("entries", {})
            self._shards[name] = payload
        return self._shards[name]

    def get(self, key: str) -> T | None:
        if not key:
            return None
        entry = self._shard(key)["entries"].get(key)
        if not isinstance(entry, dict):
            return None
        try:
            value = self._load(entry)
        except (TypeError, ValueError):
            return None
        checked_at, max_age_days = self._age_of(value)
        return None if self._is_stale(checked_at, max_age_days) else value

    def put(self, key: str, value: T) -> None:
        if key:
            self._shard(key)["entries"][key] = self._dump(value)

    @staticmethod
    def _is_stale(checked_at: str, max_age_days: int) -> bool:
        try:
            written = datetime.fromisoformat(checked_at)
        except ValueError:
            return True
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return _now() - written > timedelta(days=max_age_days)

    def flush(self) -> None:
        """Write the touched shards, or give up quietly.

        Same bargain as the result cache: the findings are already in the job
        and on their way to the browser, so a read-only disk must not turn a
        finished pass into an error.
        """
        for name, payload in self._shards.items():
            path = self.directory / name
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            except OSError:
                logger.warning("could not write cache under %s", self.directory, exc_info=True)
                return


class SiteCache:
    """What the website search concluded about a business, keyed by the business."""

    def __init__(self, directory: Path) -> None:
        self._store: ShardedStore[SiteHit] = ShardedStore(
            directory,
            load=SiteHit.from_dict,
            dump=lambda hit: hit.to_dict(),
            age_of=lambda hit: (
                hit.checked_at,
                MISS_MAX_AGE_DAYS if hit.confidence == FOUND_NONE else FOUND_MAX_AGE_DAYS,
            ),
        )

    @property
    def directory(self) -> Path:
        return self._store.directory

    def get(self, row: Row) -> SiteHit | None:
        hit = self._store.get(site_key(row))
        # An entry with no confidence is a search that failed, which says nothing
        # about the business. `put` refuses to store one; this is belt and braces.
        return None if hit is not None and not hit.confidence else hit

    def put(self, row: Row, hit: SiteHit) -> None:
        """Remember one finding. A hit with no confidence is a failed search, so it is dropped."""
        if hit.confidence:
            self._store.put(site_key(row), hit)

    def flush(self) -> None:
        self._store.flush()


class ContactCache:
    """What a website carried, keyed by the website rather than by the business."""

    def __init__(self, directory: Path) -> None:
        self._store: ShardedStore[ContactHit] = ShardedStore(
            directory,
            load=ContactHit.from_dict,
            dump=lambda hit: hit.to_dict(),
            age_of=lambda hit: (
                hit.checked_at,
                # A published address outlives a redesign; "nothing here" and "no
                # answer" are both worth asking about again before long.
                FOUND_MAX_AGE_DAYS if hit.status == CONTACT_OK else MISS_MAX_AGE_DAYS,
            ),
        )

    @property
    def directory(self) -> Path:
        return self._store.directory

    def get(self, url: str) -> ContactHit | None:
        hit = self._store.get(contact_key(url))
        return None if hit is not None and not hit.status else hit

    def put(self, url: str, hit: ContactHit) -> None:
        if hit.status:
            self._store.put(contact_key(url), hit)

    def flush(self) -> None:
        self._store.flush()
