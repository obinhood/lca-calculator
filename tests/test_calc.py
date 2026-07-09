import pytest

from app.models import (
    EmissionFactor, ActivityRecord, EmissionLineItem, Organisation, CalculationRun,
    ReportingPeriod,
)
from app.services.calc import compute_co2e, compute_activity_co2e, ReportingPeriodError
from app.services.units import UnitConversionError
from app.reports.summary import summary


def _org(db, name="DemoOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _seed_electricity_factor(db, value=0.17, unit="kWh", gwp_set="AR6"):
    f = EmissionFactor(
        source="TEST", version="1", geography="GB", year=2024,
        category="electricity", subcategory="", unit=unit, gwp_set=gwp_set, value=value,
    )
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, quantity, unit, category="electricity"):
    a = ActivityRecord(
        organisation_id=org_id, date="2025-01-01", category=category, subcategory="",
        description="", quantity=quantity, unit=unit, geo="GB", factor_id=factor_id,
    )
    db.add(a); db.commit(); db.refresh(a)
    return a


def _items(db, run):
    return db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id).all()


# --- The headline correctness fix (Gap 1) ---

def test_mwh_activity_is_unit_converted(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    a = _activity(db, org.id, f.id, quantity=1.2, unit="MWh")
    run = compute_co2e(db, org.id)
    li = db.query(EmissionLineItem).filter(EmissionLineItem.activity_id == a.id).one()
    assert li.co2e == pytest.approx(204.0)
    assert run.mapped == 1


def test_incompatible_units_produce_no_number(db):
    org = _org(db)
    f = _seed_electricity_factor(db)  # unit kWh
    _activity(db, org.id, f.id, quantity=50, unit="kg")
    run = compute_co2e(db, org.id)
    assert run.unit_errors == 1
    assert len(_items(db, run)) == 0


def test_compute_activity_co2e_direct():
    class F:
        unit = "kWh"
        value = 0.17
    assert compute_activity_co2e(1000, "kWh", F()) == pytest.approx(170.0)
    assert compute_activity_co2e(1, "MWh", F()) == pytest.approx(170.0)
    with pytest.raises(UnitConversionError):
        compute_activity_co2e(1, "kg", F())


# --- Immutable runs + org scoping (Gap 5, Gap 6, reviewer C1) ---

def test_runs_are_immutable_history(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    run1 = compute_co2e(db, org.id)
    run2 = compute_co2e(db, org.id)
    assert run2.id != run1.id
    assert db.query(CalculationRun).count() == 2
    # run1's line items are NOT deleted by run2 (no destructive global recompute).
    assert len(_items(db, run1)) == 1
    assert len(_items(db, run2)) == 1


def test_calculation_is_org_scoped(db):
    org1, org2 = _org(db, "A"), _org(db, "B")
    f = _seed_electricity_factor(db)
    _activity(db, org1.id, f.id, quantity=1000, unit="kWh")
    _activity(db, org2.id, f.id, quantity=9999, unit="kWh")
    run = compute_co2e(db, org1.id)
    # Only org1's single activity is in scope.
    assert run.total_activities == 1
    assert run.mapped == 1
    assert run.total_co2e == pytest.approx(170.0)
    # org2 has no run at all.
    assert db.query(CalculationRun).filter(CalculationRun.organisation_id == org2.id).count() == 0


# --- Coverage / completeness (Gap 4) ---

def test_run_coverage_counts_gaps(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")            # mapped
    _activity(db, org.id, None, quantity=5, unit="kg", category="widgets")  # unmapped
    _activity(db, org.id, f.id, quantity=10, unit="kg")              # unit error
    run = compute_co2e(db, org.id)
    assert run.total_activities == 3
    assert run.mapped == 1
    assert run.unmapped == 1
    assert run.unit_errors == 1
    s = summary(db, run_id=run.id)
    assert s["coverage"]["coverage_pct"] == pytest.approx(33.33, abs=0.01)
    assert "widgets" in s["coverage"]["unmapped_by_category"]


# --- Hardening regressions (verifier findings) ---

def test_non_finite_quantity_does_not_poison_total(db):
    import math
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")        # good -> 170
    bad = _activity(db, org.id, f.id, quantity=1.0, unit="kWh")
    bad.quantity = float("inf")                                   # force inf, bypass ingestion
    db.commit()
    run = compute_co2e(db, org.id)
    assert run.data_errors == 1
    total = summary(db, run_id=run.id)["total_co2e"]
    assert math.isfinite(total)
    assert total == pytest.approx(170.0)


def test_negative_quantity_is_flagged_not_calculated(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=-500, unit="kWh")
    run = compute_co2e(db, org.id)
    assert run.data_errors == 1
    assert len(_items(db, run)) == 0


def test_gwp_set_mismatch_is_flagged(db):
    org = _org(db)
    f = _seed_electricity_factor(db, gwp_set="AR6")
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    run = compute_co2e(db, org.id, gwp_set="AR5")   # request AR5, factor is AR6
    assert run.gwp_mismatch == 1
    assert len(_items(db, run)) == 0


def test_buckets_are_mece(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")             # mapped
    _activity(db, org.id, None, quantity=5, unit="kg", category="w")   # unmapped
    _activity(db, org.id, f.id, quantity=10, unit="kg")               # unit error
    _activity(db, org.id, f.id, quantity=-3, unit="kWh")              # data error
    run = compute_co2e(db, org.id)
    assert run.total_activities == 4
    assert run.mapped + run.unmapped + run.unit_errors + run.data_errors + run.gwp_mismatch == 4
    assert (run.mapped, run.unmapped, run.unit_errors, run.data_errors) == (1, 1, 1, 1)


def test_stale_run_is_surfaced(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    run = compute_co2e(db, org.id)                    # snapshot: 1 activity
    _activity(db, org.id, None, quantity=5, unit="kg", category="new")  # added after run
    cov = summary(db, organisation_id=org.id, run_id=run.id)["coverage"]
    assert cov["stale"] is True
    assert "STALE" in cov["warning"]


# --- Phase 2a verification-panel fixes ---

def test_staleness_detects_remap_without_count_change(db):
    """Fingerprint-based staleness: re-mapping at equal count must flag stale."""
    org = _org(db)
    f1 = _seed_electricity_factor(db, value=0.10)
    a = _activity(db, org.id, f1.id, quantity=1000, unit="kWh")
    run = compute_co2e(db, org.id)   # total 100.0
    f2 = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                        category="electricity", subcategory="", unit="kWh", gwp_set="AR6", value=0.50)
    db.add(f2); db.commit(); db.refresh(f2)
    a.factor_id = f2.id; db.commit()   # remap, count unchanged (1 == 1)
    cov = summary(db, organisation_id=org.id, run_id=run.id)["coverage"]
    assert cov["stale"] is True


def test_cross_tenant_run_id_is_blocked(db):
    """OrgA must not be able to read OrgB's run by id (IDOR)."""
    orgA, orgB = _org(db, "A"), _org(db, "B")
    f = _seed_electricity_factor(db)
    _activity(db, orgB.id, f.id, quantity=9999, unit="kWh")
    run_b = compute_co2e(db, orgB.id)
    s = summary(db, organisation_id=orgA.id, run_id=run_b.id)
    assert s["run"] is None
    assert s["total_co2e"] == 0.0


def test_exclusions_are_surfaced(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=10, unit="kg")  # unit error
    run = compute_co2e(db, org.id)
    s = summary(db, organisation_id=org.id, run_id=run.id)
    assert isinstance(s["exclusions"], list) and len(s["exclusions"]) == 1
    assert s["exclusions"][0]["activity_id"] is not None


def test_frozen_period_rejects_run(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    p = ReportingPeriod(organisation_id=org.id, label="FY25",
                        start_date="2025-01-01", end_date="2025-12-31", frozen=True)
    db.add(p); db.commit(); db.refresh(p)
    with pytest.raises(ReportingPeriodError):
        compute_co2e(db, org.id, reporting_period_id=p.id)


def test_period_filters_activities_by_date(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")           # dated 2025-01-01 (in range)
    a2 = _activity(db, org.id, f.id, quantity=500, unit="kWh")
    a2.date = "2026-06-01"; db.commit()                             # out of range
    p = ReportingPeriod(organisation_id=org.id, label="FY25",
                        start_date="2025-01-01", end_date="2025-12-31", frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert run.total_activities == 1   # only the in-range activity


def test_period_ownership_enforced(db):
    orgA, orgB = _org(db, "A"), _org(db, "B")
    f = _seed_electricity_factor(db)
    _activity(db, orgA.id, f.id, quantity=1000, unit="kWh")
    p_b = ReportingPeriod(organisation_id=orgB.id, label="B-FY25", frozen=False)
    db.add(p_b); db.commit(); db.refresh(p_b)
    with pytest.raises(ReportingPeriodError):
        compute_co2e(db, orgA.id, reporting_period_id=p_b.id)   # OrgA using OrgB's period


# --- Phase 2b: per-gas factors make the AR5/AR6 switch REAL (Gap 2) ---

def _seed_waste_factor_per_gas(db):
    """Landfill MSW, CH4-dominated: AR6 aggregate = 0.48 kgCO2e/kg exactly."""
    f = EmissionFactor(
        source="TEST", version="1", geography="GB", year=2024,
        category="waste", subcategory="landfill_msw", unit="kg", gwp_set="AR6",
        value=0.48, kg_co2=0.00297, kg_ch4=0.017, kg_n2o=0.00001,
    )
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_per_gas_ar6_matches_aggregate_value():
    """AR6 recomposition of the decomposed factor equals the published value."""
    class F:
        unit = "kg"; value = 0.48
        kg_co2 = 0.00297; kg_ch4 = 0.017; kg_n2o = 0.00001
        has_gas_breakdown = True
    ar6 = compute_activity_co2e(1.0, "kg", F(), gwp_set="AR6")
    assert ar6 == pytest.approx(0.48, abs=1e-12)


def test_ar5_and_ar6_produce_different_numbers():
    """THE Gap 2 fix: same factor row, different GWP set -> different CO2e."""
    class F:
        unit = "kg"; value = 0.48
        kg_co2 = 0.00297; kg_ch4 = 0.017; kg_n2o = 0.00001
        has_gas_breakdown = True
    ar5 = compute_activity_co2e(1.0, "kg", F(), gwp_set="AR5")
    ar6 = compute_activity_co2e(1.0, "kg", F(), gwp_set="AR6")
    # AR5: 0.00297 + 0.017*28.0 + 0.00001*265.0 = 0.48162
    assert ar5 == pytest.approx(0.48162, abs=1e-9)
    assert ar5 != ar6


def test_ar5_run_works_with_per_gas_factors(db):
    """An AR5 run against per-gas factors computes (no gwp_mismatch dead-end)."""
    org = _org(db)
    f = _seed_waste_factor_per_gas(db)
    _activity(db, org.id, f.id, quantity=250, unit="kg", category="waste")
    run = compute_co2e(db, org.id, gwp_set="AR5")
    assert run.gwp_mismatch == 0
    assert run.mapped == 1
    assert run.total_co2e == pytest.approx(250 * 0.48162, rel=1e-9)


def test_run_gwp_set_changes_the_total(db):
    org = _org(db)
    f = _seed_waste_factor_per_gas(db)
    _activity(db, org.id, f.id, quantity=250, unit="kg", category="waste")
    run6 = compute_co2e(db, org.id, gwp_set="AR6")
    run5 = compute_co2e(db, org.id, gwp_set="AR5")
    assert run6.total_co2e == pytest.approx(120.0)          # 250 * 0.48
    assert run5.total_co2e == pytest.approx(120.405)        # 250 * 0.48162
    assert run5.total_co2e != run6.total_co2e


def test_per_gas_lineage_in_details(db):
    import json
    org = _org(db)
    f = _seed_waste_factor_per_gas(db)
    _activity(db, org.id, f.id, quantity=250, unit="kg", category="waste")
    run = compute_co2e(db, org.id, gwp_set="AR6")
    li = db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id).one()
    d = json.loads(li.details)
    assert d["calc_method"] == "per_gas"
    assert d["gwp_set_applied"] == "AR6"
    assert d["gases_kg_per_unit"] == {"CO2": 0.00297, "CH4": 0.017, "N2O": 0.00001}
    assert d["gwp_values"]["CH4"] == 27.9


def test_aggregate_factor_still_vintage_checked(db):
    """Factors WITHOUT a gas breakdown keep the strict vintage mismatch check."""
    org = _org(db)
    f = _seed_electricity_factor(db, gwp_set="AR6")   # aggregate only
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    run = compute_co2e(db, org.id, gwp_set="AR5")
    assert run.gwp_mismatch == 1
    assert run.mapped == 0
