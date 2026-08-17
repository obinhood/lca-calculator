"""Regressions for methodology defects found by the report audit.

Each test reproduces the audit's own failing scenario and pins the corrected behaviour.
"""
import pytest

from app.models import (ActivityRecord, CalculationRun, EmissionFactor,
                        FinancedPosition, Organisation, ReportingPeriod)
from app.services.calc import compute_co2e, FactorValueError, compute_activity_co2e
from app.reports.summary import summary
from app.reports.cdp import cdp_export


def _org(db, name="Co"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, value=0.5, category="purchased_goods", unit="GBP"):
    f = EmissionFactor(source="EEIO", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit, gwp_set="AR6",
                       value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor, qty, **kw):
    a = ActivityRecord(organisation_id=org_id, date=kw.pop("date", "2025-06-01"),
                       category=factor.category, subcategory="", description="",
                       quantity=qty, unit=factor.unit, geo="GB", factor_id=factor.id, **kw)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _position(db, org_id, s1=5000.0):
    p = FinancedPosition(organisation_id=org_id, investee_name="Acme",
                         asset_class="listed_equity", currency="GBP",
                         outstanding_amount=1_000_000.0,
                         attribution_denominator=10_000_000.0,
                         investee_scope1_tco2e=s1, investee_scope2_tco2e=0.0,
                         data_quality_score=2, as_of_date="2025-01-01")
    db.add(p); db.commit(); db.refresh(p)
    return p


# --- Category 15 double counting ---------------------------------------------------

def test_cat15_declared_twice_blocks_the_combined_total(db):
    """An investee counted once through an equity-stake activity and again through the
    loan attribution must not be summed. scope3.py has always refused this; summary.py
    was adding them twelve lines from the field reporting the refusal — and the
    derivations block then certified the double count as reperformed arithmetic."""
    org = _org(db, "Bank")
    f = _factor(db)
    _act(db, org.id, f, 200_000.0, ghgp_category=15)
    _position(db, org.id)
    run = compute_co2e(db, org.id)
    r = summary(db, org.id, run.id)

    assert r["financed_co2e"] == pytest.approx(500_000.0)
    assert r["cat15_double_count_blocked"] is True
    assert r["total_co2e_incl_financed_kg"] is None      # was 600,000.0
    d = r["derivations"]
    assert "Disclosed total including financed emissions" in d["calculations_refused"]
    assert d["all_reconcile"] is False

    # CDP consumed the same unguarded sum.
    c = cdp_export(db, org.id, run_id=run.id, intensity_denominator=2.0)
    assert c["answers"]["C6.5_scope3_tco2e"] is None
    assert c["answers"]["C6.5_cat15_double_count_blocked"] is True


def test_financed_emissions_still_add_when_there_is_no_double_declaration(db):
    """The guard must not suppress a legitimate total — a bank with a portfolio but no
    activity-derived Cat 15 line has nothing double-counted."""
    org = _org(db, "CleanBank")
    f = _factor(db, category="electricity", unit="kWh")
    _act(db, org.id, f, 1000.0)                          # Scope 2, not Cat 15
    _position(db, org.id)
    run = compute_co2e(db, org.id)
    r = summary(db, org.id, run.id)
    assert r["cat15_double_count_blocked"] is False
    assert r["total_co2e_incl_financed_kg"] == pytest.approx(
        (run.total_co2e or 0.0) + 500_000.0)
    assert r["derivations"]["calculations_refused"] == []


# --- negative per-gas factor --------------------------------------------------------

def test_a_negative_gas_mass_is_rejected_not_netted_into_the_gross_total(db):
    """The aggregate `value` column has a non-negative CHECK for exactly this reason;
    the per-gas path bypassed it, so a sink netted silently into the figure disclosed as
    GROSS. Removals have their own separately-reported channel."""
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="process", subcategory="", unit="t", gwp_set="AR6",
                       value=None, kg_co2=-1000.0)
    db.add(f); db.commit(); db.refresh(f)
    with pytest.raises(FactorValueError, match="NEGATIVE"):
        compute_activity_co2e(100.0, "t", f, gwp_set="AR6")


def test_a_positive_gas_mass_still_computes(db):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="process", subcategory="", unit="t", gwp_set="AR6",
                       value=None, kg_co2=1000.0)
    db.add(f); db.commit(); db.refresh(f)
    assert compute_activity_co2e(2.0, "t", f, gwp_set="AR6") == pytest.approx(2000.0)


# --- CDP C6.10 intensity ------------------------------------------------------------

def test_cdp_intensity_is_scope_1_and_2_only(db):
    """CDP C6.10 asks for gross global combined Scope 1 AND 2 per unit revenue. Computing
    it over the whole inventory inflated the filed ratio by the entire value chain."""
    org = _org(db, "IntensityCo")
    elec = _factor(db, 0.2, category="electricity", unit="kWh")
    travel = _factor(db, 0.1, category="flight", unit="pkm")
    _act(db, org.id, elec, 1000.0)                       # Scope 2: 200 kg
    _act(db, org.id, travel, 100_000.0)                  # Scope 3: 10,000 kg
    run = compute_co2e(db, org.id)
    c = cdp_export(db, org.id, run_id=run.id, intensity_denominator=2.0)
    i = c["answers"]["C6.10_intensity"]
    # Scope 1 + 2 only: 0.2 t / 2 = 0.1. The whole inventory would give 5.1.
    assert i["numerator_tco2e"] == pytest.approx(0.2)
    assert i["tco2e_per_unit"] == pytest.approx(0.1)
    assert "NOT the whole inventory" in i["numerator_basis"]


# --- SECR energy carriers -----------------------------------------------------------

def test_secr_discloses_energy_carriers_missing_from_the_kwh_total(db):
    """A carrier outside the three-carrier allowlist had its EMISSIONS counted in
    Scope 1/2 while its ENERGY vanished from the kWh figure, with no note — a partial
    energy total reading as complete beside a full emissions total."""
    from app.reports.secr import _energy_kwh
    org = _org(db, "MultiFuel")
    elec = _factor(db, 0.2, category="electricity", unit="kWh")
    lpg = _factor(db, 1.5, category="lpg", unit="L")
    _act(db, org.id, elec, 1000.0)
    _act(db, org.id, lpg, 500.0)
    run = compute_co2e(db, org.id)
    e = _energy_kwh(db, run)
    assert e["total_kwh"] == pytest.approx(1000.0)       # LPG genuinely not converted
    assert "lpg" in e["carriers_omitted"]
    assert any("NOT in this kWh total" in n for n in e["notes"])
    assert e["carriers_reported"] == ["electricity", "gas", "diesel"]


# --- SBTi financed emissions --------------------------------------------------------

def test_sbti_includes_financed_emissions_when_the_target_covers_scope_3(db):
    """Financed emissions ARE Scope 3 Category 15 but live outside EmissionLineItem, so
    a plain line-item sum dropped them from both the base year and the actuals — making
    a bank's target look on track against a fraction of what it covers."""
    from app.services.sbti import run_scoped_emissions_kg, financed_included
    org = _org(db, "SBTiBank")
    f = _factor(db, 0.2, category="electricity", unit="kWh")
    _act(db, org.id, f, 1000.0)
    _position(db, org.id)
    run = compute_co2e(db, org.id)

    with_s3 = run_scoped_emissions_kg(db, run.id, "1+2+3")
    without = run_scoped_emissions_kg(db, run.id, "1+2")
    assert with_s3 == pytest.approx(without + 500_000.0)
    assert financed_included(db, run.id, "1+2+3") == pytest.approx(500_000.0)
    # Excluded coverage reports None, so "excluded" stays distinct from "zero".
    assert financed_included(db, run.id, "1+2") is None


# --- ISO 14064-2 comparability ------------------------------------------------------

def _period(db, org_id, label, start, end):
    p = ReportingPeriod(organisation_id=org_id, label=label, start_date=start,
                        end_date=end, frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    return p


def test_iso_14064_2_blocks_a_period_length_mismatch(db):
    """A 12-month baseline minus a 3-month project run reports the missing nine months
    as abatement."""
    from app.reports.iso_14064_2 import iso_14064_2_report
    org = _org(db, "ProjectCo")
    f = _factor(db, 0.17, category="electricity", unit="kWh")
    a = _act(db, org.id, f, 1000.0, date="2024-06-15")
    bp = _period(db, org.id, "FY24", "2024-01-01", "2024-12-31")
    base = compute_co2e(db, org.id, gwp_set="AR6", reporting_period_id=bp.id)
    a.quantity, a.date = 600.0, "2025-02-15"
    db.commit()
    qp = _period(db, org.id, "Q1-25", "2025-01-01", "2025-03-31")
    proj = compute_co2e(db, org.id, gwp_set="AR6", reporting_period_id=qp.id)

    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0)
    assert r["disclosure_ready"] is False
    assert any("elapsed time as abatement" in b for b in r["blockers"])


def test_iso_14064_2_blocks_an_unscoped_run(db):
    """An unscoped run has no period length, so the delta cannot be shown to measure a
    project effect rather than a difference in elapsed time."""
    from app.reports.iso_14064_2 import iso_14064_2_report
    org = _org(db, "UnscopedCo")
    f = _factor(db, 0.17, category="electricity", unit="kWh")
    a = _act(db, org.id, f, 1000.0)
    base = compute_co2e(db, org.id, gwp_set="AR6")
    a.quantity = 600.0
    db.commit()
    proj = compute_co2e(db, org.id, gwp_set="AR6")
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0)
    assert r["disclosure_ready"] is False
    assert any("scoped to a reporting period" in b for b in r["blockers"])


# --- GRI biogenic attribution -------------------------------------------------------

def test_gri_does_not_attribute_an_all_scopes_biogenic_pool_to_scope_1(db):
    """The run tracks one undifferentiated biogenic pool; reporting it on the 305-1 line
    labelled Scope 2 and Scope 3 biogenic CO2 as Scope 1."""
    from app.reports.gri import gri_report
    org = _org(db, "BioCo")
    f = _factor(db, 0.2, category="electricity", unit="kWh")
    _act(db, org.id, f, 1000.0)
    run = compute_co2e(db, org.id)
    r = gri_report(db, org.id, run_id=run.id, intensity_denominator=1.0)
    assert r["gri_305_1_scope1"]["biogenic_co2_tco2_separate"] is None
    assert "cannot be split" in r["gri_305_1_scope1"]["biogenic_note"]
    assert r["biogenic_co2_tco2_all_scopes"] is not None


# --- SFDR PAI disclosure ------------------------------------------------------------

def test_sfdr_pai_echoes_include_scope3_and_counts_missing_investee_scope3(db):
    """`include_scope3` changes PAI 1, 2 and 3, so two reports generated with different
    values for it are not comparable — and a NULL investee Scope 3 treated as zero
    understates the weighted intensity."""
    from app.reports.sfdr_pai import sfdr_pai_report
    org = _org(db, "FundCo")
    p = _position(db, org.id)
    p.investee_revenue_millions = 10.0
    p.investee_scope3_tco2e = None                       # not reported, not zero
    db.commit()
    r = sfdr_pai_report(db, org.id, include_scope3=True)
    assert r["include_scope3"] is True
    cov = r["pai3_data_coverage"]
    assert cov["investee_scope3_not_reported"] == 1
    assert "'not reported' is not 'zero'" in cov["note"]


# --- SBTi: the financed-dimension comparability gate ---------------------------------

def test_sbti_blocks_when_only_one_run_has_financed_emissions_evaluated(db):
    """`financed_co2e` is None on any run computed before positions were loaded, and
    flattening that to 0.0 compared a base year WITH the portfolio against actuals
    WITHOUT it — manufacturing a 71% reduction from an organisation whose own operations
    had not changed at all."""
    from app.services.sbti import financed_comparable, financed_included
    org = _org(db, "GapBank")
    f = _factor(db, 0.2, category="electricity", unit="kWh")
    _act(db, org.id, f, 1000.0)
    _position(db, org.id)
    with_fin = compute_co2e(db, org.id)                  # financed evaluated
    assert with_fin.financed_co2e is not None

    bare = compute_co2e(db, org.id)
    bare.financed_co2e = None                            # dimension not evaluated
    db.commit()

    msg = financed_comparable(db, with_fin.id, bare.id, "1+2+3")
    assert msg is not None and "whole portfolio" in msg
    # Symmetric, and silent when both agree.
    assert financed_comparable(db, bare.id, with_fin.id, "1+2+3") is not None
    assert financed_comparable(db, with_fin.id, with_fin.id, "1+2+3") is None
    assert financed_comparable(db, bare.id, bare.id, "1+2+3") is None
    # A Scope-1+2 target is unaffected either way.
    assert financed_comparable(db, with_fin.id, bare.id, "1+2") is None
    # "Not evaluated" reports as None, never as zero.
    assert financed_included(db, bare.id, "1+2+3") is None


def test_sbti_excludes_financed_emissions_when_cat15_is_double_declared(db):
    """600,000 kg is precisely the sum summary.py and cdp.py refuse to publish; it must
    not land in the base year and the actuals that decide `on_track`."""
    from app.services.sbti import run_scoped_emissions_kg, financed_included
    org = _org(db, "DoubleBank")
    f = _factor(db)
    _act(db, org.id, f, 200_000.0, ghgp_category=15)
    _position(db, org.id)
    run = compute_co2e(db, org.id)
    assert summary(db, org.id, run.id)["cat15_double_count_blocked"] is True
    # The activity-derived Cat 15 line is counted; the portfolio is not added on top.
    assert run_scoped_emissions_kg(db, run.id, "1+2+3") == pytest.approx(run.total_co2e)
    assert financed_included(db, run.id, "1+2+3") is None


def test_a_zero_value_cat15_line_does_not_null_a_filing_field(db):
    """The guard must match scope3.py's: only Scope 3 lines with a positive amount. A
    zero-quantity line used to null CDP's C6.5."""
    org = _org(db, "ZeroCo")
    f = _factor(db)
    _act(db, org.id, f, 0.0, ghgp_category=15)           # zero spend -> zero emissions
    _position(db, org.id)
    run = compute_co2e(db, org.id)
    assert summary(db, org.id, run.id)["cat15_double_count_blocked"] is False


# --- SECR: the omission note must not assert something false ------------------------

def test_the_omission_note_respects_the_scope_filter_it_annotates(db):
    """Called with scopes=("1","2") by ESRS E1-5 and GRI 302-1, a Scope 3 energy line is
    correctly outside the own-operations kWh figure — reporting it as an omission and
    asserting its emissions ARE in Scope 1/2 shipped a false claim into a CSRD filing."""
    from app.reports.secr import _energy_kwh
    org = _org(db, "ScopedFuel")
    elec = _factor(db, 0.2, category="electricity", unit="kWh")
    flight = _factor(db, 1.5, category="flight", unit="L")     # Scope 3
    _act(db, org.id, elec, 1000.0)
    _act(db, org.id, flight, 500.0)
    run = compute_co2e(db, org.id)
    scoped = _energy_kwh(db, run, scopes=("1", "2"))
    assert "flight" not in (scoped.get("carriers_omitted") or {})
    # Across all scopes it IS an omission, and the note names the right scopes.
    allsc = _energy_kwh(db, run)
    assert "flight" in allsc["carriers_omitted"]
    assert any("Scope 1/2 figures" in n for n in allsc["notes"])


def test_the_omission_detector_catches_the_units_that_actually_appear(db):
    """m3 for gas by volume and tonne/kg for solid fuel are the most common non-allowlist
    energy units in a SECR filing, and a case-sensitive 8-entry list missed all of them."""
    from app.reports.secr import _energy_kwh
    for i, unit in enumerate(["m3", "tonne", "kg", "kwh", "l", "MJ"]):
        org = _org(db, f"UnitCo{i}")
        f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                           category="lpg", subcategory="", unit=unit, gwp_set="AR6",
                           value=1.9)
        db.add(f); db.commit(); db.refresh(f)
        _act(db, org.id, f, 500.0)
        run = compute_co2e(db, org.id)
        e = _energy_kwh(db, run)
        assert "lpg" in (e.get("carriers_omitted") or {}), f"{unit!r} went undetected"


# --- ISO 14064-2: the period gate must not fail open on a bad date ------------------

def test_an_unparseable_period_date_blocks_rather_than_skipping_the_check(db):
    """The gate that stops nine months of elapsed time reading as abatement was bypassed
    entirely by a date written 01/01/2025 instead of 2025-01-01."""
    from app.reports.iso_14064_2 import iso_14064_2_report
    org = _org(db, "BadDateCo")
    f = _factor(db, 0.17, category="electricity", unit="kWh")
    a = _act(db, org.id, f, 1000.0, date="2024-06-15")
    bp = _period(db, org.id, "FY24", "2024-01-01", "2024-12-31")
    base = compute_co2e(db, org.id, gwp_set="AR6", reporting_period_id=bp.id)
    a.quantity, a.date = 600.0, "2025-02-15"
    db.commit()
    qp = _period(db, org.id, "Q1-25", "01/01/2025", "31/03/2025")   # not ISO
    proj = compute_co2e(db, org.id, gwp_set="AR6", reporting_period_id=qp.id)
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0)
    assert r["disclosure_ready"] is False
    assert any("cannot be determined" in b for b in r["blockers"])
    # The tolerance is disclosed rather than applied silently.
    assert r["period_comparability"]["tolerance_pct"] == pytest.approx(5.0)


# --- GRI: the biogenic fix must not break the compliance verdict --------------------

def test_the_gri_biogenic_requirement_reads_the_field_that_now_carries_it(db):
    """Repointing the disclosure without repointing the compliance path made the gate
    report a requirement as unmet while the disclosure was in fact made."""
    from app.reports.compliance import evaluate
    from app.reports.gri import gri_report
    org = _org(db, "GriCo")
    f = _factor(db, 0.2, category="electricity", unit="kWh")
    _act(db, org.id, f, 1000.0)
    run = compute_co2e(db, org.id)
    payload = gri_report(db, org.id, run_id=run.id, intensity_denominator=1.0)
    ev = evaluate("gri", payload)
    bio = [x for x in ev["requirements"] if x.get("ref") == "GRI 305-1-b"]
    assert bio and bio[0]["status"] != "missing", bio


def test_the_sbti_report_actually_applies_the_financed_comparability_gate(db):
    """The gate function existing is not the same as the report calling it — the audit's
    reproduction was at report level: on_track True, blockers empty, 71% reduction from an
    organisation whose own operations had not changed."""
    from app.models import EmissionsTarget
    from app.reports.sbti import sbti_report
    org = _org(db, "WiredBank")
    f = _factor(db, 0.2, category="electricity", unit="kWh")
    _act(db, org.id, f, 1_000_000.0, date="2024-06-01")
    _position(db, org.id)
    base = compute_co2e(db, org.id, gwp_set="AR6")
    cur = compute_co2e(db, org.id, gwp_set="AR6")
    cur.financed_co2e = None                     # dimension not evaluated
    db.commit()
    t = EmissionsTarget(organisation_id=org.id, name="Net zero 2030", base_year=2024, target_year=2030,
                        target_reduction_pct=0.42, ambition="1.5C",
                        scope_coverage="1+2+3", target_type="absolute",
                        base_run_id=base.id)
    db.add(t); db.commit(); db.refresh(t)

    r = sbti_report(db, org.id, target_id=t.id, current_run_id=cur.id, current_year=2026)
    assert any("whole portfolio" in b for b in r["blockers"]), r["blockers"]
    assert r["ok"] is False
    # And the payload states whether financed emissions are inside the figures at all.
    assert "financed_emissions_included_tco2e" in r["base"]
    assert "financed_emissions_basis" in r["base"]


def test_the_sbti_payload_discloses_that_financed_emissions_are_included(db):
    """The figure changed by most of a bank's inventory; a reader must not have to infer
    it. `financed_included` was written for this and was never called by any report."""
    from app.models import EmissionsTarget
    from app.reports.sbti import sbti_report
    org = _org(db, "DisclosingBank")
    f = _factor(db, 0.2, category="electricity", unit="kWh")
    _act(db, org.id, f, 1_000_000.0, date="2024-06-01")
    _position(db, org.id)
    base = compute_co2e(db, org.id, gwp_set="AR6")
    t = EmissionsTarget(organisation_id=org.id, name="Net zero 2030", base_year=2024, target_year=2030,
                        target_reduction_pct=0.42, ambition="1.5C",
                        scope_coverage="1+2+3", target_type="absolute",
                        base_run_id=base.id)
    db.add(t); db.commit(); db.refresh(t)
    r = sbti_report(db, org.id, target_id=t.id)
    assert r["base"]["financed_emissions_included_tco2e"] == pytest.approx(500.0)
    assert "INCLUDED" in r["base"]["financed_emissions_basis"]
