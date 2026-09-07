"""Disk cache for finished job results.

A cache miss costs minutes of Overpass time, so the entries are deliberately
forgiving: anything unreadable, stale or written by another schema version is
treated as a miss rather than an error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from osm_businesses import COLUMNS, Row

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
MAX_AGE_DAYS = 30


def default_cache_dir() -> Path:
    """Where finished results live between runs.

    `OSM_CACHE_DIR` wins when set: a hosted container is not guaranteed a
    writable home directory, and on Render only the service's own disk is
    durable. Where `Path.home()` cannot be resolved at all, the system temp
    directory is better than refusing to start.
    """
    override = os.environ.get("OSM_CACHE_DIR")
    if override:
        return Path(override)
    try:
        home = Path.home()
    except RuntimeError:
        return Path(tempfile.gettempdir()) / "osm_businesses" / "web"
    return home / ".cache" / "osm_businesses" / "web"


def cache_key(area_payload: dict[str, Any], categories: Sequence[str]) -> str:
    """Stable short hash of what actually determines the result."""
    canonical = json.dumps(
        {"area": area_payload, "categories": sorted(categories)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CachedResult:
    area_label: str
    categories: list[str]
    elements_found: int
    rows: list[Row]
    created_at: str = field(default_factory=_now)


def _row_to_dict(row: Row) -> dict[str, Any]:
    data = row.as_output_dict()
    data["category_key"] = row.category_key
    return data


def _row_from_dict(data: dict[str, Any]) -> Row:
    values: dict[str, Any] = {column: data.get(column, "") for column in COLUMNS}
    values["osm_id"] = int(values["osm_id"])
    values["lat"] = float(values["lat"])
    values["lon"] = float(values["lon"])
    return Row(**values, category_key=str(data.get("category_key", "")))


class ResultCache:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> CachedResult | None:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return None
        created_at = str(payload.get("created_at", ""))
        if self._is_stale(created_at):
            return None
        try:
            rows = [_row_from_dict(item) for item in payload.get("rows", [])]
        except (TypeError, ValueError, KeyError):
            return None
        return CachedResult(
            area_label=str(payload.get("area_label", "")),
            categories=list(payload.get("categories", [])),
            elements_found=int(payload.get("elements_found", 0)),
            rows=rows,
            created_at=created_at,
        )

    @staticmethod
    def _is_stale(created_at: str) -> bool:
        try:
            written = datetime.fromisoformat(created_at)
        except ValueError:
            return True
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - written > timedelta(days=MAX_AGE_DAYS)

    def save(self, key: str, result: CachedResult) -> None:
        """Write an entry, or give up quietly.

        By the time this runs the rows are already in the job and on their way
        to the browser, so a read-only or full disk must not turn a finished
        scrape into an error. A miss next time costs minutes; a failed job
        costs the user the whole run.
        """
        payload = {
            "version": CACHE_VERSION,
            "created_at": result.created_at,
            "area_label": result.area_label,
            "categories": result.categories,
            "elements_found": result.elements_found,
            "rows": [_row_to_dict(row) for row in result.rows],
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self._path(key).with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._path(key))
        except OSError:
            logger.warning("could not cache results under %s", self.directory, exc_info=True)
