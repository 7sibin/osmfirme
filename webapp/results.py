"""Preparing, filtering, faceting, sorting and paginating a finished job's rows.

Two stages, in this order:

`prepare_rows` decides which rows are on the table at all - it drops what is
not a sales lead and collapses a chain to one row. It runs once per request,
before both `filter_rows` and `facets`, so a chain that collapsed to one row
also counts as one in the category counts.

`filter_rows` then applies the filters the user set in the panel.

Pure functions: no I/O, no globals. The frontend calls these through
/api/jobs/{id}/results on every filter change, so they run on every keystroke
of the search box and must stay cheap.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from osm_businesses import COLUMNS, Row, fold_text, is_richer

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
SEARCHABLE = ("name", "street", "place", "category")


#: Accepted values for ResultFilters.website. Anything else means "any".
WEBSITE_YES = "yes"
WEBSITE_NO = "no"
WEBSITE_DEAD = "dead"
""""dead" keeps only rows whose site was read and turned out not to answer."""


@dataclass
class ResultFilters:
    categories: list[str] = field(default_factory=list)
    require_contact: bool = False
    website: str = "any"
    """"yes" keeps only rows that have a website, "no" only those that don't."""
    q: str = ""
    sort: str = "name"
    order: str = "asc"
    commercial_only: bool = True
    """Drop banks, post offices and public institutions - see NON_COMMERCIAL."""
    collapse: bool = True
    """Collapse a chain's branches to one row."""
    hide_found: bool = False
    """Drop rows the website search confidently turned up a site for."""


#: Confidence values on Row.found_confidence. An empty string means the row was
#: never checked, which is different from FOUND_NONE: checked, nothing found.
FOUND_STRONG = "strong"
FOUND_WEAK = "weak"
FOUND_NONE = "none"

#: Values for Row.contact_status - how reading the business's own site went.
#: Empty means it was never read, which is different from CONTACT_NONE.
CONTACT_OK = "ok"
CONTACT_NONE = "none"
CONTACT_DEAD = "dead"
"""Nothing answered at that address at all. A fact about the business, not the
fetch: their site is gone, and a business that had one and lost it is a lead
again. A bot filter or an unreadable page is CONTACT_NONE, never this."""

#: `key=value` pairs that are on the map but are never a sales lead: a branch of
#: an institution with no local decision to make, or a public body. Matched on
#: the pair rather than the bare value, so `office=insurance` goes while a shop
#: that happened to tag `shop=insurance` would not.
#:
#: Deliberately absent: dentist, pharmacy, veterinary, driving_school, lawyer,
#: notary and the rest of the private practices, which are exactly the leads
#: this tool exists to find.
NON_COMMERCIAL: frozenset[str] = frozenset(
    {
        # post and money
        "amenity=post_office",
        "amenity=post_depot",
        "amenity=bank",
        "amenity=bureau_de_change",
        "amenity=money_transfer",
        "amenity=gambling",
        "office=insurance",
        "office=financial",
        "shop=bookmaker",
        # government and public order
        "amenity=townhall",
        "amenity=courthouse",
        "amenity=police",
        "amenity=fire_station",
        "amenity=prison",
        "amenity=embassy",
        "amenity=public_building",
        "office=government",
        "office=administrative",
        "office=diplomatic",
        "office=political_party",
        # education
        "amenity=school",
        "amenity=kindergarten",
        "amenity=college",
        "amenity=university",
        # health, the public half of it
        "amenity=hospital",
        "amenity=clinic",
        "amenity=doctors",
        "healthcare=hospital",
        # community
        "amenity=place_of_worship",
        "amenity=library",
        "amenity=community_centre",
        "amenity=social_facility",
    }
)


def is_non_commercial(row: Row) -> bool:
    return f"{row.category_key}={row.category}" in NON_COMMERCIAL


def drop_non_commercial(rows: list[Row]) -> list[Row]:
    return [row for row in rows if not is_non_commercial(row)]


def chain_key(row: Row) -> tuple[str, str]:
    """What makes two rows the same business.

    The category is part of the key on purpose: `Apoteka` the pharmacy and
    `Apoteka` the cafe are two businesses that share a word, not one chain.
    """
    return (fold_text(row.name), row.category)


def collapse_chains(rows: list[Row]) -> list[Row]:
    """One row per business, however many branches of it the area holds.

    `_dedupe` in the parser already merges the repeats OSM produces for a single
    POI, but three Maxis on three real addresses are three legitimate rows there
    and survive it. For a call list they are one lead, so this keeps the richest
    copy and drops the rest. First-appearance order is preserved; sorting is a
    later stage's job.
    """
    kept: list[Row] = []
    index_of: dict[tuple[str, str], int] = {}
    for row in rows:
        key = chain_key(row)
        seen = index_of.get(key)
        if seen is None:
            index_of[key] = len(kept)
            kept.append(row)
        elif is_richer(row, kept[seen]):
            kept[seen] = row
    return kept


def drop_found(rows: list[Row]) -> list[Row]:
    """Drop rows the website search confidently turned up a site for.

    Only `strong` hits: a weak one is a candidate to eyeball, not a reason to
    lose the lead.
    """
    return [row for row in rows if row.found_confidence != FOUND_STRONG]


def prepare_rows(rows: list[Row], filters: ResultFilters) -> list[Row]:
    """Which rows are on the table at all, before any of the panel's filters.

    Order matters: drop the non-leads first so they cannot become the kept copy
    of a chain, collapse next, and judge what the search found last - by then
    each business is a single row.
    """
    prepared = rows
    if filters.commercial_only:
        prepared = drop_non_commercial(prepared)
    if filters.collapse:
        prepared = collapse_chains(prepared)
    if filters.hide_found:
        prepared = drop_found(prepared)
    return prepared


def _matches_text(row: Row, needle: str) -> bool:
    return any(needle in str(getattr(row, column, "")).lower() for column in SEARCHABLE)


def filter_rows(rows: list[Row], filters: ResultFilters) -> list[Row]:
    kept = rows
    if filters.require_contact:
        kept = [row for row in kept if row.has_contact]
    if filters.website == WEBSITE_YES:
        kept = [row for row in kept if row.website]
    elif filters.website == WEBSITE_NO:
        kept = [row for row in kept if not row.website]
    elif filters.website == WEBSITE_DEAD:
        kept = [row for row in kept if row.contact_status == CONTACT_DEAD]
    needle = filters.q.strip().lower()
    if needle:
        kept = [row for row in kept if _matches_text(row, needle)]
    if filters.categories:
        wanted = set(filters.categories)
        kept = [row for row in kept if row.category in wanted]
    return kept


def facets(rows: list[Row], filters: ResultFilters) -> list[dict[str, Any]]:
    """Counts per fine category, with the category filter itself left out.

    Leaving it out is deliberate: if ticking `bakery` removed `cafe` from the
    list, the user could never tick a second box.
    """
    without_categories = ResultFilters(
        categories=[],
        require_contact=filters.require_contact,
        website=filters.website,
        q=filters.q,
    )
    counts = Counter(row.category for row in filter_rows(rows, without_categories) if row.category)
    return [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def sort_filtered(rows: list[Row], sort: str, order: str) -> list[Row]:
    column = sort if sort in COLUMNS else "name"

    def key(row: Row) -> Any:
        value = getattr(row, column, "")
        return value if isinstance(value, (int, float)) else str(value).lower()

    return sorted(rows, key=key, reverse=(order == "desc"))


def paginate(rows: list[Row], page: int, page_size: int) -> tuple[list[Row], int]:
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    start = (page - 1) * page_size
    return rows[start : start + page_size], len(rows)
