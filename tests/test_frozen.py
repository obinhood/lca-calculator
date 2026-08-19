"""The shared parsers for frozen run state.

Every renderer reads JSON blobs the engine froze onto a run. Parsing them bare
meant one malformed row raised and took down every report for that organisation.
These parse defensively — and `corrupt_details` is the counterpart that keeps the
softness honest.
"""
import pytest

from app.models import (
    ActivityRecord, EmissionFactor, EmissionLineItem, Organisation,
)
from app.services.calc import compute_co2e
from app.services.frozen import (
    corrupt_details, parse_detail, parse_list, parse_optional,
)


@pytest.mark.parametrize("raw", [
    None, "", "{not json", "[]", "null", "7", '"a string"', 7, [], object(),
])
def test_parse_detail_always_returns_a_dict(raw):
    assert parse_detail(raw) == {}


def test_parse_detail_returns_a_real_object():
    assert parse_detail('{"factor_id": 7}') == {"factor_id": 7}


@pytest.mark.parametrize("raw", [None, "", "{not json", "{}", "null", "7"])
def test_parse_list_always_returns_a_list(raw):
    assert parse_list(raw) == []


def test_parse_list_returns_a_real_list():
    assert parse_list('[{"a": 1}]') == [{"a": 1}]


@pytest.mark.parametrize("raw", [None, "", "{not json", "[]", "null"])
def test_parse_optional_returns_none_for_absent_or_unusable(raw):
    assert parse_optional(raw) is None


def test_parse_optional_distinguishes_absent_from_empty():
    """None means 'not present'; {} means 'present and empty'. Callers test both."""
    assert parse_optional(None) is None
    assert parse_optional("{}") == {}


def test_corrupt_details_counts_and_names_the_bad_lines(db):
    org = Organisation(name="FrozenOrg")
    db.add(org); db.commit(); db.refresh(org)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.2)
    db.add(f); db.commit(); db.refresh(f)
    for i in range(4):
        db.add(ActivityRecord(organisation_id=org.id, date="2024-06-01",
                              category="electricity", subcategory="", description="",
                              quantity=100.0 + i, unit="kWh", geo="GB",
                              factor_id=f.id, mapping_basis="exact"))
    db.commit()
    run = compute_co2e(db, org.id)

    assert corrupt_details(db, run.id)["clean"] is True

    lines = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).order_by(EmissionLineItem.id).all()
    lines[0].details = "{broken"
    db.commit()

    out = corrupt_details(db, run.id)
    assert out["clean"] is False
    assert out["lines_unreadable"] == 1
    assert out["line_ids"] == [lines[0].id]
    assert out["lines_total"] == len(lines)
    assert "factor lineage" in out["note"]
