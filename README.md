# osm_businesses

Extract businesses and POIs from OpenStreetMap via the Overpass API and write
them to CSV — with flexible area selection and clean contact extraction.

OSM only. No Google Maps fallback, no HTML scraping of map sites.

## Install

```bash
pip install -r requirements.txt      # requests, pytest
python osm_businesses.py --help
```

Python 3.11+. Standard library plus `requests`; `pytest` for the tests.

## Area selection

Exactly one of these five flags is required.

### 1. `--place` — a named place, resolved through Nominatim

```bash
python osm_businesses.py --place "Niš" --country RS --yes -o nis.csv
```

The name is **not** guessed at against Overpass. It goes to Nominatim
(`/search`, `format=jsonv2`, `addressdetails=1`, `limit=10`), the results are
filtered down to relations whose class is `boundary` or `place` — the only
things that can become an Overpass area — and the relation id is converted with
`3600000000 + osm_id`.

This matters for Serbian places in particular: most carry Cyrillic as the
primary `name` tag, so a Latin string matched straight against Overpass finds
nothing. Nominatim indexes the name variants, so `"Niš"` resolves to `Ниш`
correctly. Do not work around this with transliteration.

When several areas match you get a table and a prompt:

```
$ python osm_businesses.py --list-areas "Niš" --country RS
#  display_name                                                     type            osm_id    admin_level
1  Ниш, Град Ниш, Нишавски управни округ, Централна Србија, Србија  city            11538321  6
2  Ниш (Медијана), Градска општина Медијана, Град Ниш, ...          administrative  10625873  9
3  Ниш (Палилула), Градска општина Палилула, Град Ниш, ...          administrative  10277501  9
4  Ниш (Пантелеј), Градска општина Пантелеј, Град Ниш, ...          administrative  10277637  9
```

- `--list-areas NAME` prints that table and exits without querying Overpass.
- `--pick N` selects candidate N non-interactively.
- `--yes` takes the top match.
- `--country RS` narrows Nominatim by country code.
- Zero matches is an error that suggests `--bbox`.

Resolved names are cached in `~/.cache/osm_businesses/areas.json`, so repeat
runs skip Nominatim entirely. `--no-cache` bypasses it, `--cache-path` moves it.

Nominatim's usage policy is enforced in code, not left to the caller: at most
one request per second, and an identifying `User-Agent`.

### 2. `--area-id` — a known Overpass area id

```bash
python osm_businesses.py --area-id 3611538321 -o nis.csv
```

Skips resolution entirely. Get the id from `--list-areas`.

### 3. `--bbox S,W,N,E` — a raw bounding box

```bash
python osm_businesses.py --bbox 43.28,21.85,43.36,21.96 -o nis-box.csv
```

Order is south, west, north, east — the Overpass order, not the GeoJSON one.

### 4. `--center` + `--radius` — a circle

```bash
python osm_businesses.py --center 43.32,21.90 --radius 3km -o centar.csv
```

Becomes Overpass `around:`. The radius accepts `3km`, `800m`, or a bare number
of metres.

### 5. `--poly` — an arbitrary polygon

```bash
python osm_businesses.py --poly area.geojson -o zona.csv
```

Reads a GeoJSON `Polygon` or `MultiPolygon` (bare geometry, `Feature`, or
`FeatureCollection` — the first polygon's outer ring is used) and emits an
Overpass `poly:` filter. GeoJSON's lon,lat order is converted to Overpass's
lat lon.

## Debugging: `--dry-run`

Prints the resolved area and the generated Overpass QL, then exits without
calling anything. The output is copy-pasteable straight into
[overpass-turbo.eu](https://overpass-turbo.eu/).

```bash
$ python osm_businesses.py --place "Niš" --pick 1 --dry-run --category shop
Area:  Ниш, Град Ниш, ... -> Overpass area 3611538321
Overpass QL (paste into https://overpass-turbo.eu/):
[out:json][timeout:300];
area(3611538321)->.searchArea;
(
  nwr["shop"](area.searchArea);
);
out center tags;
```

## What gets extracted

The query covers `nwr` for `shop`, `amenity`, `office`, `craft`, `tourism` and
`healthcare`, with `out center tags;`.

| Column | Source |
|---|---|
| `osm_type`, `osm_id` | element identity |
| `name` | `name`, else `operator`, else `brand` |
| `category` | first of shop / office / craft / healthcare / tourism / amenity, raw OSM value |
| `place` | `addr:city`, else `addr:suburb`, else `addr:place` |
| `street`, `housenumber`, `postcode` | `addr:street`, `addr:housenumber`, `addr:postcode` |
| `phone` | `phone`, else `contact:phone`, else `contact:mobile`; normalized to digits with a leading `+` |
| `phone_alt` | the remaining numbers when a tag holds several, `;` separated |
| `website` | `website`, else `contact:website`, else `url`; `https://` prepended when the scheme is missing, implausible values dropped |
| `email` | `email`, else `contact:email` |
| `facebook`, `instagram` | `contact:facebook`, `contact:instagram` |
| `opening_hours` | raw |
| `lat`, `lon` | node: top level; way/relation: under `center` |

A missing tag becomes an empty string — never a `KeyError`.

Rows are dropped when they have no name after the fallbacks, no usable
coordinate, or are street furniture (`bench`, `waste_basket`, `atm`, `parking`,
`toilets`, `vending_machine`, …). The furniture exclusion applies **only** when
the element has no `shop`, `office` or `craft` key, so a bakery that also tags
`amenity=cafe` survives.

The same POI is frequently mapped as both a node and a building way. Rows are
deduplicated on `(name.lower(), round(lat,5), round(lon,5))`, keeping whichever
has more non-empty fields; on a tie the node wins.

## Filtering

```bash
--require-contact              drop rows with neither phone nor website
--category shop,office         restrict to these top-level keys (repeatable)
--exclude-category amenity     drop these top-level keys
--limit 500                    keep at most N rows
```

`--category` narrows the Overpass query too, not just the output, so it also
makes the request cheaper.

## Output

CSV in `utf-8-sig`, so Excel renders š/č/ć/ž correctly. Columns:

```
osm_type, osm_id, name, category, place, street, housenumber, postcode,
phone, phone_alt, website, email, facebook, instagram, opening_hours, lat, lon
```

Sorted by category, then name. `--format json` writes the same records as a
JSON array instead. `-o PATH` writes to a file; without it, output goes to
stdout and the summary goes to stderr, so redirecting is safe:

```bash
python osm_businesses.py --bbox 43.31,21.88,43.33,21.91 > out.csv
```

The summary on stderr reports how many elements Overpass returned, how many
rows survived parsing and filters, and how many carry a phone or a website.

## Networking

- Endpoints tried in order: `overpass-api.de`, `overpass.kumi.systems`,
  `overpass.private.coffee`. A 429/504/timeout moves on to the next.
- Exponential backoff with jitter, at most 4 attempts per endpoint.
- The query is POSTed in the body, never placed in the URL.
- 300 s read timeout. A city-sized area taking minutes is normal, not a bug.
- Identifying `User-Agent` on every request.
- An HTML response body is reported as an Overpass overload page, not as a JSON
  decode traceback.

## Tests

```bash
python -m pytest -q
```

73 tests, no network anywhere — the HTTP layer is mocked and every fixture is
hand-built. `test_parse.py` covers the pure `parse_elements` function
(coordinates, name fallbacks, furniture exclusion, dedupe, phone and website
normalization); `test_geo.py` covers area resolution (candidate filtering, the
area-id arithmetic, `--pick`, the cache, and the rate limiter).

## Attribution

Data © OpenStreetMap contributors, available under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/).
See <https://www.openstreetmap.org/copyright>.

Anything you publish from this tool's output must carry that attribution and
respect the ODbL's share-alike terms. Both Nominatim and the public Overpass
instances are volunteer-funded: keep requests modest, cache results (this tool
caches resolved areas for you), and do not remove the rate limiting.
