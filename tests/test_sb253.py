import pytest

from app.models import EmissionFactor, ActivityRecord, Organisation, MarketInstrument
from app.services.calc import compute_co2e
from app.reports.sb253 import sb253_report


def _org(db, name="DemoOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, geo="GB", subcategory=""):
    f = EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography=geo, year=2024,
                       category=category, subcategory=subcategory, unit=unit,
                       gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category, quantity, unit):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


@pytest.fixture
def seeded(db):
    """Sample-equivalent org: electricity 1200 kWh, gas 800 kWh, diesel 150 L."""
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id,
              "electricity", 1200, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 800, "kWh")
    _activity(db, org.id, _factor(db, "diesel", "L", 2.676).id, "diesel", 150, "L")
    run = compute_co2e(db, org.id)
    return org, run


def test_sb253_golden_values_with_assurance(db, seeded):
    org, run = seeded
    r = sb253_report(db, org.id, run_id=run.id,
                     assurance_level="limited", assurance_provider="Example LLP")
    e = r["emissions_tco2e"]
    assert e["scope1"] == pytest.approx(0.5486)
    assert e["scope2_location_based"] == pytest.approx(0.204)
    assert e["scope2_market_based"] == pytest.approx(0.204)
    assert r["assurance"]["level"] == "limited"
    assert r["filing_ready"] is True
    assert r["blockers"] == []
    assert "SB 253" in r["methodology_statement"]
    assert "GHG Protocol" in r["methodology_statement"]


def test_sb253_blocks_without_assurance(db, seeded):
    org, run = seeded
    r = sb253_report(db, org.id, run_id=run.id)          # assurance_level="none"
    assert r["filing_ready"] is False
    assert any("assurance" in b.lower() for b in r["blockers"])


def test_sb253_rejects_bad_assurance_level(db, seeded):
    org, run = seeded
    r = sb253_report(db, org.id, run_id=run.id, assurance_level="pinky_promise")
    assert r["filing_ready"] is False
    assert any("assurance_level must be" in b for b in r["blockers"])


def test_sb253_blocks_partial_run(db, seeded):
    org, _ = seeded
    _activity(db, org.id, None, "widgets", 5, "kg")      # unmapped
    run = compute_co2e(db, org.id)
    r = sb253_report(db, org.id, run_id=run.id, assurance_level="limited")
    assert r["filing_ready"] is False
    assert any("PARTIAL" in b for b in r["blockers"])


def test_sb253_market_scope2_reflects_rec(db, seeded):
    org, _ = seeded
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=1200.0,
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    r = sb253_report(db, org.id, run_id=run.id, assurance_level="limited")
    e = r["emissions_tco2e"]
    assert e["scope2_location_based"] == pytest.approx(0.204)
    assert e["scope2_market_based"] == pytest.approx(0.0)
    assert r["scope2_market_disclosure"]["kwh_contractual"] == pytest.approx(1200.0)


def test_sb253_no_run_yet(db):
    org = _org(db)
    r = sb253_report(db, org.id, assurance_level="limited")
    assert r["filing_ready"] is False
    assert any("no calculation run" in b for b in r["blockers"])
