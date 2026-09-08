# osm_businesses

Extract businesses and POIs from OpenStreetMap via the Overpass API and write
them to CSV — with flexible area selection and clean contact extraction.

OSM only. No Google Maps fallback, no HTML scraping of map sites.

## Install

```bash
pip install -r requirements.txt
python osm_businesses.py --help
```

Python 3.11+. The CLI needs only the standard library plus `requests`; the web
app adds `fastapi`, `uvicorn`, `openpyxl` and `ddgs` (the last only for
[finding sites the map does not know about](#finding-sites-the-map-does-not-know-about));
`pytest` runs the tests.

## Web app

Everything the CLI does, in a browser — with a map, a preview table and an
Excel download instead of flags and a CSV file.

```bash
uvicorn webapp.main:app --reload      # then open http://127.0.0.1:8000
```

The page is a fixed-height instrument, not a scrolling document: a control
column on the left, the map as a permanent surface on the right, and results
appearing as a third zone between them. Nothing navigates away — searching,
drawing, working, failing and listing are all bands that appear in place, so
the map is never lost. Below 720px the three zones become tabs with the map
pinned above them and the rows shown as records rather than a table.

The three sections:

1. **Oblast** — type a place name to get the same Nominatim candidates
   `--list-areas` prints; picking one draws its boundary on the map. Or draw a
   rectangle or a circle directly on the map, which is the `--bbox` and
   `--center`/`--radius` selection by other means. Exactly one area is active
   at a time: drawing replaces a searched area and vice versa.
2. **Sta trazimo** — tick the top-level keys to query (`shop`, `amenity`,
   `office`, `craft`, `tourism`, `healthcare`). This narrows the Overpass query
   itself, so fewer ticks also means a cheaper request.
3. **Rezultati** — the rows in a sortable, paginated table, with a text search,
   a "samo sa kontaktom" toggle, a "prikazi i one koje imaju sajt" toggle, and a
   checkbox per concrete category actually found (`bakery`, `cafe`, `dentist`,
   …). Every row that survives the filters is also a dot on the map, so the
   table and the map always show the same set. "Preuzmi Excel" downloads
   exactly what the filters currently show.

   **The website toggle starts off, and off means businesses that already have
   a website are hidden.** That is deliberate: the default view is the outreach
   list — whoever is missing a site. Tick the box to see everything. The count
   line says so whenever rows are being hidden this way.

The interface is in Serbian; the code is not.

### Jobs and caching

A city-sized query takes minutes, so a search runs as a background job. The
page polls `/api/jobs/{id}` once a second and shows the phase, the elapsed
time, and a cancel button.

Finished results are cached for 30 days in `~/.cache/osm_businesses/web`,
keyed by the area and the chosen categories — so asking for the same place
twice is instant. Delete that directory to force a fresh query. (Resolved area
names use `~/.cache/osm_businesses/areas.json`, shared with the CLI.)

Jobs themselves live in memory. Restarting the server loses any job still
running, but finished results survive in the cache. Cancelling is cooperative:
it stops the page from waiting and marks the job cancelled, but an HTTP request
already in flight is abandoned rather than killed, so its thread may run on to
its own timeout in the background.

### API

| Route | What it does |
|---|---|
| `GET /api/places?q=…&country=RS` | Nominatim candidates, each with its Overpass `area_id` |
| `GET /api/places/{osm_type}/{osm_id}/geometry` | the area's boundary as GeoJSON, for the map |
| `POST /api/jobs` | start a job; body is `{area, categories}` |
| `GET /api/jobs/{id}` | job status: `pending`/`running`/`done`/`error`/`cancelled` |
| `DELETE /api/jobs/{id}` | cancel |
| `GET /api/jobs/{id}/results` | filtered rows, paginated, plus per-category counts |
| `GET /api/jobs/{id}/points` | `[lat, lon]` per filtered row, so the map can plot what the table lists |
| `GET /api/jobs/{id}/export.xlsx` | the same filtered rows as a workbook |
| `POST /api/jobs/{id}/enrich` | start a website-search pass over the prepared rows |
| `GET /api/jobs/{id}/enrich` | how that pass is going |
| `DELETE /api/jobs/{id}/enrich` | stop it |

All three filtering routes take the same query parameters: `categories`
(repeatable), `require_contact`, `website` (`any`, `yes` or `no`), `q`, `sort`
and `order`, plus the three below. `points` is capped at 5000 coordinates and
says so with `truncated`.

`results` also carries `counts`. All but one of them describe the **area**
rather than the filtered table: `area_total`, `area_without_site`,
`found_sites`, `maybe_sites` and `unchecked`. The page headline is "this area
holds N businesses, M of them without a site" — a sentence about the area —
while `total` is what the table is currently showing.

`unchecked_here` is the exception, and follows the filters: it is what the
website search would take on right now, so the button can say how many it will
actually check. Keeping them separate is what lets the page say "588
firmi je u oblasti — filteri ih sve iskljucuju" when a filter empties the table.

### Preparing the rows

Before any of the panel's filters run, `webapp/results.py` decides which rows
are on the table at all. Two steps, both on by default and both switchable from
the filter bar:

- **`collapse`** — one row per business, however many branches of it the area
  holds. `_dedupe` in the parser already merges the repeats OSM produces for a
  *single* POI; three Maxis on three real addresses survive that and are three
  legitimate rows there. For a call list they are one lead, so the richest copy
  is kept and the rest dropped. The key is the folded name plus the category,
  so `Apoteka` the pharmacy and `Apoteka` the cafe stay apart.
- **`commercial_only`** — drops what is on the map but is never a sales lead:
  banks, post offices, exchange offices, insurers, bookmakers, town halls,
  courts, police and fire stations, schools, kindergartens, universities,
  public hospitals and clinics, places of worship, libraries and community
  centres. The full list is `NON_COMMERCIAL` in `webapp/results.py`, matched on
  the `key=value` pair rather than the bare value. Dentists, pharmacies, vets,
  driving schools, lawyers and the rest of the private practices are
  deliberately *not* on it.

This runs before both `filter_rows` and `facets`, so a chain collapsed to one
row also counts as one in the category chips.

### Finding sites the map does not know about

OSM's `website` tag is filled in by whoever mapped the shop, which is usually
not the shop. Plenty of businesses with a perfectly good site therefore land in
the "no website" pile, and calling them is wasted time. `POST
/api/jobs/{id}/enrich` goes and looks: it searches the web for each such
business, and decides whether any result is the business's own site.

The hard part is rejecting, not searching. A search for a Serbian business
returns catalogues (companywall, navidiku, planplus), map mirrors and review
sites long before it returns the business, and every one of those is *titled*
with the business's name. So a result counts only when the **domain** is built
from the name — `trpkovic.rs` for `Pekara Trpkovic` — after the trade word
(`pekara`) and the legal form (`doo`) are stripped off. A domain that merely
appears in a snippet counts too, but only on an exact match. Whatever survives
then has to answer an HTTP request: a name-shaped domain is the easiest thing
in the world to squat or let expire.

Findings land in `found_website`, `found_confidence` (`strong`, `weak`, `none`
or empty for unchecked) and `found_source` (`search`, `mention` or `social`),
never in `website` — what a mapper wrote down beats what a search guessed, and
ODbL covers the OSM half of the sheet and nothing else. A perfect name match on
a foreign TLD is only `weak`: `restoranzlatnik.ba` for a restaurant in Nis is a
Bosnian namesake. Facebook and Instagram profiles are recorded separately; for
a lot of small businesses the Facebook page *is* the website.

The pass is deliberately serial — every engine worth asking rate-limits a burst
and answers a trickle indefinitely — so it costs about two seconds per
business. What keeps it bearable is doing less work: rows that already carry a
site are skipped, so are names that identify nothing (`Pekara` alone would
match some other bakery), findings are cached per business in
`~/.cache/osm_businesses/sites` (60 days for a hit, 21 for a miss), and a pass
is capped at `limit` businesses (default 150) with the next one continuing
where it stopped.

**A pass runs over the rows the panel's filters leave**, not over everything.
A city is around an hour at two seconds apiece, and the queue is otherwise in
`sort_rows` order — alphabetically by category — so a first pass spends two
fifths of itself on artwork, attractions and bus stations while every
restaurant in the area waits for the seventh. Narrowing to a trade and checking
those forty costs nothing in total work, because the cache is keyed by the
business rather than by the job: a shop checked while the table was narrowed to
bakeries stays checked once the filter comes off.

A search that *fails* is recorded as unchecked, not as "no site": caching a
timeout would retire a real lead over a bad minute. Set `hide_found=true` to
drop the confident hits from the table — the counts above it do not move, so
you can still see how many were hidden.

`OSM_SEARCH_BACKEND` picks the engines (default
`duckduckgo,brave,google,yahoo,startpage,mojeek`; breadth is what makes a long
run possible, since any single engine blocks within a handful of queries).

The area in `POST /api/jobs` is one of:

```json
{"kind": "area",   "area_id": 3611538321, "label": "Nis"}
{"kind": "bbox",   "bbox": [43.28, 21.85, 43.36, 21.96]}
{"kind": "circle", "center": [43.32, 21.90], "radius_m": 3000}
```

An unusable area is a `422` with a readable message, not a traceback. No
traceback ever reaches the browser: the server log gets the exception, the
user gets a sentence.

### The workbook

Two sheets. **Firme** holds the same columns as the CSV, with Serbian headers,
a frozen header row, an autofilter, clickable website and email cells, and
phone numbers stored as text so Excel keeps the leading `+`. **Info** records
the area, the date, the row count, the filters applied, how many sites the web
search contributed, and the ODbL attribution.

Three extra columns sit after the OSM ones — `Sajt (pretraga)`, `Pouzdanost`
and `Odakle` — carrying what the website search found. They are kept separate
and labelled rather than folded into `Sajt` on purpose: they did not come from
the map, ODbL does not cover them, and a row marked `za proveru` is the user's
call to make.

That attribution is not decoration. Anything you publish from these
spreadsheets carries the same obligation as the CLI's CSVs — see
[Attribution](#attribution) at the end of this file.

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

Two shapes of repeat are collapsed, in both cases keeping whichever row has
more non-empty fields (on a tie, the node wins):

- **One POI mapped twice** — usually a node *and* the building way around it.
  Same name within 50 m counts as one business; when the two carry different
  categories (a cafe also tagged `shop=convenience`) the radius tightens to 15 m,
  because the name alone is weaker evidence: "Tvrdjava" the bakery and
  "Tvrdjava" the pharmacy 50 m apart are two businesses.
- **Rows nothing can tell apart** — same name and the same value in every
  actionable column (category, address, phone, website, hours). Distance is
  irrelevant here: three `Erste Bank` rows with no address and the same central
  number are one line in an export. Branches that carry their own address or
  number differ in a column that matters, so they survive — the nine `Benu`
  pharmacies in Nis stay nine rows.

On real extracts this removes about 5% of rows.

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

275 tests, no network anywhere — the HTTP layer is mocked, the search engine is
injected, and every fixture is hand-built.

The CLI's 78: `test_parse.py` covers the pure `parse_elements` function
(coordinates, name fallbacks, furniture exclusion, dedupe, phone and website
normalization); `test_geo.py` covers area resolution (candidate filtering, the
area-id arithmetic, `--pick`, the cache, and the rate limiter).

The web app's 197: `test_webapp_models.py` (area validation and the translation
into `AreaSpec`), `test_webapp_cache.py` (the key, the round trip, and every
way an entry can be rejected), `test_webapp_jobs.py` (the job state machine and
cancellation), `test_webapp_runner.py` (cache hit, query shape, Overpass
failure), `test_webapp_results.py` (the blocklist, chain collapsing, filters,
facets, sorting, pagination), `test_webapp_export.py` (the workbook, read back
with `openpyxl`), `test_webapp_nominatim.py` (the `/lookup` client) and
`test_webapp_api.py` (every route, through `TestClient`, with Nominatim,
Overpass and the website search stubbed — including that a pass takes on only
what the filters leave).

The website search's own 58: `test_webapp_enrich.py` drives the whole decision
path with canned search results — which domains are accepted, which directories
are rejected and why, the foreign-TLD downgrade, social profiles, dead domains,
and a failed search staying *unchecked* rather than becoming a miss.
`test_webapp_enrich_runner.py` covers a pass: what it skips, the cap and
continuing, cancellation, and one broken row not losing the rest.
`test_webapp_site_cache.py` covers the key, the two expiry rates, and surviving
a corrupt shard or an unwritable disk.

The frontend has no automated tests; it is checked by hand.

## Attribution

Data © OpenStreetMap contributors, available under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/).
See <https://www.openstreetmap.org/copyright>.

Anything you publish from this tool's output must carry that attribution and
respect the ODbL's share-alike terms. Both Nominatim and the public Overpass
instances are volunteer-funded: keep requests modest, cache results (this tool
caches resolved areas for you), and do not remove the rate limiting.
