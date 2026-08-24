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
def unscreened(db):
    """Sample-equivalent org, computed WITHOUT a Scope 3 screen or a resolved boundary."""
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id,
              "electricity", 1200, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 800, "kWh")
    _activity(db, org.id, _factor(db, "diesel", "L", 2.676).id, "diesel", 150, "L")
    # Period-scoped so the comparison below isolates what it claims to test: this fixture
    # withholds the Scope 3 screen and the boundary, not the reporting period. Leaving the
    # run unscoped would add a SECR-only intensity blocker SB 253 has no equivalent of.
    from app.models import ReportingPeriod
    p = ReportingPeriod(organisation_id=org.id, label="FY25", start_date="2025-01-01",
                        end_date="2025-12-31", frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    return org, run


@pytest.fixture
def seeded(db):
    """electricity 1200 kWh, gas 800 kWh, diesel 150 L, waste 250 kg (Scope 3, Cat 5).

    § 38532 requires Scope 3 per the GHG Protocol, so a filing-ready SB 253 run is a
    period-scoped, fully-screened one — the same fixture shape as ESRS E1 and IFRS S2."""
    from tests.scope3_util import ready_run
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id,
              "electricity", 1200, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 800, "kWh")
    _activity(db, org.id, _factor(db, "diesel", "L", 2.676).id, "diesel", 150, "L")
    waste = _activity(db, org.id, _factor(db, "waste", "kg", 0.480).id, "waste", 250, "kg")
    waste.ghgp_category = 5
    db.commit()
    run, _period = ready_run(db, org.id)
    return org, run


def test_sb253_golden_values_with_assurance(db, seeded):
    org, run = seeded
    r = sb253_report(db, org.id, run_id=run.id,
                     assurance_level="limited", assurance_provider="Example LLP")
    e = r["emissions_tco2e"]
    assert e["scope1"] == pytest.approx(0.5486)
    assert e["scope2_location_based"] == pytest.approx(0.204)
    assert e["scope2_market_based"] == pytest.approx(0.204)
    assert e["scope3"] == pytest.approx(0.120)               # waste 250 * 0.48
    assert r["assurance"]["level"] == "limited"
    assert r["filing_ready"] is True
    assert r["blockers"] == []
    assert "SB 253" in r["methodology_statement"]
    assert "GHG Protocol" in r["methodology_statement"]


def test_sb253_applies_the_scope3_and_boundary_gates(db, unscreened):
    """§ 38532 requires Scope 3 per the GHG Protocol: an unscreened Scope 3 inventory or an
    unresolved organisational boundary must block a California filing exactly as it blocks
    a SECR one. SB 253 used to apply neither gate."""
    from app.reports.secr import secr_report
    org, run = unscreened
    sb = sb253_report(db, org.id, run_id=run.id, assurance_level="limited")
    secr = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0,
                       intensity_denominator_period_days=365)
    assert sb["filing_ready"] is False
    # Whatever blocks SECR's boundary claim must block SB 253's too.
    assert set(secr["blockers"]) <= set(sb["blockers"])
    assert any("15 GHG Protocol categories" in b or "category" in b.lower()
               for b in sb["blockers"])


def test_sb253_scope3_includes_cat15_financed_and_is_labelled(db, seeded):
    """Financed emissions are GHGP Cat 15, so they are IN Scope 3 and the totals — with the
    activity-derived figure published alongside, never swapped for it silently."""
    org, run = seeded
    run.financed_co2e = 4000.0                                # 4 tCO2e, frozen
    run.financed_as_of = "2025-12-31"
    db.commit()
    e = sb253_report(db, org.id, run_id=run.id,
                     assurance_level="limited")["emissions_tco2e"]
    assert e["scope3_excl_financed"] == pytest.approx(0.120)
    assert e["scope3"] == pytest.approx(4.120)
    assert e["total_location_based"] == pytest.approx(
        e["total_location_based_excl_financed"] + 4.0)
    assert e["scope3_cat15_financed"]["included_in_scope3_and_totals"] is True
    assert e["scope3_cat15_financed"]["tco2e"] == pytest.approx(4.0)
    assert e["scope3_cat15_financed"]["as_of"] == "2025-12-31"


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
