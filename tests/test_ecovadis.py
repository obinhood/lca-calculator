"""EcoVadis Environment-theme readiness: evidence, gaps, gates, and honesty.

The report must never imply a score/medal, must fail closed on a partial/stale
inventory, and must be org-scoped (a baseline run from another tenant is not
readable).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401
from app.models import (
    Organisation, ActivityRecord, EmissionFactor, MarketInstrument,
    EmissionsTarget, AssuranceEngagement,
)
from app.services.calc import compute_co2e
from app.reports.ecovadis import ecovadis_readiness
from app import main as main_mod


def _org(db, name="Co"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", unit="kWh", value=0.5):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit,
                       gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category="electricity", quantity=1000.0, unit="kWh"):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- Gates --------------------------------------------------------------------

def test_no_run_is_not_ready(db):
    r = ecovadis_readiness(db, _org(db).id)
    assert r["assessment_ready"] is False
    assert "no calculation run" in r["blockers"][0]


def test_partial_run_blocks(db):
    org = _org(db)
    _activity(db, org.id, _factor(db).id)
    _activity(db, org.id, None, category="widgets")   # unmapped -> PARTIAL
    compute_co2e(db, org.id)
    r = ecovadis_readiness(db, org.id)
    assert r["assessment_ready"] is False
    assert any("PARTIAL" in b for b in r["blockers"])


# --- Evidence -----------------------------------------------------------------

def test_full_evidence_pack(db):
    org = _org(db)
    _activity(db, org.id, _factor(db, value=0.5).id, quantity=1000)   # 500 kg Scope 2
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=1000.0, gwp_set="AR6",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    db.add(EmissionsTarget(organisation_id=org.id, name="halve by 2030",
                           target_type="near_term", scope_coverage="1+2",
                           base_run_id=run.id, base_year=2025, target_year=2030,
                           target_reduction_pct=0.5, ambition="1.5C", sbti_validated=True))
    db.add(AssuranceEngagement(organisation_id=org.id, run_id=run.id,
                               standard="ISAE_3410", level="limited",
                               assuror_name="Auditor LLP", materiality_pct=5.0,
                               status="concluded", opinion="unqualified"))
    db.commit()

    r = ecovadis_readiness(db, org.id, intensity_denominator=2.0,
                           denominator_unit="GBP million revenue",
                           has_environmental_policy=True, iso_14001_certified=True,
                           published_sustainability_report=True)
    assert r["assessment_ready"] is True and r["blockers"] == []

    # Results: dual Scope 2 reported; market lowered by the REC
    k = r["kpis"]
    assert k["scope2_location_tco2e"] == pytest.approx(0.5)
    assert k["scope2_market_tco2e"] == pytest.approx(0.0)
    assert k["energy_kwh"] == pytest.approx(1000.0)
    assert k["intensity_tco2e_per_denominator"] == pytest.approx(0.25)  # 0.5 t / 2.0

    # Actions: the procurement saving is real evidence
    actions = " ".join(r["pillars"]["actions"]["evidence"])
    assert "zero-carbon electricity contract" in actions
    assert "reduces market-based Scope 2 by 0.500 tCO2e" in actions

    # Policies: the SBTi-validated target
    policies = " ".join(r["pillars"]["policies"]["evidence"])
    assert "SBTi VALIDATED" in policies and "50.0% reduction" in policies

    # Reporting & verification: concluded assurance
    rv = " ".join(r["pillars"]["reporting_and_verification"]["evidence"])
    assert "ISAE_3410" in rv and "unqualified" in rv and "Auditor LLP" in rv


def test_gaps_surface_when_nothing_evidenced(db):
    org = _org(db)
    _activity(db, org.id, _factor(db).id)
    compute_co2e(db, org.id)
    r = ecovadis_readiness(db, org.id)
    gaps = " ".join(r["all_gaps"])
    assert "no quantified emissions-reduction target" in gaps
    assert "no third-party assurance" in gaps
    assert "no zero-carbon electricity procurement" in gaps
    assert "no Scope 3 emissions reported" in gaps
    assert r["pillars"]["policies"]["status"] == "missing"


# --- Trend --------------------------------------------------------------------

def test_measured_reduction_is_evidence(db):
    org = _org(db)
    f = _factor(db, value=0.5)
    a = _activity(db, org.id, f.id, quantity=1000)     # 500 kg
    base = compute_co2e(db, org.id)
    a.quantity = 500.0                                  # halve consumption
    db.commit()
    compute_co2e(db, org.id)                            # 250 kg
    r = ecovadis_readiness(db, org.id, baseline_run_id=base.id)
    assert r["trend_vs_baseline"]["direction"] == "reduction"
    assert r["trend_vs_baseline"]["change_pct"] == pytest.approx(-50.0)
    assert any("Measured reduction of 50.0%" in e for e in r["pillars"]["actions"]["evidence"])


def test_increase_is_a_gap_not_evidence(db):
    org = _org(db)
    f = _factor(db, value=0.5)
    a = _activity(db, org.id, f.id, quantity=500)
    base = compute_co2e(db, org.id)
    a.quantity = 1000.0                                 # emissions went UP
    db.commit()
    compute_co2e(db, org.id)
    r = ecovadis_readiness(db, org.id, baseline_run_id=base.id)
    assert r["trend_vs_baseline"]["direction"] == "increase"
    assert any("no reduction demonstrated" in g for g in r["pillars"]["actions"]["gaps"])


def test_baseline_run_is_org_scoped(db):
    a_org, b_org = _org(db, "A"), _org(db, "B")
    _activity(db, b_org.id, _factor(db).id)
    b_run = compute_co2e(db, b_org.id)
    _activity(db, a_org.id, _factor(db).id)
    compute_co2e(db, a_org.id)
    r = ecovadis_readiness(db, a_org.id, baseline_run_id=b_run.id)   # other tenant's run
    assert r["assessment_ready"] is False
    assert any("baseline_run_id not found" in b for b in r["blockers"])


# --- Honesty ------------------------------------------------------------------

def test_never_claims_a_score_or_the_other_themes(db):
    org = _org(db)
    _activity(db, org.id, _factor(db).id)
    compute_co2e(db, org.id)
    r = ecovadis_readiness(db, org.id)
    blob = str(r).lower()
    assert "medal" not in blob or "only ecovadis issues it" in blob
    for k in ("score", "medal"):
        assert k not in r["kpis"]
    not_assessed = " ".join(r["not_assessed"]).lower()
    assert "labour & human rights" in not_assessed
    assert "ethics" in not_assessed
    assert "sustainable procurement" in not_assessed
    assert "only ecovadis issues it" in not_assessed


# --- API ----------------------------------------------------------------------

@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    def override():
        d = Session()
        try:
            yield d
        finally:
            d.close()
    main_mod.app.dependency_overrides[main_mod.get_db] = override
    c = TestClient(main_mod.app)
    key = c.post("/organisations", params={"name": "A"}).json()["api_key"]
    seed = Session()
    org = seed.query(Organisation).filter(Organisation.name == "A").one()
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.5)
    seed.add(f); seed.commit(); seed.refresh(f)
    seed.add(ActivityRecord(organisation_id=org.id, date="2025-01-01", category="electricity",
                            subcategory="", description="", quantity=1000, unit="kWh",
                            geo="GB", factor_id=f.id))
    seed.commit()
    compute_co2e(seed, org.id)
    seed.close()
    yield c, {"X-API-Key": key}
    main_mod.app.dependency_overrides.clear()


def test_endpoint_carries_guidance_and_validates(client):
    c, hdr = client
    r = c.get("/reports/ecovadis", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["guidance"]["key"] == "ecovadis"
    assert body["assessment_ready"] is True
    # a ratings scheme, not a standard we can self-certify
    assert any("only EcoVadis issues it" in n for n in body["not_assessed"])
    assert c.get("/reports/ecovadis", params={"intensity_denominator": 0},
                 headers=hdr).status_code == 400


def test_ecovadis_listed_in_frameworks(client):
    c, _ = client
    entry = c.get("/frameworks/ecovadis").json()
    assert entry["platform_support"] == "partial"
    assert entry["endpoint"] == "/reports/ecovadis"
    assert any("RATINGS scheme" in p for p in entry["key_points"])
