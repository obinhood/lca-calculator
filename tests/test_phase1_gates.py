"""Audit Phase 1 — 'stop false disclosure_ready' regression tests.

Covers: the ESOS completeness gate (it had none), period-aware staleness (a
period-scoped run was perpetually STALE), a fingerprint that actually notices an
in-place date/category edit, and factor-drift detection (an edited factor means
the run no longer reproduces).
"""
import pytest

from app.models import (
    Organisation, ActivityRecord, EmissionFactor, ReportingPeriod, CalculationRun,
)
from app.services.calc import compute_co2e, activities_fingerprint, FINGERPRINT_VERSION
from app.reports.summary import summary, coverage
from app.reports.compliance_extra import esos_report


def _org(db, name="Co"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", unit="kWh", value=0.2):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit,
                       gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category="electricity", quantity=1000.0,
              unit="kWh", date="2025-01-01"):
    a = ActivityRecord(organisation_id=org_id, date=date, category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- ESOS completeness gate ---------------------------------------------------

def test_esos_ready_on_a_clean_run(db):
    org = _org(db)
    _activity(db, org.id, _factor(db).id)
    run = compute_co2e(db, org.id)
    r = esos_report(db, org.id, run_id=run.id)
    assert r["report_ready"] is True and r["blockers"] == []
    assert r["total_energy_kwh"] == pytest.approx(1000.0)


def test_esos_blocks_on_partial_run(db):
    org = _org(db)
    _activity(db, org.id, _factor(db).id)
    _activity(db, org.id, None, category="widgets")     # unmapped -> PARTIAL
    run = compute_co2e(db, org.id)
    r = esos_report(db, org.id, run_id=run.id)
    assert r["report_ready"] is False
    assert any("PARTIAL" in b for b in r["blockers"])


def test_esos_blocks_on_stale_run(db):
    org = _org(db)
    a = _activity(db, org.id, _factor(db).id)
    run = compute_co2e(db, org.id)
    a.quantity = 2000.0                                 # edit after the run
    db.commit()
    r = esos_report(db, org.id, run_id=run.id)
    assert r["report_ready"] is False
    assert any("STALE" in b for b in r["blockers"])


# --- Period-aware staleness (the headline fix) --------------------------------

def test_period_scoped_run_is_not_perpetually_stale(db):
    """A period run's fingerprint is taken over the IN-PERIOD activities, so the
    staleness check must compare against the same filtered set — not the org's
    whole activity list (which made every annual inventory read as STALE)."""
    org = _org(db)
    f = _factor(db)
    _activity(db, org.id, f.id, date="2025-06-01")      # inside the period
    _activity(db, org.id, f.id, date="2024-06-01")      # OUTSIDE the period
    p = ReportingPeriod(organisation_id=org.id, label="FY25",
                        start_date="2025-01-01", end_date="2025-12-31", frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert run.total_activities == 1                    # only the in-period row

    cov = coverage(db, run)
    assert cov["period_scoped"] is True
    assert cov["stale"] is False                        # was perpetually True
    assert cov["staleness_assessable"] is True


def test_period_run_goes_stale_when_an_in_period_activity_changes(db):
    org = _org(db)
    f = _factor(db)
    a = _activity(db, org.id, f.id, date="2025-06-01")
    p = ReportingPeriod(organisation_id=org.id, label="FY25",
                        start_date="2025-01-01", end_date="2025-12-31", frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert coverage(db, run)["stale"] is False
    a.quantity = 5000.0
    db.commit()
    assert coverage(db, run)["stale"] is True           # real change IS caught


# --- Fingerprint v2 catches date / category edits -----------------------------

def test_in_place_date_edit_invalidates_the_run(db):
    org = _org(db)
    a = _activity(db, org.id, _factor(db).id, date="2025-01-01")
    run = compute_co2e(db, org.id)
    assert coverage(db, run)["stale"] is False
    a.date = "2024-01-01"                               # changes period attribution
    db.commit()
    assert coverage(db, run)["stale"] is True


def test_in_place_category_edit_invalidates_the_run(db):
    org = _org(db)
    a = _activity(db, org.id, _factor(db).id, category="electricity")
    run = compute_co2e(db, org.id)
    assert coverage(db, run)["stale"] is False
    a.category = "steam"                                # changes SCOPE classification
    db.commit()
    assert coverage(db, run)["stale"] is True


def test_legacy_fingerprint_is_not_assessable_rather_than_falsely_stale(db):
    org = _org(db)
    _activity(db, org.id, _factor(db).id)
    run = compute_co2e(db, org.id)
    run.activities_fingerprint = "deadbeef"             # pre-v2 scheme
    db.commit()
    cov = coverage(db, run)
    assert cov["staleness_assessable"] is False
    assert cov["stale"] is False                        # not falsely STALE
    assert any("fingerprint scheme" in w for w in [cov["warning"] or ""])


def test_fingerprint_is_versioned(db):
    org = _org(db)
    _activity(db, org.id, _factor(db).id)
    run = compute_co2e(db, org.id)
    assert run.activities_fingerprint.startswith(f"{FINGERPRINT_VERSION}:")


# --- Factor drift -------------------------------------------------------------

def test_in_place_factor_edit_is_detected(db):
    org = _org(db)
    f = _factor(db, value=0.2)
    _activity(db, org.id, f.id)
    run = compute_co2e(db, org.id)
    assert coverage(db, run)["factor_drift"] == []
    f.value = 0.9                                       # edited in place (never do this)
    db.commit()
    cov = coverage(db, run)
    assert len(cov["factor_drift"]) == 1
    assert "0.2 -> 0.9" in cov["factor_drift"][0]
    assert "no longer reproduce" in cov["factor_drift"][0]
    assert "factor" in (cov["warning"] or "")
