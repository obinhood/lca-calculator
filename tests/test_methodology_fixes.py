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


# --- money summed across currencies -------------------------------------------------
# Three disclosed ratios divided a sum of amounts in DIFFERENT currencies by a figure in
# one. The sum is not a quantity, so neither is the ratio: a JPY position counted equal
# to a USD one produced "133% of gross exposure covered" against a true ~67%.

def _fx(db, base, quote, year, rate):
    from app.models import FxRate
    row = FxRate(base_currency=base, quote_currency=quote, year=year, rate=rate)
    db.add(row); db.commit(); db.refresh(row)
    return row


def _fpos(db, org_id, currency, outstanding, denom=10_000_000.0, s1=5000.0,
          revenue=None, as_of="2025-01-01"):
    p = FinancedPosition(organisation_id=org_id, investee_name=f"{currency} investee",
                         asset_class="listed_equity", currency=currency,
                         outstanding_amount=outstanding, attribution_denominator=denom,
                         investee_scope1_tco2e=s1, investee_scope2_tco2e=0.0,
                         investee_scope3_tco2e=0.0, investee_revenue_millions=revenue,
                         data_quality_score=2, as_of_date=as_of)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _cat15_run(db, org, gross_total=1_500_000.0, gross_currency="USD"):
    """A period-scoped, screened run whose Cat 15 screen declares a gross exposure."""
    from app.models import Scope3CategoryDeclaration
    from tests.scope3_util import ready_run
    _run, p = ready_run(db, org.id)
    d = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id, category=15).first()
    d.gross_exposure_total = gross_total
    d.gross_exposure_currency = gross_currency
    db.commit()
    return compute_co2e(db, org.id, reporting_period_id=p.id), p


def _financed(db, run):
    from app.reports.scope3 import scope3_by_ghgp_category
    return scope3_by_ghgp_category(db, run)["categories"]["15"]["financed_emissions"]


def test_mixed_currency_exposure_is_refused_not_summed_into_133_pct_coverage(db):
    """The audit's own case: 1,000,000 JPY + 1,000,000 USD of covered exposure against a
    1,500,000 USD declared gross returned exposure_covered 2,000,000 and
    pct_gross_exposure_covered 133.33 — a share of a gross exposure that cannot exceed
    100% by construction, and a figure no reader could reconcile. With no JPY->USD rate
    loaded the ratio is refused, and the per-currency exposure (which needs no rate) is
    disclosed in its place."""
    org = _org(db, "MultiCcyBank")
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0)
    _fpos(db, org.id, "JPY", 1_000_000.0)
    _fpos(db, org.id, "USD", 1_000_000.0)
    run, _p = _cat15_run(db, org)
    fin = _financed(db, run)

    assert fin["exposure_covered_by_currency"] == {"JPY": 1_000_000.0, "USD": 1_000_000.0}
    assert fin["exposure_covered"] is None                    # was 2,000,000.0
    assert fin["pct_gross_exposure_covered"] is None           # was 133.33
    assert "JPY->USD" in fin["pct_gross_exposure_covered_refused_reason"]
    assert "guessed rate" in fin["pct_gross_exposure_covered_refused_reason"]
    # The emissions themselves are unaffected: the attribution factor is a same-currency
    # ratio, so only the disclosed coverage ratio was ever wrong.
    assert fin["tco2e"] == pytest.approx(1000.0)               # 2 x (0.1 x 5000)


def test_a_loaded_rate_converts_the_exposure_instead_of_refusing_it(db):
    """With the rate on file the ratio is computed, not approximated: 1,000,000 JPY at
    0.0065 = 6,500 USD, so 1,006,500 of 1,500,000 USD is covered — ~67%, not 133%."""
    org = _org(db, "RatedBank")
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0)
    _fpos(db, org.id, "JPY", 1_000_000.0)
    _fpos(db, org.id, "USD", 1_000_000.0)
    rate_row = _fx(db, "JPY", "USD", 2025, 0.0065)
    run, _p = _cat15_run(db, org)
    fin = _financed(db, run)

    assert fin["exposure_covered"] == pytest.approx(1_006_500.0)
    assert fin["exposure_covered_currency"] == "USD"
    assert fin["pct_gross_exposure_covered"] == pytest.approx(67.1)
    assert fin["pct_gross_exposure_covered_refused_reason"] is None
    # WHICH rate row was applied is part of the answer — fx_rates is append-only.
    conv = fin["exposure_conversions"]
    assert len(conv) == 1
    assert (conv[0]["from"], conv[0]["to"], conv[0]["rate"]) == ("JPY", "USD", 0.0065)
    assert conv[0]["fx_rate_id"] == rate_row.id and conv[0]["fx_year"] == 2025


def test_a_later_fx_correction_does_not_move_a_filed_percentage(db):
    """scope3.py's reproduction contract: the conversion is frozen onto the run, so
    correcting the rate afterwards (an INSERT, since fx_rates is append-only) cannot
    restate a filing that has already gone out."""
    org = _org(db, "FrozenFxBank")
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0)
    _fpos(db, org.id, "JPY", 1_000_000.0)
    _fpos(db, org.id, "USD", 1_000_000.0)
    _fx(db, "JPY", "USD", 2025, 0.0065)
    run, _p = _cat15_run(db, org)
    before = _financed(db, run)["pct_gross_exposure_covered"]

    _fx(db, "JPY", "USD", 2025, 0.0100)          # corrected rate, filed run untouched
    assert _financed(db, run)["pct_gross_exposure_covered"] == pytest.approx(before)


def test_issb_s2_blocks_when_the_coverage_ratio_had_to_be_refused(db):
    """¶B58-B63 wants financed emissions WITH the gross exposure and the % covered. An
    absent percentage already blocked when the gross was missing; a gross that the
    positions cannot be compared against is the same defect, and the S2 payload must not
    read as disclosure-ready without it."""
    from app.reports.issb_s2 import issb_s2_report
    org = _org(db, "S2MixedBank")
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0)
    _fpos(db, org.id, "JPY", 1_000_000.0)
    _fpos(db, org.id, "USD", 1_000_000.0)
    run, _p = _cat15_run(db, org)
    r = issb_s2_report(db, org.id, run_id=run.id)
    assert r["disclosure_ready"] is False
    assert any("% of gross exposure covered cannot be reported" in b for b in r["blockers"])

    # With the rate loaded the same filing is ready again.
    _fx(db, "JPY", "USD", 2025, 0.0065)
    run2, _p2 = _cat15_run(db, org)
    r2 = issb_s2_report(db, org.id, run_id=run2.id)
    assert not [b for b in r2["blockers"] if "gross exposure" in b]
    assert r2["disclosure_ready"] is True


def test_restating_a_position_currency_flags_the_filed_run_as_changed(db):
    """The currency is now an input to a DISCLOSED figure, so it belongs in the staleness
    fingerprint: restating a position from USD to JPY changes the % covered, and a filed
    run must not keep quoting the old one as still matching the ledger."""
    from app.services.ghgp import scope3_completeness
    org = _org(db, "RestatedCcyBank")
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0)
    pos = _fpos(db, org.id, "USD", 1_000_000.0)
    run, _p = _cat15_run(db, org)
    assert not [b for b in scope3_completeness(db, run)["blockers"] if "changed since" in b]

    pos.currency = "JPY"                          # same amount, different money
    db.commit()
    assert [b for b in scope3_completeness(db, run)["blockers"] if "changed since" in b]


def test_a_legacy_fingerprint_is_compared_under_its_own_version(db):
    """Anti-cliff: adding `currency` to the hash must not read as an edit on every run
    frozen before it — a v1 fingerprint is re-derived as v1."""
    from app.services.calc import _financed_fingerprint
    from app.services.ghgp import scope3_completeness
    org = _org(db, "LegacyFpBank")
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0)
    _fpos(db, org.id, "USD", 1_000_000.0)
    run, _p = _cat15_run(db, org)
    positions = db.query(FinancedPosition).filter_by(organisation_id=org.id).all()
    run.financed_fingerprint = _financed_fingerprint(positions, "v1")   # as a v1 run froze it
    db.commit()
    assert not [b for b in scope3_completeness(db, run)["blockers"] if "changed since" in b]


def test_single_currency_exposure_needs_no_rate_and_is_unchanged(db):
    """The fix must not withhold a ratio that was always well-defined: one currency, no
    conversion, no refusal."""
    org = _org(db, "OneCcyBank")
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0)
    _fpos(db, org.id, "USD", 1_000_000.0)
    run, _p = _cat15_run(db, org)
    fin = _financed(db, run)

    assert fin["exposure_covered"] == pytest.approx(1_000_000.0)
    assert fin["exposure_covered_currency"] == "USD"
    assert fin["pct_gross_exposure_covered"] == pytest.approx(66.67)
    assert fin["exposure_conversions"] is None
    assert fin["exposure_covered_refused_reason"] is None
    assert fin["pct_gross_exposure_covered_refused_reason"] is None


def test_pai3_value_weights_across_currencies_are_refused_without_a_rate(db):
    """PAI 3 weights each investee's intensity by its outstanding amount. Weighting
    1,000,000 JPY equally with 1,000,000 EUR over-weights the JPY investee ~150x, which
    is a different indicator from the one SFDR defines."""
    from app.reports.sfdr_pai import sfdr_pai_report
    org = _org(db, "PaiBank")
    _fpos(db, org.id, "EUR", 1_000_000.0, s1=1000.0, revenue=50.0)    # intensity 20
    _fpos(db, org.id, "JPY", 1_000_000.0, s1=5000.0, revenue=100.0)   # intensity 50
    r = sfdr_pai_report(db, org.id, portfolio_value_millions=2.0)
    pai3 = r["pai_3_ghg_intensity_of_investees"]

    assert pai3["value_weighted_tco2e_per_eur_million_revenue"] is None   # was 35.0
    assert "JPY->EUR" in pai3["refused_reason"]
    assert any("PAI 3 refused" in b for b in r["blockers"])
    assert r["ok"] is False


def test_pai3_weights_convert_to_eur_when_a_rate_is_loaded(db):
    """1,000,000 JPY at 0.006 is 6,000 EUR of weight, not 1,000,000 — so the JPY
    investee's intensity moves the average by 0.6%, not by half of it."""
    from app.reports.sfdr_pai import sfdr_pai_report
    org = _org(db, "PaiRatedBank")
    _fpos(db, org.id, "EUR", 1_000_000.0, s1=1000.0, revenue=50.0)
    _fpos(db, org.id, "JPY", 1_000_000.0, s1=5000.0, revenue=100.0)
    _fx(db, "JPY", "EUR", 2025, 0.006)
    r = sfdr_pai_report(db, org.id, portfolio_value_millions=2.0)
    pai3 = r["pai_3_ghg_intensity_of_investees"]

    # (1,000,000 x 20 + 6,000 x 50) / 1,006,000
    assert pai3["value_weighted_tco2e_per_eur_million_revenue"] == pytest.approx(
        (1_000_000 * 20.0 + 6_000 * 50.0) / 1_006_000, abs=1e-5)
    assert pai3["weighting_currency"] == "EUR"
    assert pai3["weighting_conversions"][0]["rate"] == 0.006
    assert pai3["refused_reason"] is None


def test_pai3_single_currency_average_is_unchanged(db):
    """One currency: the weights are a ratio, so the average is invariant to which
    currency it is and no rate is needed."""
    from app.reports.sfdr_pai import sfdr_pai_report
    org = _org(db, "PaiOneCcy")
    _fpos(db, org.id, "GBP", 1_000_000.0, s1=1000.0, revenue=50.0)
    _fpos(db, org.id, "GBP", 3_000_000.0, s1=5000.0, revenue=100.0)
    r = sfdr_pai_report(db, org.id, portfolio_value_millions=2.0)
    pai3 = r["pai_3_ghg_intensity_of_investees"]
    assert pai3["value_weighted_tco2e_per_eur_million_revenue"] == pytest.approx(
        (1_000_000 * 20.0 + 3_000_000 * 50.0) / 4_000_000)
    assert pai3["weighting_currency"] == "GBP"
    assert pai3["weighting_conversions"] is None


def test_pai2_denominator_currency_is_checked_not_assumed_to_be_eur(db):
    """PAI 2 is stated per EUR million invested. A portfolio value supplied in USD was
    divided in and labelled EUR regardless — a 10% wrong headline indicator."""
    from app.reports.sfdr_pai import sfdr_pai_report
    org = _org(db, "Pai2Bank")
    _fpos(db, org.id, "EUR", 1_000_000.0, s1=1000.0, revenue=50.0)

    r = sfdr_pai_report(db, org.id, portfolio_value_millions=10.0,
                        portfolio_value_currency="USD")
    assert r["pai_2_carbon_footprint"] is None
    assert any("PAI 2 refused" in b and "USD" in b for b in r["blockers"])

    _fx(db, "USD", "EUR", 2025, 0.9)
    r = sfdr_pai_report(db, org.id, portfolio_value_millions=10.0,
                        portfolio_value_currency="USD")
    pai2 = r["pai_2_carbon_footprint"]
    assert pai2["portfolio_value_currency"] == "USD"
    assert pai2["portfolio_value_millions_eur"] == pytest.approx(9.0)
    # 100 tCO2e financed (0.1 x 1000) over 9 EUR million, not over 10.
    assert pai2["tco2e_per_eur_million_invested"] == pytest.approx(100.0 / 9.0, abs=1e-5)
    assert pai2["portfolio_value_fx"]["rate"] == 0.9


def test_pai2_in_eur_is_unchanged_and_needs_no_rate(db):
    from app.reports.sfdr_pai import sfdr_pai_report
    org = _org(db, "Pai2Eur")
    _fpos(db, org.id, "EUR", 1_000_000.0, s1=1000.0, revenue=50.0)
    r = sfdr_pai_report(db, org.id, portfolio_value_millions=10.0)
    pai2 = r["pai_2_carbon_footprint"]
    assert pai2["tco2e_per_eur_million_invested"] == pytest.approx(10.0)
    assert pai2["portfolio_value_millions_eur"] == pytest.approx(10.0)
    assert pai2["portfolio_value_fx"] is None


# --- E1-5: a share whose numerator and denominator are on different boundaries -------

def test_esrs_renewable_share_is_on_the_same_boundary_as_the_energy_total(db):
    """The instrument pool is consumed in GROSS kWh (a REC covers physical MWh), while
    E1-5's total_mwh follows the consolidation scope. For a 40%-held JV that put a 1.0 MWh
    gross numerator over a 0.4 MWh consolidated denominator — a 250% renewable share
    sitting inside a filed CSRD payload."""
    from app.models import ReportingEntity, MarketInstrument
    from app.reports.esrs_e1 import esrs_e1_report
    org = Organisation(name="JVCo", consolidation_approach="equity_share")
    db.add(org); db.commit(); db.refresh(org)
    jv = ReportingEntity(organisation_id=org.id, name="JV",
                         accounting_category="joint_venture_incorporated",
                         equity_share_pct=40.0, joint_financial_control=True,
                         in_consolidated_accounting_group=False)
    db.add(jv); db.commit(); db.refresh(jv)
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0,
         entity_id=jv.id)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=1000.0,
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    energy = esrs_e1_report(db, org.id, run_id=run.id,
                            net_revenue_millions=1.0)["e1_5_energy_consumption"]

    assert energy["by_carrier_mwh"]["electricity"] == pytest.approx(0.4)   # 40% of 1 MWh
    # The physical instrument volume is still disclosed — labelled as gross.
    assert energy["electricity_renewable_contractual_gross_mwh"] == pytest.approx(1.0)
    # ...but the figure reported beside the consolidated total is on that basis.
    assert energy["electricity_renewable_contractual_mwh"] == pytest.approx(0.4)
    assert energy["electricity_renewable_share_pct"] == pytest.approx(100.0)  # was 250.0


def test_esrs_renewable_share_unchanged_for_a_wholly_owned_org(db):
    """No partial entity, no weighting: the gross and consolidated figures coincide."""
    from app.models import MarketInstrument
    from app.reports.esrs_e1 import esrs_e1_report
    org = _org(db, "WhollyOwned")
    _act(db, org.id, _factor(db, 0.2, category="electricity", unit="kWh"), 1000.0)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=700.0,
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    energy = esrs_e1_report(db, org.id, run_id=run.id,
                            net_revenue_millions=1.0)["e1_5_energy_consumption"]
    assert energy["electricity_renewable_contractual_mwh"] == pytest.approx(0.7)
    assert energy["electricity_renewable_contractual_gross_mwh"] == pytest.approx(0.7)
    assert energy["electricity_renewable_share_pct"] == pytest.approx(70.0)
