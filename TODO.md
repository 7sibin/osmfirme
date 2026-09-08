# TODO

## Enhancers — enrich rows from sources beyond OSM

**Two of them are built.** Website discovery ships as `webapp/enrich.py` +
`webapp/enrich_runner.py`, and reading those sites for an email ships as
`webapp/contacts.py` + `webapp/contact_runner.py`. See "Finding sites the map
does not know about" and "Reading those sites for an email" in the README. What
follows is the rest of the idea, and the decisions it is still waiting on.

Answers the first enhancer settled, which the next one should reuse:

- It runs **on demand**, not automatically, and over the *prepared* rows — no
  point searching for a branch that was collapsed away.
- Enriched fields are their own columns, in the API and in the workbook, so it
  stays obvious which half of the sheet is ODbL and which is not.
- Its cache is separate from the Overpass one, keyed by the business rather
  than the area, with a shorter life for a miss than for a hit.
- A failed lookup is **unchecked**, never a negative answer.

### Where it plugs in

The pipeline in `webapp/runner.py` is linear:

```
area -> build_query -> OverpassClient.fetch -> parse_elements -> sort_rows -> cache.save
```

The website search sits *after* the job instead, as a second pass over a
finished job's prepared rows, reached through `POST /api/jobs/{id}/enrich`.
That turned out to be the right shape: the scrape stays fast, the pass is
cancellable on its own, and re-running an area does not force a re-search.
A future enhancer should copy it rather than the plan below.

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
| ~~Scrape the business's own site~~ | **done** — email, extra phone, socials, and whether the domain still answers | only helps rows that have a `website`, including one the search found; misses an address that is only in an image or behind a form |
| ~~Find a site/socials for rows without one~~ | **done** — `ddgs` over several engines, domain-must-match-the-name, plus a liveness check | slow (~2 s per business) and precision-first, so it misses sites whose domain is not built from the name |
| Official registries (APR, NBS) | PIB, matični broj, activity code, status (active / in liquidation), registered address | no public API; matching by name and address is unreliable |
| Google Places | rating, review count, phone, website, opening hours | paid per call, and the README currently promises "OSM only. No Google Maps fallback" — adopting this is a deliberate break with that |

### Open questions

1. Which of the remaining sources above are actually wanted (see the table).
2. Whether the website search should try harder for the businesses it misses -
   a name whose domain is not built from it (`Zlatni Papagaj` at `zp.rs`) is
   invisible to the current rule, and loosening the rule is what let the
   directories back in.
3. Whether a paid search API is worth it, now that the free engines cost about
   two seconds each and a city is thousands of businesses.
4. ~~Whether the dead sites the reading pass turns up deserve their own view.~~
   **Done** — `website=dead`, reached by clicking the dead count in the reading
   line.
