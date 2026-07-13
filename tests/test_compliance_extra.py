import pytest

from app.models import EmissionFactor, ActivityRecord, Organisation, TaxonomyActivity
from app.services.calc import compute_co2e
from app.reports.compliance_extra import taxonomy_report, ets_mrv_report, esos_report


def _org(db, name="Co"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit, gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category, quantity, unit):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- EU Taxonomy ---

def test_taxonomy_alignment_kpis(db):
    org = _org(db)
    # aligned activity (all four gates pass)
    db.add(TaxonomyActivity(organisation_id=org.id, name="green", reporting_year=2025,
                            turnover=60, capex=40, opex=10, eligible=True,
                            substantial_contribution=True, dnsh_pass=True,
                            minimum_safeguards_pass=True))
    # eligible but not aligned (DNSH fails)
    db.add(TaxonomyActivity(organisation_id=org.id, name="amber", reporting_year=2025,
                            turnover=40, capex=60, opex=90, eligible=True,
                            substantial_contribution=True, dnsh_pass=False,
                            minimum_safeguards_pass=True))
    db.commit()
    r = taxonomy_report(db, org.id, 2025)
    assert r["disclosure_ready"] is True
    assert r["turnover"]["eligible_pct"] == pytest.approx(100.0)   # both eligible
    assert r["turnover"]["aligned_pct"] == pytest.approx(60.0)     # only green aligned (60/100)
    assert r["capex"]["aligned_pct"] == pytest.approx(40.0)        # 40/100
    assert r["opex"]["aligned_pct"] == pytest.approx(10.0)         # 10/100


def test_taxonomy_blocks_when_empty(db):
    org = _org(db)
    r = taxonomy_report(db, org.id, 2025)
    assert r["disclosure_ready"] is False


# --- ETS MRV ---

def test_ets_mrv_reports_scope1_and_requires_verification(db):
    org = _org(db)
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 10000, "kWh")   # 1840 kg S1
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.17).id, "electricity", 1000, "kWh")  # S2, excluded
    run = compute_co2e(db, org.id)
    r = ets_mrv_report(db, org.id, "EU ETS", run_id=run.id, verified=False)
    assert r["direct_emissions_tco2e"] == pytest.approx(1.84)      # Scope 1 only
    assert r["report_ready"] is False                             # needs verification
    assert any("verification" in b for b in r["blockers"])
    r2 = ets_mrv_report(db, org.id, "EU ETS", run_id=run.id, verified=True)
    assert r2["report_ready"] is True


def test_ets_mrv_blocks_partial(db):
    org = _org(db)
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 100, "kWh")
    _activity(db, org.id, None, "widgets", 5, "kg")
    run = compute_co2e(db, org.id)
    r = ets_mrv_report(db, org.id, "EU ETS", run_id=run.id, verified=True)
    assert r["report_ready"] is False and any("PARTIAL" in b for b in r["blockers"])


# --- ESOS ---

def test_esos_energy_and_significant_use(db):
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.17).id, "electricity", 3000, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 1000, "kWh")
    run = compute_co2e(db, org.id)
    r = esos_report(db, org.id, run_id=run.id)
    assert r["report_ready"] is True
    assert r["total_energy_kwh"] == pytest.approx(4000.0)
    assert r["by_carrier_kwh"]["electricity"] == pytest.approx(3000.0)
    assert r["significant_energy_use_pct"]["electricity"] == pytest.approx(75.0)


def test_reports_are_org_scoped(db):
    org_a, org_b = _org(db, "A"), _org(db, "B")
    db.add(TaxonomyActivity(organisation_id=org_b.id, name="x", reporting_year=2025,
                            turnover=100, eligible=True, substantial_contribution=True,
                            dnsh_pass=True, minimum_safeguards_pass=True))
    db.commit()
    r = taxonomy_report(db, org_a.id, 2025)
    assert r["activities"] == 0 and r["disclosure_ready"] is False
