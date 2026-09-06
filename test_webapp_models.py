from __future__ import annotations

import pytest
from pydantic import ValidationError

from webapp.models import AreaInput, JobRequest


def test_area_kind_builds_area_selector():
    area = AreaInput(kind="area", area_id=3611538321, label="Nis")
    spec = area.to_spec()
    assert spec.kind == "area"
    assert "area(3611538321)->.searchArea;" == spec.preamble


def test_bbox_kind_builds_bbox_selector():
    area = AreaInput(kind="bbox", bbox=[43.28, 21.85, 43.36, 21.96])
    spec = area.to_spec()
    assert spec.kind == "bbox"
    assert spec.selector == "(43.28,21.85,43.36,21.96)"


def test_circle_kind_builds_around_selector():
    area = AreaInput(kind="circle", center=[43.32, 21.90], radius_m=3000)
    spec = area.to_spec()
    assert spec.kind == "around"
    assert spec.selector == "(around:3000,43.32,21.9)"


def test_area_kind_requires_area_id():
    with pytest.raises(ValidationError):
        AreaInput(kind="area")


def test_bbox_requires_four_ordered_numbers():
    with pytest.raises(ValidationError):
        AreaInput(kind="bbox", bbox=[43.36, 21.85, 43.28, 21.96])  # south > north
    with pytest.raises(ValidationError):
        AreaInput(kind="bbox", bbox=[43.28, 21.85, 43.36])


def test_circle_radius_is_bounded():
    with pytest.raises(ValidationError):
        AreaInput(kind="circle", center=[43.32, 21.90], radius_m=10)
    with pytest.raises(ValidationError):
        AreaInput(kind="circle", center=[43.32, 21.90], radius_m=99999)


def test_display_label_falls_back_to_the_spec_description():
    assert AreaInput(kind="area", area_id=42, label="Nis").display_label() == "Nis"
    assert "bounding box" in AreaInput(kind="bbox", bbox=[1.0, 2.0, 3.0, 4.0]).display_label()


def test_cache_payload_ignores_the_label():
    a = AreaInput(kind="area", area_id=42, label="Nis")
    b = AreaInput(kind="area", area_id=42, label="Ниш")
    assert a.cache_payload() == b.cache_payload()


def test_job_request_defaults_to_all_query_keys():
    request = JobRequest(area=AreaInput(kind="area", area_id=42))
    assert set(request.categories) == {"shop", "amenity", "office", "craft", "tourism", "healthcare"}


def test_job_request_rejects_unknown_category():
    with pytest.raises(ValidationError):
        JobRequest(area=AreaInput(kind="area", area_id=42), categories=["nonsense"])
