"""GHG Protocol Land Sector & Removals — the inventory-removals dimension.

Removals are the org's OWN within-boundary sequestration, reported SEPARATELY from
gross emissions (never in total_co2e), distinct from purchased offset credits and
from biogenic-CO2 flux, with permanence never overclaimed.
"""
import pytest

from app.models import (
    Organisation, ActivityRecord, EmissionFactor, ReportingPeriod, RemovalRecord,
    RunRemovalLine, EmissionLineItem, ReportingEntity, CarbonCredit,
)
from app.services.calc import compute_co2e
from app.services.removals import removals_completeness
from app.reports.summary import summary
from app.reports.esrs_e1 import esrs_e1_report


def _org(db, name="Farm", approach="operational_control"):
    o = Organisation(name=name, consolidation_approach=approach,
                     consolidation_approach_reason="Operational control basis for the group.")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _period(db, org_id):
    p = ReportingPeriod(organisation_id=org_id, label="FY25", start_date="2025-01-01",
                        end_date="2025-12-31", frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _factor(db, value=0.5):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024, category="gas",
                       subcategory="", unit="kWh", gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id, kwh=1000.0):
    a = ActivityRecord(organisation_id=org_id, date="2025-06-01", category="gas", subcategory="",
                       description="", quantity=kwh, unit="kWh", geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _removal(db, org_id, period_id, category="technological", qty=5.0, **kw):
    kw.setdefault("method", "dac" if category == "technological" else "afforestation")
    kw.setdefault("scope", "1")
    kw.setdefault("quantification_method", "metered" if category == "technological" else "gain_loss")
    kw.setdefault("as_of_date", "2025-06-01")
    if category == "technological":
        kw.setdefault("expected_durability_years", 1000)
        kw.setdefault("monitoring_method", "continuous CO2 metering at the storage site")
        kw.setdefault("reversal_accounting", "permanent geological storage")
    r = RemovalRecord(organisation_id=org_id, reporting_period_id=period_id,
                      removal_category=category, quantity_tco2e=qty, **kw)
    db.add(r); db.commit(); db.refresh(r)
    return r


# --- Core invariant: removals are SEPARATE, never in total_co2e ---------------

def test_removals_are_never_in_total_co2e(db):
    org = _org(db); p = _period(db, org.id)
    _act(db, org.id, _factor(db, 0.5).id, kwh=1000)            # 500 kg emissions
    _removal(db, org.id, p.id, category="technological", qty=5.0)   # 5000 kg removal
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert run.total_co2e == pytest.approx(500.0)             # emissions ONLY
    assert run.total_removals_co2e == pytest.approx(5000.0)   # a separate pool
    lines = db.query(EmissionLineItem).filter_by(run_id=run.id, method="location").all()
    assert sum(l.co2e for l in lines) == pytest.approx(run.total_co2e)   # the invariant


def test_summary_and_esrs_report_gross_removals_net_separately(db):
    org = _org(db); p = _period(db, org.id)
    _act(db, org.id, _factor(db, 0.5).id, kwh=2000)           # 1000 kg = 1 tCO2e
    _removal(db, org.id, p.id, category="technological", qty=0.4)   # 400 kg
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    s = summary(db, organisation_id=org.id, run_id=run.id)
    assert s["total_co2e"] == pytest.approx(1000.0)          # gross headline unchanged
    assert s["removals_co2e_separate"] == pytest.approx(400.0)
    assert s["net_co2e_after_removals_kg"] == pytest.approx(600.0)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    e = r["e1_6_gross_ghg_emissions_tco2e"]
    assert e["scope1"] == pytest.approx(1.0)                 # gross key UNCHANGED
    assert e["inventory_removals_tco2e"] == pytest.approx(0.4)
    assert e["net_of_removals_tco2e"] == pytest.approx(0.6)
    # E1-7 keeps inventory removals and purchased credits DISTINCT
    inv = r["e1_7_removals_and_credits"]["inventory_removals"]
    assert inv["own_operations_tco2e"] == pytest.approx(0.4)


# --- Entity weighting ---------------------------------------------------------

def test_removal_on_a_jv_enters_at_the_entity_share(db):
    org = _org(db, approach="equity_share")
    p = _period(db, org.id)
    jv = ReportingEntity(organisation_id=org.id, name="JV", accounting_category="joint_venture_incorporated",
                         equity_share_pct=40.0, joint_financial_control=True,
                         in_consolidated_accounting_group=False)
    db.add(jv); db.commit(); db.refresh(jv)
    _removal(db, org.id, p.id, category="technological", qty=10.0, entity_id=jv.id)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert run.total_removals_co2e == pytest.approx(4000.0)  # 10 t x 1000 x 0.40
    line = db.query(RunRemovalLine).filter_by(run_id=run.id).one()
    d = __import__("json").loads(line.details)
    assert d["consolidation"]["share_factor"] == pytest.approx(0.40)


# --- Reversals ----------------------------------------------------------------

def test_reversal_reduces_net_without_restating_the_prior_run(db):
    org = _org(db); p = _period(db, org.id)
    orig = _removal(db, org.id, p.id, category="technological", qty=10.0)
    base = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert base.total_removals_co2e == pytest.approx(10000.0)
    # a reversal occurs (some stored carbon re-emitted)
    _removal(db, org.id, p.id, category="technological", qty=3.0, record_kind="reversal",
             reverses_record_id=orig.id)
    current = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert current.total_removals_co2e == pytest.approx(10000.0)   # gross removals
    assert current.removals_reversed_co2e == pytest.approx(3000.0)
    g = removals_completeness(db, current)
    assert g["net_removals_kg"] == pytest.approx(7000.0)           # gross - reversal
    # the earlier filed run is untouched
    assert base.removals_reversed_co2e in (0.0, None)


# --- Permanence gate ----------------------------------------------------------

def test_land_based_removal_without_monitoring_blocks(db):
    org = _org(db); p = _period(db, org.id)
    _removal(db, org.id, p.id, category="land_based", qty=5.0,
             monitoring_method=None, reversal_accounting=None)   # no permanence basis
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    g = removals_completeness(db, run)
    assert any("not reportable" in b for b in g["blockers"])
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    assert r["disclosure_ready"] is False


def test_technological_removal_missing_monitoring_is_a_warning_not_a_block(db):
    org = _org(db); p = _period(db, org.id)
    _removal(db, org.id, p.id, category="technological", qty=5.0, monitoring_method=None,
             reversal_accounting="permanent geological storage")
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    g = removals_completeness(db, run)
    assert g["blockers"] == []
    assert any("no monitoring_method" in w for w in g["warnings"])


# --- Double-count with purchased credits --------------------------------------

def test_removal_also_sold_as_a_credit_blocks(db):
    org = _org(db); p = _period(db, org.id)
    db.add(CarbonCredit(organisation_id=org.id, registry="puro", serial_number="PURO-123",
                        quantity_tco2e=5.0, credit_type="removal"))
    db.commit()
    _removal(db, org.id, p.id, category="technological", qty=5.0,
             credit_registry="puro", credit_serial_if_sold="PURO-123")   # same tonne, sold
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert any("both an inventory removal and a credit" in b
               for b in removals_completeness(db, run)["blockers"])


def test_attribute_not_retained_blocks(db):
    org = _org(db); p = _period(db, org.id)
    _removal(db, org.id, p.id, category="technological", qty=5.0, attribute_retained=False)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert any("attribute_retained=false" in b
               for b in removals_completeness(db, run)["blockers"])


# --- Reproduction / false-zero / legacy ---------------------------------------

def test_editing_the_ledger_after_the_run_is_detected(db):
    org = _org(db); p = _period(db, org.id)
    rec = _removal(db, org.id, p.id, category="technological", qty=5.0)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert removals_completeness(db, run)["blockers"] == []
    rec.quantity_tco2e = 50.0                                 # restate after filing
    db.commit()
    assert run.total_removals_co2e == pytest.approx(5000.0)   # frozen figure unchanged
    assert any("ledger changed since this run" in b
               for b in removals_completeness(db, run)["blockers"])


def test_false_zero_as_of_leaves_none_not_zero(db):
    org = _org(db); p = _period(db, org.id)
    _removal(db, org.id, p.id, category="technological", qty=5.0, as_of_date="2026-01-01")
    run = compute_co2e(db, org.id, reporting_period_id=p.id, removals_as_of="2025-06-30")
    assert run.total_removals_co2e is None                    # NOT 0.0
    assert any("excluded every removal record" in b
               for b in removals_completeness(db, run)["blockers"])


def test_period_bound_removal_without_period_blocks(db):
    org = _org(db)
    _removal(db, org.id, None, category="technological", qty=5.0)
    run = compute_co2e(db, org.id)                            # no reporting period
    assert any("period-bound" in b for b in removals_completeness(db, run)["blockers"])


def test_legacy_run_not_assessable_only_if_records_exist(db):
    org = _org(db); p = _period(db, org.id)
    _removal(db, org.id, p.id, category="technological", qty=5.0)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    run.removals_lsrg_version = None                          # simulate a pre-dimension run
    db.commit()
    assert removals_completeness(db, run)["assessable"] is False


def test_non_removals_run_is_unaffected(db):
    org = _org(db); p = _period(db, org.id)
    _act(db, org.id, _factor(db).id)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert run.total_removals_co2e is None                    # not evaluated, not zero
    g = removals_completeness(db, run)
    assert g["assessable"] is True and g["blockers"] == []
    assert summary(db, organisation_id=org.id, run_id=run.id)["removals_co2e_separate"] is None


def test_run_for_a_period_with_no_removals_is_not_false_legacy(db):
    """Review finding #1: a removal in FY24 must not false-flag an FY25 run (no FY25
    removals) as 'pre-dimension' — the legacy check is period-scoped like auto-detect."""
    org = _org(db)
    fy24 = _period(db, org.id)
    fy25 = ReportingPeriod(organisation_id=org.id, label="FY26", start_date="2026-01-01",
                           end_date="2026-12-31", frozen=False)
    db.add(fy25); db.commit(); db.refresh(fy25)
    _removal(db, org.id, fy24.id, category="technological", qty=5.0)   # a FY24 removal only
    _act(db, org.id, _factor(db).id)
    run = compute_co2e(db, org.id, reporting_period_id=fy25.id)        # FY25 run, no FY25 removals
    g = removals_completeness(db, run)
    assert g["assessable"] is True                                    # NOT a false legacy block
    assert g["blockers"] == []
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    assert not any("predates the GHGP Land Sector" in b for b in r["blockers"])


def test_post_filing_sale_of_a_removal_is_detected(db):
    """Review finding #2: filing a clean removal then selling it (edit record + mint
    credit) must not re-render clean — the fingerprint now hashes the sale fields."""
    org = _org(db); p = _period(db, org.id)
    rec = _removal(db, org.id, p.id, category="technological", qty=5.0)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert removals_completeness(db, run)["blockers"] == []          # clean as filed
    # later: sell the same tonne — record it as also a credit
    db.add(CarbonCredit(organisation_id=org.id, registry="puro", serial_number="SOLD-1",
                        quantity_tco2e=5.0, credit_type="removal"))
    rec.attribute_retained = False
    rec.credit_registry = "puro"
    rec.credit_serial_if_sold = "SOLD-1"
    db.commit()
    # the same FILED run now trips the forgery gate (ledger changed since it froze)
    assert any("ledger changed since this run" in b
               for b in removals_completeness(db, run)["blockers"])
    # ...and recomputing surfaces the double-claim directly
    run2 = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert any("both an inventory removal and a credit" in b or "attribute_retained=false" in b
               for b in removals_completeness(db, run2)["blockers"])


def test_removal_does_not_touch_biogenic_pool(db):
    """Separation: a removal is not biogenic CO2."""
    org = _org(db); p = _period(db, org.id)
    _removal(db, org.id, p.id, category="land_based", qty=5.0,
             monitoring_method="annual forest inventory", reversal_accounting="20% buffer pool")
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert (run.total_biogenic_co2e or 0.0) == pytest.approx(0.0)
    assert run.total_removals_co2e == pytest.approx(5000.0)
