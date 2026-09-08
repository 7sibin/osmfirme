#!/usr/bin/env python3
"""Extract businesses / POIs from OpenStreetMap via the Overpass API into CSV.

Data (c) OpenStreetMap contributors, ODbL. https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TextIO

import requests

import geo
from geo import AreaCache, NominatimClient, PlaceNotFoundError, RateLimiter, format_candidates, resolve_place

__version__ = "1.0.0"

USER_AGENT = (
    f"osm_businesses/{__version__} "
    "(OSM POI extractor; +https://github.com/openstreetmap/osm-businesses-cli)"
)

OVERPASS_ENDPOINTS: tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

#: Top-level keys we query for, in the order Overpass sees them.
QUERY_KEYS: tuple[str, ...] = ("shop", "amenity", "office", "craft", "tourism", "healthcare")

#: Order in which a row's `category` is taken from the element's tags.
CATEGORY_KEYS: tuple[str, ...] = ("shop", "office", "craft", "healthcare", "tourism", "amenity")

#: An amenity value from this set is street furniture, not a business...
STREET_FURNITURE: frozenset[str] = frozenset(
    {
        "bench",
        "waste_basket",
        "atm",
        "parking",
        "parking_space",
        "bicycle_parking",
        "drinking_water",
        "shelter",
        "toilets",
        "recycling",
        "fountain",
        "telephone",
        "post_box",
        "vending_machine",
        "charging_station",
        "clock",
        "bbq",
        "bicycle_repair_station",
    }
)

#: ...unless the element also carries one of these, in which case it is a real business.
BUSINESS_KEYS: tuple[str, ...] = ("shop", "office", "craft")

COLUMNS: tuple[str, ...] = (
    "osm_type",
    "osm_id",
    "name",
    "category",
    "place",
    "street",
    "housenumber",
    "postcode",
    "phone",
    "phone_alt",
    "website",
    "email",
    "facebook",
    "instagram",
    "opening_hours",
    "lat",
    "lon",
)

#: Values that show up in `website` but are not websites.
WEBSITE_JUNK: frozenset[str] = frozenset({"yes", "no", "none", "n/a", "na", "-", "?", "unknown"})

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_RADIUS_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(km|m)?\s*$", re.IGNORECASE)

OVERPASS_TIMEOUT = 300

#: Same name this close together: one POI mapped twice, as a node and as the
#: building way around it. Building centres sit tens of metres off their node.
SAME_POI_M = 50.0
#: Same name, different category: the name alone is weaker evidence — "Tvrdjava"
#: the bakery and "Tvrdjava" the pharmacy are both real — so the two have to be
#: practically on top of each other before they count as one double-tagged POI.
CROSS_CATEGORY_M = 15.0
METRES_PER_DEGREE = 111_320.0

#: Columns that could tell two same-named rows apart in an export. osm_type,
#: osm_id, lat and lon are left out on purpose: they always differ, and nobody
#: calls a business on its OSM id.
DISTINGUISHING: tuple[str, ...] = (
    "category",
    "place",
    "street",
    "housenumber",
    "postcode",
    "phone",
    "phone_alt",
    "website",
    "email",
    "facebook",
    "instagram",
    "opening_hours",
)


class OverpassError(RuntimeError):
    """Overpass could not be queried, or answered with something unusable."""


# ---------------------------------------------------------------------------
# Row + parsing (pure: no network, no I/O, no globals mutated)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One business, flattened to the CSV shape."""

    osm_type: str
    osm_id: int
    name: str
    category: str
    place: str
    street: str
    housenumber: str
    postcode: str
    phone: str
    phone_alt: str
    website: str
    email: str
    facebook: str
    instagram: str
    opening_hours: str
    lat: float
    lon: float
    category_key: str = field(default="", compare=True)
    """Which top-level key produced `category`; used by --category/--exclude-category."""
    found_website: str = field(default="", compare=False)
    """A site the web search turned up for a row OSM has no `website` for."""
    found_confidence: str = field(default="", compare=False)
    """How much to trust `found_website`: "strong" or "weak". Empty when unchecked."""
    found_source: str = field(default="", compare=False)
    """Where it came from: "search", "mention" or "social"."""
    found_email: str = field(default="", compare=False)
    """An address read off the business's own site, when OSM carries none."""
    found_phone: str = field(default="", compare=False)
    """A number read off the same page, likewise only when OSM carries none."""
    contact_status: str = field(default="", compare=False)
    """How reading the site went: "ok", "none", "dead". Empty means never tried."""

    def as_output_dict(self) -> dict[str, Any]:
        return {column: getattr(self, column) for column in COLUMNS}

    @property
    def filled_count(self) -> int:
        """How many optional columns actually carry a value."""
        return sum(1 for column in COLUMNS if str(getattr(self, column, "")).strip())

    @property
    def has_contact(self) -> bool:
        """Some way to actually reach them.

        A website that has been read and turned out not to answer is not one, so
        it does not count - otherwise "only with contact details" would list
        businesses whose only listed detail is a domain that is gone. Nothing
        changes for the CLI, which never reads sites and so never sets
        `contact_status`.
        """
        if self.phone or self.found_phone:
            return True
        return bool(self.website) and self.contact_status != "dead"


def parse_elements(elements: list[dict[str, Any]]) -> list[Row]:
    """Turn raw Overpass elements into clean rows.

    Drops the unnamed, the uncoordinated and the street furniture, then
    collapses repeats of the same business — see `_dedupe`.
    """
    rows: list[Row] = []
    for element in elements:
        row = _row_from_element(element)
        if row is not None:
            rows.append(row)
    return _dedupe(rows)


def _row_from_element(element: dict[str, Any]) -> Row | None:
    tags = element.get("tags") or {}
    if not isinstance(tags, dict):
        return None

    name = _first(tags, "name", "operator", "brand")
    if not name:
        return None

    coordinate = _coordinate(element)
    if coordinate is None:
        return None
    lat, lon = coordinate

    if _is_street_furniture(tags):
        return None

    category_key, category = _category(tags)
    phone, phone_alt = _phones(tags)

    return Row(
        osm_type=str(element.get("type", "")),
        osm_id=int(element.get("id", 0)),
        name=name,
        category=category,
        category_key=category_key,
        place=_first(tags, "addr:city", "addr:suburb", "addr:place"),
        street=_first(tags, "addr:street"),
        housenumber=_first(tags, "addr:housenumber"),
        postcode=_first(tags, "addr:postcode"),
        phone=phone,
        phone_alt=phone_alt,
        website=_website(tags),
        email=_first(tags, "email", "contact:email"),
        facebook=_first(tags, "contact:facebook", "facebook"),
        instagram=_first(tags, "contact:instagram", "instagram"),
        opening_hours=_first(tags, "opening_hours"),
        lat=lat,
        lon=lon,
    )


def _first(tags: dict[str, Any], *keys: str) -> str:
    """First non-empty tag value among `keys`, or an empty string. Never raises."""
    for key in keys:
        value = tags.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _coordinate(element: dict[str, Any]) -> tuple[float, float] | None:
    """Nodes carry lat/lon at the top level; ways and relations under `center`."""
    lat, lon = element.get("lat"), element.get("lon")
    if lat is None or lon is None:
        center = element.get("center") or {}
        if isinstance(center, dict):
            lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _is_street_furniture(tags: dict[str, Any]) -> bool:
    """A bench is not a business — but a shop that also tags an amenity is."""
    if any(tags.get(key) for key in BUSINESS_KEYS):
        return False
    return _first(tags, "amenity") in STREET_FURNITURE


def _category(tags: dict[str, Any]) -> tuple[str, str]:
    for key in CATEGORY_KEYS:
        value = _first(tags, key)
        if value:
            return key, value
    return "", ""


def _phones(tags: dict[str, Any]) -> tuple[str, str]:
    """Normalize, and split a `;`-separated tag into the first number and the rest."""
    raw = _first(tags, "phone", "contact:phone", "contact:mobile")
    numbers = [normalize_phone(part) for part in raw.split(";")]
    numbers = [number for number in numbers if number]
    if not numbers:
        return "", ""
    return numbers[0], ";".join(numbers[1:])


#: Serbian Cyrillic to Latin, so the same business matches whichever alphabet
#: the mapper used. Serbian Cyrillic is a strict one-sound-one-letter script, so
#: unlike Russian this transliteration is exact and reversible.
_CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "dj",
    "е": "e", "ж": "z", "з": "z", "и": "i", "ј": "j", "к": "k",
    "л": "l", "љ": "lj", "м": "m", "н": "n", "њ": "nj", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "ћ": "c", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "c", "џ": "dz", "ш": "s",
}

#: Serbian Latin letters NFKD does not decompose, or decomposes the wrong way
#: for matching purposes. Applied before the generic accent strip.
_FOLD_MAP = str.maketrans(
    {
        **_CYRILLIC_MAP,
        "đ": "dj", "Đ": "dj",  # d with stroke
        "ð": "dj", "Ð": "dj",  # eth, which some exports use for the same letter
        "ł": "l", "Ł": "l",
        "ß": "ss",
    }
)


def fold_text(value: str) -> str:
    """Casefold and strip accents, so `Pekara Čvorović` matches `pekara cvorovic`.

    Both chain collapsing and the website search compare names that reach us in
    every spelling at once: Cyrillic transliterations, Latin with and without
    diacritics, and the ASCII a keyboard without them produces.
    """
    folded = value.casefold().translate(_FOLD_MAP)
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.split())


def normalize_phone(value: str) -> str:
    """Keep digits and a leading +; drop spaces, dashes, slashes and parens."""
    stripped = value.strip()
    digits = "".join(character for character in stripped if character.isdigit())
    if not digits:
        return ""
    return f"+{digits}" if stripped.startswith("+") else digits


def _website(tags: dict[str, Any]) -> str:
    return normalize_website(_first(tags, "website", "contact:website", "url"))


def normalize_website(value: str) -> str:
    """Add https:// when a scheme is missing; discard anything implausible."""
    candidate = value.strip()
    if not candidate or candidate.lower() in WEBSITE_JUNK:
        return ""
    if any(character.isspace() for character in candidate):
        return ""
    if not _SCHEME_RE.match(candidate):
        candidate = f"https://{candidate}"
    elif not candidate.lower().startswith(("http://", "https://")):
        return ""

    host = candidate.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    if "." not in host or host.startswith(".") or host.endswith("."):
        return ""
    if not any(character.isalpha() for character in host):
        return ""
    return candidate


def _dedupe(rows: list[Row]) -> list[Row]:
    """Collapse the repeats OSM produces for one business, keeping the richest.

    Two shapes of repeat show up in real extracts. The same POI is often mapped
    twice, as a node *and* as the building way around it: same name, metres
    apart. And branches of a chain can arrive as rows that differ in nothing
    anyone could act on — same name, the same missing address, the same central
    phone. Both are noise in an export. A branch that carries its own address or
    number differs in a column that matters, so it survives.
    """
    kept: list[Row] = []
    by_name: dict[str, list[int]] = {}
    for row in rows:
        # Only same-named rows can be duplicates, so compare within that bucket:
        # a whole-list scan would be quadratic on a city-sized extract.
        group = by_name.setdefault(row.name.casefold(), [])
        index = _duplicate_of(row, kept, group)
        if index is None:
            group.append(len(kept))
            kept.append(row)
        elif is_richer(row, kept[index]):
            kept[index] = row
    return kept


def _duplicate_of(row: Row, kept: list[Row], group: Sequence[int]) -> int | None:
    """Index in `kept` of the row this one repeats, or None if it is new."""
    for index in group:
        other = kept[index]
        if all(getattr(row, column) == getattr(other, column) for column in DISTINGUISHING):
            return index
        limit = SAME_POI_M if row.category == other.category else CROSS_CATEGORY_M
        if _metres_between(row, other) <= limit:
            return index
    return None


def _metres_between(a: Row, b: Row) -> float:
    """Equirectangular approximation: exact enough over the tens of metres here."""
    north = (a.lat - b.lat) * METRES_PER_DEGREE
    east = (a.lon - b.lon) * METRES_PER_DEGREE * math.cos(math.radians((a.lat + b.lat) / 2))
    return math.hypot(north, east)


def is_richer(challenger: Row, incumbent: Row) -> bool:
    """Whether `challenger` should replace `incumbent` as the kept copy of a repeat."""
    if challenger.filled_count != incumbent.filled_count:
        return challenger.filled_count > incumbent.filled_count
    # Tie: prefer the node, which is the POI itself rather than the building.
    return challenger.osm_type == "node" and incumbent.osm_type != "node"


# ---------------------------------------------------------------------------
# Area selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AreaSpec:
    """How the Overpass query is scoped, plus a human description for --dry-run."""

    kind: str
    description: str
    selector: str
    preamble: str = ""


def area_spec_from_area_id(area_id: int) -> AreaSpec:
    return AreaSpec(
        kind="area",
        description=f"Overpass area {area_id}",
        selector="(area.searchArea)",
        preamble=f"area({area_id})->.searchArea;",
    )


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    """Parse `S,W,N,E` into floats, validating ordering and ranges."""
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise ValueError(f"--bbox needs four comma-separated numbers S,W,N,E; got {text!r}")
    try:
        south, west, north, east = (float(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"--bbox values must be numbers; got {text!r}") from exc
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("--bbox latitudes must be between -90 and 90")
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise ValueError("--bbox longitudes must be between -180 and 180")
    if south >= north:
        raise ValueError("--bbox south must be smaller than north (order is S,W,N,E)")
    if west >= east:
        raise ValueError("--bbox west must be smaller than east (order is S,W,N,E)")
    return south, west, north, east


def area_spec_from_bbox(text: str) -> AreaSpec:
    south, west, north, east = parse_bbox(text)
    box = f"{south},{west},{north},{east}"
    return AreaSpec(kind="bbox", description=f"bounding box {box} (S,W,N,E)", selector=f"({box})")


def parse_center(text: str) -> tuple[float, float]:
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        raise ValueError(f"--center needs lat,lon; got {text!r}")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ValueError(f"--center values must be numbers; got {text!r}") from exc
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError(f"--center out of range; got {text!r}")
    return lat, lon


def parse_radius(text: str) -> float:
    """`3km`, `500m` or a bare number of metres -> metres."""
    match = _RADIUS_RE.match(text)
    if not match:
        raise ValueError(f"--radius must look like 3km, 500m or 2500; got {text!r}")
    value = float(match.group(1))
    if (match.group(2) or "m").lower() == "km":
        value *= 1000
    if value <= 0:
        raise ValueError("--radius must be positive")
    return value


def area_spec_from_center(center_text: str, radius_text: str) -> AreaSpec:
    lat, lon = parse_center(center_text)
    radius = parse_radius(radius_text)
    return AreaSpec(
        kind="around",
        description=f"circle of {radius:g} m around {lat},{lon}",
        selector=f"(around:{radius:g},{lat},{lon})",
    )


def polygon_from_geojson(data: Any) -> list[tuple[float, float]]:
    """Pull the first polygon's outer ring out of a GeoJSON document."""
    geometry = _first_geometry(data)
    if geometry is None:
        raise ValueError("no Polygon or MultiPolygon geometry found in the GeoJSON")
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        ring = coordinates[0]
    elif geometry_type == "MultiPolygon":
        ring = coordinates[0][0]
    else:
        raise ValueError(f"unsupported geometry type {geometry_type!r}; need Polygon or MultiPolygon")

    points = [(float(point[1]), float(point[0])) for point in ring]  # GeoJSON is lon,lat
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]  # Overpass does not want the ring closed
    if len(points) < 3:
        raise ValueError("polygon needs at least three distinct points")
    return points


def _first_geometry(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind in {"Polygon", "MultiPolygon"}:
        return data
    if kind == "Feature":
        return _first_geometry(data.get("geometry"))
    if kind == "FeatureCollection":
        for feature in data.get("features") or []:
            geometry = _first_geometry(feature)
            if geometry is not None:
                return geometry
    if kind == "GeometryCollection":
        for geometry_item in data.get("geometries") or []:
            geometry = _first_geometry(geometry_item)
            if geometry is not None:
                return geometry
    return None


def area_spec_from_poly(path: str | Path) -> AreaSpec:
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    points = polygon_from_geojson(data)
    poly = " ".join(f"{lat:.7f} {lon:.7f}" for lat, lon in points)
    return AreaSpec(
        kind="poly",
        description=f"polygon from {path} ({len(points)} points)",
        selector=f'(poly:"{poly}")',
    )


# ---------------------------------------------------------------------------
# Overpass QL
# ---------------------------------------------------------------------------


def build_query(spec: AreaSpec, keys: Sequence[str] = QUERY_KEYS, timeout: int = OVERPASS_TIMEOUT) -> str:
    """Generate Overpass QL that is copy-pasteable into overpass-turbo.eu."""
    lines = [f"[out:json][timeout:{timeout}];"]
    if spec.preamble:
        lines.append(spec.preamble)
    lines.append("(")
    lines.extend(f'  nwr["{key}"]{spec.selector};' for key in keys)
    lines.append(");")
    lines.append("out center tags;")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------


RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class OverpassClient:
    """POSTs a query, failing over between endpoints with backoff and jitter."""

    def __init__(
        self,
        session: Any | None = None,
        endpoints: Sequence[str] = OVERPASS_ENDPOINTS,
        user_agent: str = USER_AGENT,
        max_attempts: int = 4,
        read_timeout: float = float(OVERPASS_TIMEOUT),
        connect_timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        stream: TextIO = sys.stderr,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.endpoints = tuple(endpoints)
        self.user_agent = user_agent
        self.max_attempts = max_attempts
        self.read_timeout = read_timeout
        self.connect_timeout = connect_timeout
        self.sleep = sleep
        self.stream = stream

    def fetch(self, query: str) -> list[dict[str, Any]]:
        last_error: str = "no endpoint was tried"
        for endpoint in self.endpoints:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    return self._attempt(endpoint, query)
                except OverpassError as exc:
                    last_error = f"{endpoint}: {exc}"
                    if attempt == self.max_attempts:
                        print(f"Giving up on {endpoint}: {exc}", file=self.stream)
                        break
                    delay = self._backoff(attempt)
                    print(
                        f"{endpoint} failed ({exc}); retry {attempt}/{self.max_attempts - 1} in {delay:.1f}s",
                        file=self.stream,
                    )
                    self.sleep(delay)
        raise OverpassError(f"All Overpass endpoints failed. Last error - {last_error}")

    def _backoff(self, attempt: int) -> float:
        return min(60.0, 2.0**attempt) * (0.5 + random.random())

    def _attempt(self, endpoint: str, query: str) -> list[dict[str, Any]]:
        try:
            response = self.session.post(
                endpoint,
                data={"data": query},  # the query goes in the body, never in the URL
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except requests.exceptions.RequestException as exc:
            raise OverpassError(f"request failed: {exc}") from exc

        status = getattr(response, "status_code", 200)
        if status in RETRYABLE_STATUS:
            raise OverpassError(f"HTTP {status} (server busy or overloaded)")
        if status >= 400:
            raise OverpassError(f"HTTP {status}: {_snippet(getattr(response, 'text', ''))}")

        return _decode(response)


def _decode(response: Any) -> list[dict[str, Any]]:
    body = getattr(response, "text", "") or ""
    content_type = ""
    headers = getattr(response, "headers", None)
    if headers is not None:
        content_type = str(headers.get("Content-Type", "")).lower()

    if "html" in content_type or body.lstrip().startswith(("<!", "<html", "<HTML")):
        raise OverpassError(
            "endpoint returned an HTML page instead of JSON - this is the Overpass "
            f"overload/rate-limit page, not a parse problem. Body starts: {_snippet(body)}"
        )
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise OverpassError(f"response was not JSON: {_snippet(body)}") from exc

    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        raise OverpassError(f"response JSON had no 'elements' list: {_snippet(body)}")
    return elements


def _snippet(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


# ---------------------------------------------------------------------------
# Filtering and output
# ---------------------------------------------------------------------------


def apply_filters(
    rows: list[Row],
    *,
    require_contact: bool = False,
    categories: set[str] | None = None,
    exclude_categories: set[str] | None = None,
    limit: int | None = None,
) -> list[Row]:
    kept = rows
    if categories:
        kept = [row for row in kept if row.category_key in categories]
    if exclude_categories:
        kept = [row for row in kept if row.category_key not in exclude_categories]
    if require_contact:
        kept = [row for row in kept if row.has_contact]
    if limit is not None and limit >= 0:
        kept = kept[:limit]
    return kept


def sort_rows(rows: list[Row]) -> list[Row]:
    return sorted(rows, key=lambda row: (row.category.lower(), row.name.lower()))


def write_csv(rows: Iterable[Row], destination: Path | None) -> None:
    handle = (
        destination.open("w", encoding="utf-8-sig", newline="")
        if destination is not None
        else _reconfigured_stdout("utf-8-sig")
    )
    try:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_output_dict())
    finally:
        if destination is not None:
            handle.close()


def write_json(rows: Iterable[Row], destination: Path | None) -> None:
    payload = [row.as_output_dict() for row in rows]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if destination is None:
        print(text, file=_reconfigured_stdout())
    else:
        destination.write_text(text + "\n", encoding="utf-8")


def _reconfigured_stdout(encoding: str = "utf-8") -> TextIO:
    """Excel wants the BOM and OSM names want UTF-8, on a redirect as much as a file."""
    try:
        sys.stdout.reconfigure(encoding=encoding, newline="")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    return sys.stdout


def summarize(total_found: int, rows: list[Row], stream: TextIO = sys.stderr) -> None:
    with_phone = sum(1 for row in rows if row.phone)
    with_website = sum(1 for row in rows if row.website)
    print(
        f"Overpass returned {total_found} elements; kept {len(rows)} after parsing and filters. "
        f"{with_phone} with a phone, {with_website} with a website.",
        file=stream,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osm_businesses",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Extract businesses and POIs from OpenStreetMap into CSV.",
        epilog=(
            "Area selection - pick exactly one:\n"
            '  --place "Nis"                        resolved through Nominatim\n'
            "  --area-id 3601234567                 a known Overpass area id\n"
            "  --bbox 43.28,21.85,43.36,21.96       raw bounding box, S,W,N,E\n"
            "  --center 43.32,21.90 --radius 3km    circle\n"
            "  --poly area.geojson                  arbitrary polygon\n"
            "\n"
            'Not sure which area --place will pick?  --list-areas "Nis"  shows the\n'
            "candidates and exits.  --dry-run prints the Overpass QL for pasting\n"
            "into overpass-turbo.eu.\n"
            "\n"
            "Data (c) OpenStreetMap contributors, ODbL."
        ),
    )

    area = parser.add_argument_group("area selection (exactly one required)")
    exclusive = area.add_mutually_exclusive_group()
    exclusive.add_argument("--place", metavar="NAME", help="place name, resolved via Nominatim")
    exclusive.add_argument("--area-id", type=int, metavar="ID", help="Overpass area id, e.g. 3601234567")
    exclusive.add_argument("--bbox", metavar="S,W,N,E", help="bounding box")
    exclusive.add_argument("--center", metavar="LAT,LON", help="circle centre; requires --radius")
    exclusive.add_argument("--poly", metavar="PATH", help="GeoJSON file with a polygon")
    area.add_argument("--radius", metavar="DIST", help="circle radius for --center, e.g. 3km or 800m")

    resolution = parser.add_argument_group("--place resolution")
    resolution.add_argument(
        "--list-areas",
        metavar="NAME",
        help="print the Nominatim candidate areas for NAME and exit (does not query Overpass)",
    )
    resolution.add_argument("--country", metavar="CC", help="narrow Nominatim by country code, e.g. RS")
    resolution.add_argument("--pick", type=int, metavar="N", help="choose candidate N non-interactively")
    resolution.add_argument("--yes", action="store_true", help="take the top candidate without asking")
    resolution.add_argument("--no-cache", action="store_true", help="bypass the resolved-area cache")
    resolution.add_argument(
        "--cache-path",
        metavar="PATH",
        default=str(geo.DEFAULT_CACHE_PATH),
        help=f"where resolved areas are cached (default: {geo.DEFAULT_CACHE_PATH})",
    )

    filters = parser.add_argument_group("filters")
    filters.add_argument(
        "--category",
        action="append",
        default=[],
        metavar="KEYS",
        help=f"restrict to these top-level keys (repeatable, comma-separated). One of: {', '.join(QUERY_KEYS)}",
    )
    filters.add_argument(
        "--exclude-category", action="append", default=[], metavar="KEYS", help="drop these top-level keys"
    )
    filters.add_argument(
        "--require-contact", action="store_true", help="drop rows with neither phone nor website"
    )
    filters.add_argument("--limit", type=int, metavar="N", help="keep at most N rows")

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output", metavar="PATH", help="output file (default: stdout)")
    output.add_argument("--format", choices=("csv", "json"), default="csv", help="output format (default: csv)")
    output.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved area and the generated Overpass QL, then exit without querying",
    )
    output.add_argument("--version", action="version", version=f"osm_businesses {__version__}")
    return parser


def _split_keys(values: list[str], flag: str) -> set[str]:
    keys: set[str] = set()
    for value in values:
        for part in value.split(","):
            key = part.strip().lower()
            if not key:
                continue
            if key not in QUERY_KEYS:
                raise SystemExit(f"{flag}: unknown key {key!r}; choose from {', '.join(QUERY_KEYS)}")
            keys.add(key)
    return keys


def _make_nominatim_client() -> NominatimClient:
    return NominatimClient(
        session=requests.Session(),
        user_agent=USER_AGENT,
        rate_limiter=RateLimiter(min_interval=geo.NOMINATIM_MIN_INTERVAL),
    )


def _resolve_area(args: argparse.Namespace) -> AreaSpec:
    """Turn the chosen area flag into an AreaSpec, or exit with a clear message."""
    selected = [
        name
        for name, value in (
            ("--place", args.place),
            ("--area-id", args.area_id),
            ("--bbox", args.bbox),
            ("--center", args.center),
            ("--poly", args.poly),
        )
        if value is not None
    ]
    if not selected:
        raise SystemExit(
            "One area flag is required: --place, --area-id, --bbox, --center/--radius or --poly. "
            "See --help."
        )

    if args.center is not None and not args.radius:
        raise SystemExit("--center requires --radius, e.g. --radius 3km")
    if args.radius and args.center is None:
        raise SystemExit("--radius only makes sense together with --center")

    try:
        if args.area_id is not None:
            return area_spec_from_area_id(args.area_id)
        if args.bbox is not None:
            return area_spec_from_bbox(args.bbox)
        if args.center is not None:
            return area_spec_from_center(args.center, args.radius)
        if args.poly is not None:
            return area_spec_from_poly(args.poly)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    except OSError as exc:
        raise SystemExit(f"--poly: cannot read {args.poly}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--poly: {args.poly} is not valid JSON: {exc}") from exc

    cache = None if args.no_cache else AreaCache(args.cache_path)
    try:
        candidate = resolve_place(
            args.place,
            client=_make_nominatim_client(),
            country=args.country,
            pick=args.pick,
            assume_yes=args.yes,
            cache=cache,
        )
    except PlaceNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    except requests.exceptions.RequestException as exc:
        raise SystemExit(f"Nominatim request failed: {exc}") from exc

    spec = area_spec_from_area_id(candidate.area_id)
    return AreaSpec(
        kind=spec.kind,
        description=f"{candidate.display_name} -> {spec.description}",
        selector=spec.selector,
        preamble=spec.preamble,
    )


def _list_areas(args: argparse.Namespace) -> int:
    try:
        candidates = _make_nominatim_client().search(args.list_areas, country=args.country)
    except (PlaceNotFoundError, requests.exceptions.RequestException) as exc:
        print(f"Nominatim lookup failed: {exc}", file=sys.stderr)
        return 1
    if not candidates:
        print(
            f"No boundary or place relation found for {args.list_areas!r}. "
            "Try --country, a more specific name, or --bbox.",
            file=sys.stderr,
        )
        return 0
    print(format_candidates(candidates))
    print(
        "\nUse --place with --pick N, or pass the area id directly: "
        f"--area-id {candidates[0].area_id}",
        file=sys.stderr,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_areas:
        return _list_areas(args)

    categories = _split_keys(args.category, "--category")
    exclude_categories = _split_keys(args.exclude_category, "--exclude-category")
    query_keys = tuple(key for key in QUERY_KEYS if key in categories) if categories else QUERY_KEYS
    query_keys = tuple(key for key in query_keys if key not in exclude_categories)
    if not query_keys:
        raise SystemExit("--category / --exclude-category leave no keys to query")

    spec = _resolve_area(args)
    query = build_query(spec, query_keys)

    if args.dry_run:
        print(f"Area:  {spec.description}", file=sys.stderr)
        print("Overpass QL (paste into https://overpass-turbo.eu/):", file=sys.stderr)
        print(query)
        return 0

    print(f"Querying Overpass for {spec.description} - this can take minutes for a city.", file=sys.stderr)
    try:
        elements = OverpassClient().fetch(query)
    except OverpassError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    rows = parse_elements(elements)
    rows = apply_filters(
        rows,
        require_contact=args.require_contact,
        categories=categories,
        exclude_categories=exclude_categories,
        limit=args.limit,
    )
    rows = sort_rows(rows)

    destination = Path(args.output) if args.output else None
    if args.format == "json":
        write_json(rows, destination)
    else:
        write_csv(rows, destination)

    summarize(len(elements), rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
