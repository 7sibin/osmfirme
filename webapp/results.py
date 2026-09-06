"""Filtering, faceting, sorting and pagination over a finished job's rows.

Pure functions: no I/O, no globals. The frontend calls these through
/api/jobs/{id}/results on every filter change, so they run on every keystroke
of the search box and must stay cheap.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from osm_businesses import COLUMNS, Row

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
SEARCHABLE = ("name", "street", "place", "category")


@dataclass
class ResultFilters:
    categories: list[str] = field(default_factory=list)
    require_contact: bool = False
    q: str = ""
    sort: str = "name"
    order: str = "asc"


def _matches_text(row: Row, needle: str) -> bool:
    return any(needle in str(getattr(row, column, "")).lower() for column in SEARCHABLE)


def filter_rows(rows: list[Row], filters: ResultFilters) -> list[Row]:
    kept = rows
    if filters.require_contact:
        kept = [row for row in kept if row.has_contact]
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
        categories=[], require_contact=filters.require_contact, q=filters.q,
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
