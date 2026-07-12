import pytest

from app.models import EmissionFactor, ActivityRecord, Organisation, MarketInstrument
from app.services.calc import compute_co2e
from app.reports.secr import secr_report, DIESEL_KWH_PER_LITRE_DEMO


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
    f_el = _factor(db, "electricity", "kWh", 0.170)
    f_gas = _factor(db, "gas", "kWh", 0.184)
    f_d = _factor(db, "diesel", "L", 2.676)
    _activity(db, org.id, f_el.id, "electricity", 1200, "kWh")
    _activity(db, org.id, f_gas.id, "gas", 800, "kWh")
    _activity(db, org.id, f_d.id, "diesel", 150, "L")
    run = compute_co2e(db, org.id)
    return org, run


def test_secr_golden_values(db, seeded):
    org, run = seeded
    r = secr_report(db, org.id, run_id=run.id,
                    intensity_denominator=2.0, intensity_denominator_unit="£M revenue")
    e = r["emissions_tco2e"]
    # Scope 1: gas 800*0.184 + diesel 150*2.676 = 147.2 + 401.4 = 548.6 kg
    assert e["scope1"] == pytest.approx(0.5486)
    # Scope 2: electricity 1200*0.170 = 204 kg (dual-reported, no instrument)
    assert e["scope2_location_based"] == pytest.approx(0.204)
    assert e["scope2_market_based"] == pytest.approx(0.204)
    assert e["scope1_and_2_location"] == pytest.approx(0.7526)
    # Energy: 1200 + 800 + 150 L * demo constant
    en = r["energy_use_kwh"]
    assert en["electricity"] == pytest.approx(1200.0)
    assert en["gas"] == pytest.approx(800.0)
    assert en["diesel"] == pytest.approx(150 * DIESEL_KWH_PER_LITRE_DEMO)
    assert en["total_kwh"] == pytest.approx(2000 + 1500)
    # Intensity: 0.7526 t / 2 = 0.3763 t per £M
    assert r["intensity_ratio"]["tco2e_scope1_and_2_location"] == pytest.approx(0.3763)
    assert r["disclosure_ready"] is True
    assert r["blockers"] == []
    assert "GHG Protocol" in r["methodology_statement"]
    assert "run #" + str(run.id) in r["methodology_statement"]


def test_secr_blocks_without_intensity_ratio(db, seeded):
    org, run = seeded
    r = secr_report(db, org.id, run_id=run.id)
    assert r["disclosure_ready"] is False
    assert any("intensity ratio" in b for b in r["blockers"])


def test_secr_blocks_partial_run(db, seeded):
    org, run0 = seeded
    _activity(db, org.id, None, "widgets", 5, "kg")     # unmapped
    run = compute_co2e(db, org.id)
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0)
    assert r["disclosure_ready"] is False
    assert any("PARTIAL" in b for b in r["blockers"])
    assert any("coverage" in b for b in r["blockers"])


def test_secr_blocks_stale_run(db, seeded):
    org, run = seeded
    f_el = db.query(EmissionFactor).filter(EmissionFactor.category == "electricity").first()
    _activity(db, org.id, f_el.id, "electricity", 999, "kWh")   # added AFTER run
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0)
    assert r["disclosure_ready"] is False
    assert any("STALE" in b for b in r["blockers"])


def test_secr_market_based_reflects_rec(db, seeded):
    org, _ = seeded
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=1200.0,
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0)
    e = r["emissions_tco2e"]
    assert e["scope2_location_based"] == pytest.approx(0.204)
    assert e["scope2_market_based"] == pytest.approx(0.0)       # REC-covered
    assert e["total_market_based"] == pytest.approx(0.5486)     # scope 1 only


def test_secr_no_run_yet(db):
    org = _org(db)
    r = secr_report(db, org.id)
    assert r["disclosure_ready"] is False
    assert any("no calculation run" in b for b in r["blockers"])
