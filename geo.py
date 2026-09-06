"""Area resolution for osm_businesses.

Turns a human place name into an Overpass area id by way of Nominatim, because
the `name` tag in OSM is frequently not the string the user typed (Serbian
places, for instance, usually carry Cyrillic as the primary name). Nominatim
indexes name variants; matching a Latin string against Overpass does not.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, TextIO

NOMINATIM_URL = "https://nominatim.openstreetmap.org"
NOMINATIM_MIN_INTERVAL = 1.0
"""Nominatim's usage policy: at most one request per second. Enforced in code."""

RELATION_AREA_OFFSET = 3_600_000_000
WAY_AREA_OFFSET = 2_400_000_000

#: Only these Nominatim classes can become an Overpass area.
AREA_CLASSES = frozenset({"boundary", "place"})

_ISO_LEVEL_RE = re.compile(r"^ISO3166-2-lvl(\d+)$", re.IGNORECASE)

DEFAULT_CACHE_PATH = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
) / "osm_businesses" / "areas.json"


class PlaceNotFoundError(Exception):
    """No usable Overpass area could be derived from the given place name."""


class ResponseLike(Protocol):
    status_code: int
    text: str

    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class SessionLike(Protocol):
    def get(self, url: str, **kwargs: Any) -> ResponseLike: ...


def to_area_id(osm_type: str, osm_id: int) -> int:
    """Convert an OSM object id into an Overpass area id.

    Relations are preferred; ways work but are rarely what you want for a
    settlement. Nodes have no area at all.
    """
    if osm_type == "relation":
        return RELATION_AREA_OFFSET + osm_id
    if osm_type == "way":
        return WAY_AREA_OFFSET + osm_id
    raise ValueError(f"osm_type {osm_type!r} cannot be converted to an Overpass area id")


@dataclass(frozen=True)
class AreaCandidate:
    """One Nominatim result that can be used as an Overpass area."""

    osm_type: str
    osm_id: int
    display_name: str
    category: str
    place_type: str
    admin_level: str

    @property
    def area_id(self) -> int:
        return to_area_id(self.osm_type, self.osm_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "osm_type": self.osm_type,
            "osm_id": self.osm_id,
            "display_name": self.display_name,
            "category": self.category,
            "place_type": self.place_type,
            "admin_level": self.admin_level,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AreaCandidate:
        return cls(
            osm_type=str(data["osm_type"]),
            osm_id=int(data["osm_id"]),
            display_name=str(data.get("display_name", "")),
            category=str(data.get("category", "")),
            place_type=str(data.get("place_type", "")),
            admin_level=str(data.get("admin_level", "")),
        )


@dataclass
class RateLimiter:
    """Blocks so that consecutive calls are at least `min_interval` apart.

    The clock and sleep are injectable so the limiter is testable without
    actually waiting.
    """

    min_interval: float = NOMINATIM_MIN_INTERVAL
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last: float | None = field(default=None, init=False, repr=False)

    def wait(self) -> None:
        now = self.monotonic()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.min_interval:
                self.sleep(self.min_interval - elapsed)
                now = self.monotonic()
        self._last = now


class NominatimClient:
    """Thin, polite Nominatim /search wrapper."""

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

    def search(self, name: str, country: str | None = None, limit: int = 10) -> list[AreaCandidate]:
        """Return the results that can actually become an Overpass area."""
        params: dict[str, Any] = {
            "q": name,
            "format": "jsonv2",
            "addressdetails": 1,
            "extratags": 1,
            "limit": limit,
        }
        if country:
            params["countrycodes"] = country.strip().lower()

        self.rate_limiter.wait()
        response = self.session.get(
            f"{self.base_url}/search",
            params=params,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise PlaceNotFoundError(f"Unexpected Nominatim response for {name!r}")

        candidates: list[AreaCandidate] = []
        for result in payload:
            if not isinstance(result, dict):
                continue
            if result.get("osm_type") != "relation":
                continue
            category = _category_of(result)
            if category not in AREA_CLASSES:
                continue
            candidates.append(
                AreaCandidate(
                    osm_type="relation",
                    osm_id=int(result["osm_id"]),
                    display_name=str(result.get("display_name", "")),
                    category=category,
                    place_type=str(result.get("type", "")),
                    admin_level=_admin_level(result),
                )
            )
        return candidates


def _category_of(result: dict[str, Any]) -> str:
    """format=jsonv2 calls it `category`; the older format=json calls it `class`."""
    for key in ("category", "class"):
        value = result.get(key)
        if value:
            return str(value)
    return ""


def _admin_level(result: dict[str, Any]) -> str:
    """Nominatim rarely returns admin_level outright; ISO3166-2-lvlN carries it."""
    for container in (result.get("extratags"), result.get("address")):
        if isinstance(container, dict) and container.get("admin_level") is not None:
            return str(container["admin_level"])
    address = result.get("address")
    if isinstance(address, dict):
        for key in address:
            match = _ISO_LEVEL_RE.match(str(key))
            if match:
                return match.group(1)
    return ""


def format_candidates(candidates: list[AreaCandidate]) -> str:
    """Render a numbered table of candidates, one per line, header first."""
    headers = ("#", "display_name", "type", "osm_id", "admin_level")
    rows: list[tuple[str, ...]] = [
        (
            str(index),
            candidate.display_name,
            candidate.place_type,
            str(candidate.osm_id),
            candidate.admin_level,
        )
        for index, candidate in enumerate(candidates, start=1)
    ]
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows)] if rows else [
        len(header) for header in headers
    ]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in [headers, *rows]]
    return "\n".join(lines)


class AreaCache:
    """name+country -> resolved area, persisted as a small JSON dict."""

    def __init__(self, path: Path | str = DEFAULT_CACHE_PATH) -> None:
        self.path = Path(path)

    @staticmethod
    def key(name: str, country: str | None = None) -> str:
        return f"{name.strip().lower()}|{(country or '').strip().lower()}"

    def _load(self) -> dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> AreaCandidate | None:
        entry = self._load().get(key)
        if not isinstance(entry, dict):
            return None
        try:
            return AreaCandidate.from_dict(entry)
        except (KeyError, TypeError, ValueError):
            return None

    def set(self, key: str, candidate: AreaCandidate) -> None:
        data = self._load()
        data[key] = candidate.to_dict()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        except OSError:
            pass  # a cache that cannot be written is not a reason to fail the run


def resolve_place(
    name: str,
    *,
    client: NominatimClient,
    country: str | None = None,
    pick: int | None = None,
    assume_yes: bool = False,
    cache: AreaCache | None = None,
    input_fn: Callable[[str], str] = input,
    stream: TextIO = sys.stderr,
) -> AreaCandidate:
    """Resolve a place name to a single Overpass area, asking the user if needed."""
    cache_key = AreaCache.key(name, country)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            print(f"Using cached area for {name!r}: {_describe(cached)}", file=stream)
            return cached

    candidates = client.search(name, country=country)
    if not candidates:
        raise PlaceNotFoundError(
            f"No OSM boundary or place relation found for {name!r}"
            + (f" in country {country!r}" if country else "")
            + ". Try a more specific name (e.g. 'Nis, Serbia'), --country, "
            "or fall back to --bbox S,W,N,E."
        )

    if len(candidates) > 1:
        print(f"{len(candidates)} candidate areas for {name!r}:", file=stream)
        print(format_candidates(candidates), file=stream)

    chosen = _choose(name, candidates, pick=pick, assume_yes=assume_yes, input_fn=input_fn, stream=stream)
    print(f"Using area for {name!r}: {_describe(chosen)}", file=stream)

    if cache is not None:
        cache.set(cache_key, chosen)
    return chosen


def _choose(
    name: str,
    candidates: list[AreaCandidate],
    *,
    pick: int | None,
    assume_yes: bool,
    input_fn: Callable[[str], str],
    stream: TextIO,
) -> AreaCandidate:
    if pick is not None:
        if not 1 <= pick <= len(candidates):
            raise PlaceNotFoundError(
                f"--pick {pick} is out of range: {len(candidates)} candidate(s) for {name!r}"
            )
        return candidates[pick - 1]
    if len(candidates) == 1 or assume_yes:
        return candidates[0]

    while True:
        try:
            answer = input_fn(f"Pick 1-{len(candidates)} [1]: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise PlaceNotFoundError(
                f"No area chosen for {name!r}; re-run with --pick N or --yes"
            ) from exc
        if not answer:
            return candidates[0]
        try:
            index = int(answer)
        except ValueError:
            print(f"Not a number: {answer!r}", file=stream)
            continue
        if 1 <= index <= len(candidates):
            return candidates[index - 1]
        print(f"Out of range: {index}", file=stream)


def _describe(candidate: AreaCandidate) -> str:
    admin = f", admin_level={candidate.admin_level}" if candidate.admin_level else ""
    return (
        f"{candidate.display_name} "
        f"[{candidate.category}={candidate.place_type}{admin}, "
        f"relation {candidate.osm_id} -> area {candidate.area_id}]"
    )
