"""Turn a filtered list of rows into an .xlsx workbook in memory."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from osm_businesses import COLUMNS, Row

from webapp.results import ResultFilters

HEADERS_SR: dict[str, str] = {
    "osm_type": "OSM tip",
    "osm_id": "OSM ID",
    "name": "Naziv",
    "category": "Kategorija",
    "place": "Mesto",
    "street": "Ulica",
    "housenumber": "Broj",
    "postcode": "Postanski broj",
    "phone": "Telefon",
    "phone_alt": "Drugi telefon",
    "website": "Sajt",
    "email": "Email",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "opening_hours": "Radno vreme",
    "lat": "Geo. sirina",
    "lon": "Geo. duzina",
}

ATTRIBUTION = (
    "Podaci (c) OpenStreetMap contributors, ODbL. "
    "https://www.openstreetmap.org/copyright"
)

MAX_COLUMN_WIDTH = 45
TEXT_COLUMNS = ("phone", "phone_alt", "postcode", "housenumber")
LINK_FONT = Font(color="0563C1", underline="single")


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or "oblast"


def export_filename(area_label: str) -> str:
    return f"firme-{_slug(area_label)}-{date.today().isoformat()}.xlsx"


def build_workbook(rows: list[Row], *, area_label: str, filters: ResultFilters) -> BytesIO:
    book = Workbook()
    sheet = book.active
    sheet.title = "Firme"

    for index, column in enumerate(COLUMNS, start=1):
        cell = sheet.cell(row=1, column=index, value=HEADERS_SR[column])
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center")

    for row_index, row in enumerate(rows, start=2):
        values = row.as_output_dict()
        for column_index, column in enumerate(COLUMNS, start=1):
            value = values[column]
            cell = sheet.cell(row=row_index, column=column_index)
            if column in ("lat", "lon"):
                cell.value = float(value)
                cell.number_format = "0.0000000"
            elif column in TEXT_COLUMNS:
                cell.value = str(value)
                cell.number_format = "@"  # text, so Excel keeps the leading +
            else:
                cell.value = value
            if value and column == "website":
                cell.hyperlink = str(value)
                cell.font = LINK_FONT
            elif value and column == "email":
                cell.hyperlink = f"mailto:{value}"
                cell.font = LINK_FONT

    sheet.freeze_panes = "A2"
    last_column = get_column_letter(len(COLUMNS))
    sheet.auto_filter.ref = f"A1:{last_column}{max(1, len(rows) + 1)}"
    _fit_columns(sheet, rows)

    _write_info_sheet(book, rows=rows, area_label=area_label, filters=filters)

    buffer = BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer


def _fit_columns(sheet, rows: list[Row]) -> None:
    for index, column in enumerate(COLUMNS, start=1):
        widest = len(HEADERS_SR[column])
        for row in rows:
            widest = max(widest, len(str(getattr(row, column, ""))))
        sheet.column_dimensions[get_column_letter(index)].width = min(widest + 2, MAX_COLUMN_WIDTH)


def _write_info_sheet(book: Workbook, *, rows: list[Row], area_label: str, filters: ResultFilters) -> None:
    info = book.create_sheet("Info")
    applied = []
    if filters.categories:
        applied.append("kategorije: " + ", ".join(sorted(filters.categories)))
    if filters.require_contact:
        applied.append("samo sa kontaktom")
    if filters.q.strip():
        applied.append(f"pretraga: {filters.q.strip()}")

    lines: list[tuple[str, object]] = [
        ("Oblast", area_label),
        ("Datum", date.today().isoformat()),
        ("Broj firmi", len(rows)),
        ("Filteri", "; ".join(applied) or "bez filtera"),
        ("Izvor", ATTRIBUTION),
    ]
    for row_index, (label, value) in enumerate(lines, start=1):
        info.cell(row=row_index, column=1, value=label).font = Font(bold=True)
        info.cell(row=row_index, column=2, value=value)
    info.column_dimensions["A"].width = 16
    info.column_dimensions["B"].width = 80
