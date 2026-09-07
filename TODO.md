# TODO

## Enhancers — enrich rows from sources beyond OSM

Add a stage that takes the parsed rows and fills in what OSM does not carry.
Not designed yet; this is the captured idea plus the decisions it is waiting on.

### Where it plugs in

The pipeline in `webapp/runner.py` is linear:

```
area -> build_query -> OverpassClient.fetch -> parse_elements -> sort_rows -> cache.save
```

An enhancer stage sits between `sort_rows` and `cache.save`: it takes
`list[Row]`, fetches per-business data from elsewhere, and returns rows with
extra fields.

Everything that stage touches:

- `Row` (`osm_businesses.py:111`) is a frozen dataclass over a fixed `COLUMNS`
  tuple. New fields have to flow through all four consumers: the disk cache
  (`webapp/cache.py`, incl. a `CACHE_VERSION` bump), filters and facets
  (`webapp/results.py`), the workbook (`webapp/export.py`) and the results
  table in `webapp/static/`.
- A city-sized area is thousands of rows, so this needs its own concurrency
  limit and rate limiting — one request per business, not one per job.
- It needs a **separate cache** from the Overpass one. Overpass results are
  good for 30 days; a scraped email or a live-site check is not, and enriching
  should not force a re-query of the map data (or vice versa).
- A new `JobPhase` (`webapp/jobs.py`) so the page can say what it is doing, and
  it must stay cancellable — cancellation is cooperative today.
- Partial failure is normal: one business's site being down must not fail the
  job. Per-row status, not an exception.

### Candidate sources (undecided — pick before designing)

| Source | What it adds | Catch |
|---|---|---|
| Scrape the business's own site | email, extra phones, Facebook/Instagram links, and whether the domain is even alive (a dead site is still a lead) | only helps rows that already have a `website`; every site is shaped differently |
| Find a site/socials for rows without one | the highest-value column for outreach, since the default view is businesses with no website | needs a paid search API (Brave/Bing/SerpAPI) or scraping search results, which is against ToS and gets blocked |
| Official registries (APR, NBS) | PIB, matični broj, activity code, status (active / in liquidation), registered address | no public API; matching by name and address is unreliable |
| Google Places | rating, review count, phone, website, opening hours | paid per call, and the README currently promises "OSM only. No Google Maps fallback" — adopting this is a deliberate break with that |

### Open questions

1. Which of the sources above are actually wanted (see the table).
2. Enrich every row of a job automatically, or only on demand for a selected
   subset (cheaper, and keeps the job fast)?
3. Are the enriched fields exported and filterable like the OSM ones, or shown
   as a separate group so it stays obvious which data is ODbL and which is not?
4. Provenance: does a scraped email need to record where it came from, given
   the ODbL attribution rules only cover the OSM half?
