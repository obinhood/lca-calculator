"""GHG Protocol Ch.5 base-year recalculation detection for SBTi trajectories.

An SBTi trajectory measured across two DIFFERENT organisational boundaries is
meaningless — as meaningless as measuring across GWP vintages. A structural change
(approach change, acquisition, divestment, ownership restatement) between the base
year and now must block the trajectory and force a re-base; organic growth (same
entities, more activity) must NOT.
"""
import pytest

from app.models import (
    Organisation, ActivityRecord, EmissionFactor, ReportingEntity, EmissionsTarget,
)
from app.services.calc import compute_co2e
from app.services.boundary import base_year_recalculation
from app.reports.sbti import sbti_report


def _org(db, name="Group", approach="operational_control"):
    o = Organisation(name=name, consolidation_approach=approach,
                     consolidation_approach_reason="Operational control basis for the group.")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _entity(db, org_id, name="Sub", **kw):
    kw.setdefault("accounting_category", "subsidiary")
    kw.setdefault("in_consolidated_accounting_group", True)
    e = ReportingEntity(organisation_id=org_id, name=name, **kw)
    db.add(e); db.commit(); db.refresh(e)
    return e


def _factor(db, value=0.5):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024, category="gas",
                       subcategory="", unit="kWh", gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id, kwh=1000.0, entity_id=None):
    a = ActivityRecord(organisation_id=org_id, date="2025-06-01", category="gas",
                       subcategory="", description="", quantity=kwh, unit="kWh",
                       geo="GB", factor_id=factor_id, entity_id=entity_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _target(db, org_id, base_run_id):
    t = EmissionsTarget(organisation_id=org_id, name="Halve by 2030", target_type="near_term",
                        scope_coverage="1+2", base_run_id=base_run_id, base_year=2020,
                        target_year=2030, target_reduction_pct=0.5, ambition="1.5C")
    db.add(t); db.commit(); db.refresh(t)
    return t


# --- Comparable: organic growth ----------------------------------------------

def test_organic_growth_does_not_trigger_recalculation(db):
    """Same entities, more activity — the trajectory IS comparable."""
    org = _org(db)
    _act(db, org.id, _factor(db).id, kwh=1000)
    base = compute_co2e(db, org.id)
    _act(db, org.id, _factor(db).id, kwh=500)          # organic growth
    current = compute_co2e(db, org.id)
    assert base_year_recalculation(db, base, current) is None
    t = _target(db, org.id, base.id)
    r = sbti_report(db, org.id, t.id, current_run_id=current.id, current_year=2025)
    assert r["ok"] is True and r["trajectory"] is not None


# --- Structural changes: not comparable --------------------------------------

def test_approach_change_triggers_recalculation(db):
    org = _org(db, approach="operational_control")
    base = compute_co2e(db, org.id)
    org.consolidation_approach = "equity_share"        # a change of boundary
    db.commit()
    current = compute_co2e(db, org.id)
    reason = base_year_recalculation(db, base, current)
    assert reason is not None and "consolidation approach changed" in reason
    t = _target(db, org.id, base.id)
    r = sbti_report(db, org.id, t.id, current_run_id=current.id, current_year=2025)
    assert r["ok"] is False
    assert any("Ch.5" in b and "approach" in b for b in r["blockers"])


def test_acquisition_of_an_entity_triggers_recalculation(db):
    org = _org(db, approach="equity_share")
    _act(db, org.id, _factor(db).id, entity_id=None)
    base = compute_co2e(db, org.id)
    # acquire a subsidiary AFTER the base year
    sub = _entity(db, org.id, name="NewCo", equity_share_pct=100.0, financial_control=True,
                  operational_control=True)
    _act(db, org.id, _factor(db).id, entity_id=sub.id)
    current = compute_co2e(db, org.id)
    reason = base_year_recalculation(db, base, current)
    assert reason is not None and "acquired, divested, or its ownership/control restated" in reason
    t = _target(db, org.id, base.id)
    r = sbti_report(db, org.id, t.id, current_run_id=current.id, current_year=2025)
    assert r["ok"] is False


def test_ownership_restatement_triggers_recalculation(db):
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, name="JV", accounting_category="joint_venture_incorporated",
                 equity_share_pct=40.0, joint_financial_control=True,
                 in_consolidated_accounting_group=False)
    _act(db, org.id, _factor(db).id, entity_id=jv.id)
    base = compute_co2e(db, org.id)
    jv.equity_share_pct = 55.0                          # ownership restated
    db.commit()
    current = compute_co2e(db, org.id)
    assert base_year_recalculation(db, base, current) is not None


def test_legacy_base_run_is_not_comparable(db):
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    base = compute_co2e(db, org.id)
    base.boundary_version = None                        # a pre-boundary base run
    db.commit()
    current = compute_co2e(db, org.id)
    reason = base_year_recalculation(db, base, current)
    assert reason is not None and "predates the GHGP organisational-boundary" in reason


def test_a_reason_only_edit_does_not_trigger_recalculation(db):
    """Editing the rationale TEXT is not a structural change — it must not false-trigger."""
    org = _org(db, approach="operational_control")
    base = compute_co2e(db, org.id)
    org.consolidation_approach_reason = "Reworded rationale, same operational-control basis."
    db.commit()
    current = compute_co2e(db, org.id)
    assert base_year_recalculation(db, base, current) is None
