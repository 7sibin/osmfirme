"""Request models. Validation happens here so every route inherits it."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from osm_businesses import (
    QUERY_KEYS,
    AreaSpec,
    area_spec_from_area_id,
    area_spec_from_bbox,
    area_spec_from_center,
)

MIN_RADIUS_M = 50.0
MAX_RADIUS_M = 50_000.0


class AreaInput(BaseModel):
    """One of three ways the browser can describe an area."""

    kind: Literal["area", "bbox", "circle"]
    area_id: int | None = Field(default=None, gt=0)
    label: str = ""
    bbox: list[float] | None = None
    center: list[float] | None = None
    radius_m: float | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> AreaInput:
        if self.kind == "area":
            if self.area_id is None:
                raise ValueError("kind 'area' requires area_id")
        elif self.kind == "bbox":
            self._check_bbox()
        else:
            self._check_circle()
        return self

    def _check_bbox(self) -> None:
        if self.bbox is None or len(self.bbox) != 4:
            raise ValueError("kind 'bbox' requires four numbers: south, west, north, east")
        south, west, north, east = self.bbox
        if not (-90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("latitude must be between -90 and 90")
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("longitude must be between -180 and 180")
        if south >= north:
            raise ValueError("south must be smaller than north")
        if west >= east:
            raise ValueError("west must be smaller than east")

    def _check_circle(self) -> None:
        if self.center is None or len(self.center) != 2:
            raise ValueError("kind 'circle' requires center as [lat, lon]")
        lat, lon = self.center
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("center is out of range")
        if self.radius_m is None or not MIN_RADIUS_M <= self.radius_m <= MAX_RADIUS_M:
            raise ValueError(f"radius_m must be between {MIN_RADIUS_M:g} and {MAX_RADIUS_M:g}")

    def to_spec(self) -> AreaSpec:
        """Translate into the AreaSpec the existing query builder understands."""
        if self.kind == "area":
            assert self.area_id is not None  # guaranteed by the validator
            return area_spec_from_area_id(self.area_id)
        if self.kind == "bbox":
            assert self.bbox is not None
            return area_spec_from_bbox(",".join(f"{value:g}" for value in self.bbox))
        assert self.center is not None and self.radius_m is not None
        center = f"{self.center[0]:g},{self.center[1]:g}"
        return area_spec_from_center(center, f"{self.radius_m:g}")

    def display_label(self) -> str:
        return self.label.strip() or self.to_spec().description

    def cache_payload(self) -> dict[str, Any]:
        """What identifies this area for caching. The label is cosmetic, so it is left out."""
        if self.kind == "area":
            return {"kind": "area", "area_id": self.area_id}
        if self.kind == "bbox":
            return {"kind": "bbox", "bbox": self.bbox}
        return {"kind": "circle", "center": self.center, "radius_m": self.radius_m}


class JobRequest(BaseModel):
    area: AreaInput
    categories: list[str] = Field(default_factory=lambda: list(QUERY_KEYS))

    @model_validator(mode="after")
    def _check_categories(self) -> JobRequest:
        unknown = sorted(set(self.categories) - set(QUERY_KEYS))
        if unknown:
            raise ValueError(f"unknown categories: {', '.join(unknown)}")
        if not self.categories:
            raise ValueError("pick at least one category")
        return self

    def ordered_categories(self) -> tuple[str, ...]:
        """QUERY_KEYS order, so the generated Overpass query is stable."""
        wanted = set(self.categories)
        return tuple(key for key in QUERY_KEYS if key in wanted)
