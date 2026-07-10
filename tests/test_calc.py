import pytest

from app.models import (
    EmissionFactor, ActivityRecord, EmissionLineItem, Organisation, CalculationRun,
    ReportingPeriod, MarketInstrument,
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
    li = db.query(EmissionLineItem).filter(
        EmissionLineItem.activity_id == a.id,
        EmissionLineItem.method == "location").one()
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
    # Scope 2 electricity => 2 lines per run (location + market dual reporting).
    assert len(_items(db, run1)) == 2
    assert len(_items(db, run2)) == 2


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


# --- Phase 2c: dual Scope 2 (location + market) — Gap 3 ---

def _market_lines(db, run):
    return db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "market").all()


def test_scope2_gets_dual_line_items_with_grid_fallback(db):
    """No instrument on file -> market line exists and equals location (fallback)."""
    import json
    org = _org(db)
    f = _seed_electricity_factor(db)                      # 0.17/kWh grid average
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    run = compute_co2e(db, org.id)
    market = _market_lines(db, run)
    assert len(market) == 1
    assert market[0].co2e == pytest.approx(170.0)
    assert json.loads(market[0].details)["method_basis"] == "grid_average_fallback"
    assert run.total_co2e_market == pytest.approx(run.total_co2e)


def test_rec_zeroes_market_scope2(db):
    """A REC (0 kgCO2e/kWh) zeroes market-based Scope 2; location unchanged."""
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0)); db.commit()
    run = compute_co2e(db, org.id)
    assert run.total_co2e == pytest.approx(170.0)          # location headline unchanged
    assert run.total_co2e_market == pytest.approx(0.0)     # market zeroed by REC
    s = summary(db, organisation_id=org.id, run_id=run.id)
    assert s["scope2"]["location_based"] == pytest.approx(170.0)
    assert s["scope2"]["market_based"] == pytest.approx(0.0)


def test_supplier_specific_beats_residual_mix(db):
    """Instrument hierarchy: contractual instrument outranks residual mix."""
    import json
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="residual_mix",
                            kg_co2e_per_kwh=0.25))
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="supplier_specific",
                            kg_co2e_per_kwh=0.05))
    db.commit()
    run = compute_co2e(db, org.id)
    m = _market_lines(db, run)[0]
    d = json.loads(m.details)
    assert d["method_basis"] == "contractual_instrument"
    assert d["allocations"][0]["instrument_type"] == "supplier_specific"
    assert m.co2e == pytest.approx(50.0)                   # 1000 kWh * 0.05


def test_instrument_date_window_respected(db):
    """An instrument outside the activity date window must not apply."""
    import json
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")  # dated 2025-01-01
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0,
                            start_date="2026-01-01", end_date="2026-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    m = _market_lines(db, run)[0]
    assert json.loads(m.details)["method_basis"] == "grid_average_fallback"
    assert run.total_co2e_market == pytest.approx(run.total_co2e)


def test_market_instrument_is_org_scoped(db):
    """OrgB's REC must not zero OrgA's market Scope 2."""
    orgA, orgB = _org(db, "A"), _org(db, "B")
    f = _seed_electricity_factor(db)
    _activity(db, orgA.id, f.id, quantity=1000, unit="kWh")
    db.add(MarketInstrument(organisation_id=orgB.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0)); db.commit()
    run = compute_co2e(db, orgA.id)
    assert run.total_co2e_market == pytest.approx(170.0)   # B's REC did not apply


def test_scope1_and_3_have_no_market_lines_and_no_double_count(db):
    org = _org(db)
    f_gas = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                           category="gas", subcategory="", unit="kWh", gwp_set="AR6", value=0.184)
    db.add(f_gas); db.commit(); db.refresh(f_gas)
    f_el = _seed_electricity_factor(db)
    _activity(db, org.id, f_gas.id, quantity=100, unit="kWh", category="gas")     # scope 1
    _activity(db, org.id, f_el.id, quantity=1000, unit="kWh")                     # scope 2
    run = compute_co2e(db, org.id)
    assert len(_market_lines(db, run)) == 1                # only the scope-2 activity
    s = summary(db, organisation_id=org.id, run_id=run.id)
    # by_scope sums location lines only: 18.4 (scope 1) + 170 (scope 2) = total.
    assert sum(r["co2e"] for r in s["by_scope"]) == pytest.approx(run.total_co2e)
    assert run.total_co2e == pytest.approx(188.4)
    # market total swaps only scope 2 (no instrument -> equal here).
    assert run.total_co2e_market == pytest.approx(188.4)


# --- Phase 2b/2c verification-panel hardening ---

def test_ch4_origin_routes_gwp_variant():
    """Fossil vs biogenic CH4 must use their own GWPs, not the blended value."""
    class F:
        unit = "kg"; value = None
        kg_co2 = 0.0; kg_ch4 = 1.0; kg_n2o = None
        has_gas_breakdown = True
        ch4_origin = "fossil"
    fossil = compute_activity_co2e(1.0, "kg", F(), gwp_set="AR6")
    F.ch4_origin = "biogenic"
    biogenic = compute_activity_co2e(1.0, "kg", F(), gwp_set="AR6")
    F.ch4_origin = None
    blended = compute_activity_co2e(1.0, "kg", F(), gwp_set="AR6")
    assert fossil == pytest.approx(29.8)
    assert biogenic == pytest.approx(27.0)
    assert blended == pytest.approx(27.9)


def test_rec_volume_matching_partial_coverage(db):
    """Scope 2 Guidance Ch.4: a 400 kWh REC covers 400 of 1000 kWh, not all of it."""
    import json
    org = _org(db)
    f = _seed_electricity_factor(db)                      # grid 0.17/kWh
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=400.0)); db.commit()
    run = compute_co2e(db, org.id)
    m = _market_lines(db, run)[0]
    d = json.loads(m.details)
    # 400 kWh at 0.0 + 600 kWh at grid 0.17 = 102.0
    assert m.co2e == pytest.approx(102.0)
    assert d["method_basis"] == "partial_contractual"
    assert d["kwh_contractual"] == pytest.approx(400.0)
    assert d["kwh_grid_fallback"] == pytest.approx(600.0)
    assert run.total_co2e_market == pytest.approx(102.0)
    assert run.total_co2e == pytest.approx(170.0)


def test_rec_volume_exhausts_across_activities(db):
    """Volume is consumed cumulatively across the run, not reset per activity."""
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=400, unit="kWh")
    _activity(db, org.id, f.id, quantity=600, unit="kWh")
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=500.0)); db.commit()
    run = compute_co2e(db, org.id)
    # 500 kWh covered total; remaining 500 kWh at 0.17 = 85.0
    assert run.total_co2e_market == pytest.approx(85.0)
    assert run.total_co2e == pytest.approx(170.0)


def test_instrument_gwp_vintage_mismatch_not_applied(db):
    """An AR6-vintage instrument must not enter an AR5 run's market total."""
    import json
    org = _org(db)
    f = _seed_waste_factor_per_gas(db)                    # per-gas, AR5-computable
    # electricity per-gas factor so the AR5 run computes scope 2 too
    fe = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                        category="electricity", subcategory="", unit="kWh", gwp_set="AR6",
                        value=0.17, kg_co2=0.168337, kg_ch4=0.00001, kg_n2o=0.000005,
                        ch4_origin="fossil")
    db.add(fe); db.commit(); db.refresh(fe)
    _activity(db, org.id, fe.id, quantity=1000, unit="kWh")
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, gwp_set="AR6")); db.commit()
    run5 = compute_co2e(db, org.id, gwp_set="AR5")
    m = _market_lines(db, run5)[0]
    d = json.loads(m.details)
    assert d["method_basis"] == "grid_average_fallback"   # instrument skipped
    assert d["instruments_skipped_gwp_vintage"] != []
    # market == location under AR5 (no vintage-mixed number)
    assert run5.total_co2e_market == pytest.approx(run5.total_co2e)


def test_dated_instrument_never_covers_undated_activity(db):
    """C1: an activity with no/malformed date must not match a dated instrument."""
    import json
    org = _org(db)
    f = _seed_electricity_factor(db)
    a = _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    a.date = ""; db.commit()
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0,
                            start_date="2025-01-01", end_date="2025-12-31")); db.commit()
    run = compute_co2e(db, org.id)
    d = json.loads(_market_lines(db, run)[0].details)
    assert d["method_basis"] == "grid_average_fallback"
    assert run.total_co2e_market == pytest.approx(170.0)


def test_malformed_date_fails_closed_in_window_check(db):
    """C2: '2025-9-5' must not be string-compared; unparseable -> no dated match."""
    import json
    org = _org(db)
    f = _seed_electricity_factor(db)
    a = _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    a.date = "2025-9-5"; db.commit()                       # not zero-padded
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0,
                            start_date="2025-01-01", end_date="2025-12-31")); db.commit()
    run = compute_co2e(db, org.id)
    d = json.loads(_market_lines(db, run)[0].details)
    assert d["method_basis"] == "grid_average_fallback"


def test_non_electricity_scope2_never_gets_electricity_instrument(db):
    """C4: purchased heat/gas preset to scope 2 must not be zeroed by a REC."""
    import json
    org = _org(db)
    f_gas = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                           category="gas", subcategory="", unit="kWh", gwp_set="AR6", value=0.184)
    db.add(f_gas); db.commit(); db.refresh(f_gas)
    a = _activity(db, org.id, f_gas.id, quantity=1000, unit="kWh", category="gas")
    a.scope = "2"; db.commit()                             # preset scope 2 (purchased heat-like)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0)); db.commit()
    run = compute_co2e(db, org.id)
    m = _market_lines(db, run)[0]
    d = json.loads(m.details)
    assert d["method_basis"] == "grid_average_fallback"
    assert "non-electricity" in d["fallback_reason"]
    assert m.co2e == pytest.approx(184.0)                  # NOT zeroed by the REC


def test_period_run_flags_missing_dates_as_data_errors(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")   # dated 2025-01-01
    a2 = _activity(db, org.id, f.id, quantity=500, unit="kWh")
    a2.date = ""; db.commit()                                # undatable
    p = ReportingPeriod(organisation_id=org.id, label="FY25",
                        start_date="2025-01-01", end_date="2025-12-31", frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert run.total_activities == 2                         # kept, not vanished
    assert run.mapped == 1
    assert run.data_errors == 1


def test_summary_partial_flag_and_market_bases(db):
    org = _org(db)
    f = _seed_electricity_factor(db)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")
    _activity(db, org.id, None, quantity=5, unit="kg", category="w")   # unmapped
    run = compute_co2e(db, org.id)
    s = summary(db, organisation_id=org.id, run_id=run.id)
    assert s["partial"] is True
    assert s["partial_reasons"] == {"unmapped": 1}
    assert s["scope2"]["market_bases"] == {"grid_average_fallback": 1}


# --- Phase 3: mapping review gate (Gap 6) ---

def test_resolver_gates_coarse_matches(db):
    """Exact binds; Global exact binds; coarse fallbacks only suggest."""
    from app.services.resolver import auto_map_activity
    org = _org(db)
    f_gb = _seed_electricity_factor(db)                    # GB, subcategory ""
    f_fl = EmissionFactor(source="TEST", version="1", geography="Global", year=2024,
                          category="flight", subcategory="short_haul_economy",
                          unit="pkm", gwp_set="AR6", value=0.145)
    db.add(f_fl); db.commit(); db.refresh(f_fl)

    a1 = _activity(db, org.id, None, quantity=100, unit="kWh")               # exact GB
    a2 = ActivityRecord(organisation_id=org.id, date="2025-01-01", category="flight",
                        subcategory="short_haul_economy", description="", quantity=900,
                        unit="pkm", geo="GB")
    a3 = ActivityRecord(organisation_id=org.id, date="2025-01-01", category="electricity",
                        subcategory="", description="", quantity=500, unit="kWh", geo="DE")
    db.add_all([a2, a3]); db.commit(); db.refresh(a2); db.refresh(a3)

    assert auto_map_activity(db, a1) == "auto"             # exact -> bound
    assert a1.factor_id == f_gb.id
    assert auto_map_activity(db, a2) == "auto"             # Global exact -> bound
    assert a2.factor_id == f_fl.id and a2.mapping_basis == "exact_global"
    assert auto_map_activity(db, a3) == "needs_review"     # coarse -> suggested only
    assert a3.factor_id is None and a3.suggested_factor_id == f_gb.id
    db.commit()

    # Unreviewed suggestion stays OUT of totals (visible as unmapped).
    run = compute_co2e(db, org.id)
    assert run.unmapped == 1
    assert run.mapped == 2


# --- Phase 3 verification-panel hardening ---

def test_superseded_factor_never_proposed(db):
    """A corrected factor supersedes the old one; the old row must never bind."""
    from app.services.resolver import propose_mapping
    old = _seed_electricity_factor(db, value=0.999)                 # stale, inserted first
    new = EmissionFactor(source="TEST", version="2", geography="GB", year=2024,
                         category="electricity", subcategory="", unit="kWh",
                         gwp_set="AR6", value=0.17, supersedes_id=old.id)
    db.add(new); db.commit(); db.refresh(new)
    factor, basis, conf = propose_mapping(db, "electricity", "", None, "GB")
    assert factor.id == new.id                                      # never the superseded row
    assert basis == "exact" and conf == 1.0


def test_resolver_prefers_newest_year(db):
    from app.services.resolver import propose_mapping
    f2023 = EmissionFactor(source="TEST", version="1", geography="GB", year=2023,
                           category="electricity", subcategory="", unit="kWh",
                           gwp_set="AR6", value=0.20)
    f2024 = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                           category="electricity", subcategory="", unit="kWh",
                           gwp_set="AR6", value=0.17)
    db.add_all([f2023, f2024]); db.commit()
    factor, _, _ = propose_mapping(db, "electricity", "", None, "GB")
    assert factor.year == 2024                                      # deterministic, newest


def test_mistyped_subcategory_gets_fuzzy_correction(db):
    """'Short-Haul Economy' must fuzzy-match, not fall to a subcategory-blind pick."""
    from app.services.resolver import propose_mapping
    short = EmissionFactor(source="TEST", version="1", geography="Global", year=2024,
                           category="flight", subcategory="short_haul_economy",
                           unit="pkm", gwp_set="AR6", value=0.145)
    lng = EmissionFactor(source="TEST", version="1", geography="Global", year=2024,
                         category="flight", subcategory="long_haul_economy",
                         unit="pkm", gwp_set="AR6", value=0.110)
    db.add_all([short, lng]); db.commit()
    factor, basis, conf = propose_mapping(db, "flight", "Short-Haul Economy", None, "GB")
    assert basis == "fuzzy_subcategory"
    assert factor.subcategory == "short_haul_economy"               # right variant suggested
    assert conf < 0.95                                              # still review-gated


def test_needs_review_upgrades_when_catalog_improves(db):
    """Re-running the mapper on a needs_review row picks up a new exact factor."""
    from app.services.resolver import auto_map_activity
    org = _org(db)
    f_gb = _seed_electricity_factor(db)                             # GB only
    a = ActivityRecord(organisation_id=org.id, date="2025-01-01", category="electricity",
                       subcategory="", description="", quantity=500, unit="kWh", geo="DE")
    db.add(a); db.commit(); db.refresh(a)
    assert auto_map_activity(db, a) == "needs_review"               # coarse suggestion
    f_de = EmissionFactor(source="TEST", version="1", geography="DE", year=2024,
                          category="electricity", subcategory="", unit="kWh",
                          gwp_set="AR6", value=0.30)
    db.add(f_de); db.commit(); db.refresh(f_de)
    assert auto_map_activity(db, a) == "auto"                       # upgraded to exact
    assert a.factor_id == f_de.id
    assert a.suggested_factor_id is None


def test_date_policy_is_per_row_and_dayfirst():
    """'01/02/2025' is ALWAYS 1 Feb (day-first policy), never sibling-dependent."""
    from app.services.ingestion import parse_csv
    csv1 = b"date,category,quantity,unit\n01/02/2025,electricity,1,kWh\n"
    csv2 = (b"date,category,quantity,unit\n"
            b"01/02/2025,electricity,1,kWh\n"
            b"15/01/2025,electricity,2,kWh\n")   # sibling that used to flip inference
    d1 = parse_csv(csv1, "a.csv")["date"].tolist()
    d2 = parse_csv(csv2, "b.csv")["date"].tolist()
    assert d1 == ["2025-02-01"]
    assert d2 == ["2025-02-01", "2025-01-15"]    # same string, same answer
    iso = parse_csv(b"date,category,quantity,unit\n2025-01-15,e,1,kWh\n", "c.csv")
    assert iso["date"].tolist() == ["2025-01-15"]
    bad = parse_csv(b"date,category,quantity,unit\n31/31/2025,e,1,kWh\n", "d.csv")
    assert bad["date"].tolist() == [""]          # unparseable -> empty, flagged by QA


# --- LCA methods: spend-based EEIO, method hierarchy, biogenic pool ---

def _spend_factor(db, subcategory="professional_services", value=0.045, unit="GBP"):
    f = EmissionFactor(source="EEIO_DEMO", version="1", geography="GB", year=2024,
                       category="spend", subcategory=subcategory, unit=unit,
                       gwp_set="AR6", value=value, method_type="spend_based",
                       lca_boundary="cradle_to_gate")
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_spend_based_calculation_end_to_end(db):
    """Spend activity (GBP) x EEIO factor (kgCO2e/GBP) computes as Scope 3."""
    import json
    org = _org(db)
    f = _spend_factor(db)
    a = ActivityRecord(organisation_id=org.id, date="2025-01-01", category="spend",
                       subcategory="professional_services", description="legal fees",
                       quantity=10000, unit="GBP", geo="GB", factor_id=f.id)
    db.add(a); db.commit(); db.refresh(a)
    run = compute_co2e(db, org.id)
    assert run.mapped == 1
    assert run.total_co2e == pytest.approx(450.0)          # 10000 * 0.045
    li = db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id).one()
    assert li.scope == "3"
    d = json.loads(li.details)
    assert d["method_type"] == "spend_based"
    assert d["lca_boundary"] == "cradle_to_gate"


def test_currency_mismatch_fails_closed(db):
    """EUR spend against a GBP factor needs an audited FX rate — reject, not guess."""
    org = _org(db)
    f = _spend_factor(db, unit="GBP")
    a = ActivityRecord(organisation_id=org.id, date="2025-01-01", category="spend",
                       subcategory="professional_services", description="", quantity=5000,
                       unit="EUR", geo="GB", factor_id=f.id)
    db.add(a); db.commit()
    run = compute_co2e(db, org.id)
    assert run.unit_errors == 1
    assert run.mapped == 0


def test_resolver_prefers_supplier_specific_over_spend(db):
    """GHG Protocol hierarchy: supplier data beats EEIO at the same match tier."""
    from app.services.resolver import propose_mapping
    _spend_factor(db, subcategory="widgets", value=0.5)
    sup = EmissionFactor(source="SUPPLIER", version="1", geography="GB", year=2024,
                         category="spend", subcategory="widgets", unit="GBP",
                         gwp_set="AR6", value=0.1, method_type="supplier_specific",
                         lca_boundary="cradle_to_gate")
    db.add(sup); db.commit(); db.refresh(sup)
    factor, basis, _ = propose_mapping(db, "spend", "widgets", None, "GB")
    assert factor.id == sup.id                               # supplier-specific wins
    assert basis == "exact"


def test_method_split_and_primary_share_in_summary(db):
    org = _org(db)
    f_el = _seed_electricity_factor(db)                      # average_data (default)
    f_sp = _spend_factor(db)                                 # spend_based
    _activity(db, org.id, f_el.id, quantity=1000, unit="kWh")           # 170 kg
    a = ActivityRecord(organisation_id=org.id, date="2025-01-01", category="spend",
                       subcategory="professional_services", description="", quantity=10000,
                       unit="GBP", geo="GB", factor_id=f_sp.id)
    db.add(a); db.commit()
    run = compute_co2e(db, org.id)
    s = summary(db, organisation_id=org.id, run_id=run.id)
    ms = s["method_split"]
    assert ms["co2e_by_method"]["average_data"] == pytest.approx(170.0)
    assert ms["co2e_by_method"]["spend_based"] == pytest.approx(450.0)
    assert ms["spend_based_share_pct"] == pytest.approx(100 * 450 / 620, abs=0.01)
    assert ms["primary_data_share_pct"] == 0.0               # no supplier data yet


def test_biogenic_co2_is_separate_never_netted(db):
    """ISO 14067: biogenic CO2 reported separately, excluded from total_co2e."""
    org = _org(db)
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category="waste", subcategory="composting", unit="kg",
                       gwp_set="AR6", value=0.01, method_type="average_data",
                       lca_boundary="waste_treatment", kg_co2_biogenic=0.2)
    db.add(f); db.commit(); db.refresh(f)
    a = ActivityRecord(organisation_id=org.id, date="2025-01-01", category="waste",
                       subcategory="composting", description="", quantity=100,
                       unit="kg", geo="GB", factor_id=f.id)
    db.add(a); db.commit()
    run = compute_co2e(db, org.id)
    assert run.total_co2e == pytest.approx(1.0)              # fossil-basis total only
    assert run.total_biogenic_co2e == pytest.approx(20.0)    # 100 * 0.2, separate pool
    s = summary(db, organisation_id=org.id, run_id=run.id)
    assert s["biogenic_co2e_separate"] == pytest.approx(20.0)
    assert s["total_co2e"] == pytest.approx(1.0)             # never netted


# --- Phase 5 verifier hardening ---

def test_method_split_uses_frozen_lineage_not_live_mapping(db):
    """Re-mapping an activity after a run must NOT relabel that run's method mix."""
    org = _org(db)
    f_avg = _seed_electricity_factor(db, value=0.9)         # average_data
    a = _activity(db, org.id, f_avg.id, quantity=1000, unit="kWh")
    run = compute_co2e(db, org.id)                          # 900 kg, average_data
    before = summary(db, organisation_id=org.id, run_id=run.id)["method_split"]
    assert before["co2e_by_method"] == {"average_data": pytest.approx(900.0)}
    # Re-map the SAME activity to a spend_based factor; do NOT re-run.
    f_spend = _spend_factor(db, subcategory="misc", value=0.1)
    a.factor_id = f_spend.id; db.commit()
    after = summary(db, organisation_id=org.id, run_id=run.id)["method_split"]
    # Frozen run keeps its true method label and figure.
    assert after["co2e_by_method"] == {"average_data": pytest.approx(900.0)}


def test_biogenic_not_duplicated_across_location_market_lineage(db):
    """A scope-2 activity's biogenic amount appears in ONE line's detail, counted once."""
    import json
    org = _org(db)
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh", gwp_set="AR6",
                       value=0.17, method_type="average_data", lca_boundary="generation",
                       kg_co2_biogenic=0.01)
    db.add(f); db.commit(); db.refresh(f)
    _activity(db, org.id, f.id, quantity=1000, unit="kWh")   # scope 2 -> location+market
    run = compute_co2e(db, org.id)
    assert run.total_biogenic_co2e == pytest.approx(10.0)    # counted once
    items = db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id).all()
    with_bio = [i for i in items if "biogenic_co2e" in json.loads(i.details)]
    assert len(with_bio) == 1                                # only the location line
    assert with_bio[0].method == "location"
