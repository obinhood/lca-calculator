"""TCFD four-pillar cross-reference map.

TCFD is delivered through ISSB S2; this renderer maps the platform's quantitative output onto
the 11 recommended disclosures and is HONEST that only Metrics & Targets (b) is machine-backed.
These tests pin: exactly one produced disclosure, numbers sourced verbatim from ISSB S2 (one
source of truth), the precisely-named quantitative readiness signal (never a whole-report
'ready'), graceful handling of a non-ready / no-run org, and the loud report_scope caveat.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app.models import Organisation, EmissionFactor, ActivityRecord
from app.services.calc import compute_co2e
from app.reports.tcfd import tcfd_report
from app.reports.issb_s2 import issb_s2_report


def _org(db, name="TcfdCo"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, **kw):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024, category=category,
                       subcategory="", unit=unit, gwp_set="AR6", value=value, **kw)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category, qty, unit):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=qty, unit=unit, geo="GB",
                       factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _ready(db):
    """A disclosure-ready run (electricity + gas + screened waste)."""
    from tests.scope3_util import ready_run
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id, "electricity", 1200, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 800, "kWh")
    waste = _activity(db, org.id, _factor(db, "waste", "kg", 0.480).id, "waste", 250, "kg")
    waste.ghgp_category = 5
    db.commit()
    run, _period = ready_run(db, org.id)
    return org, run


def _all_disclosures(r):
    return [d for p in r["pillars"] for d in p["recommended_disclosures"]]


def test_exactly_one_produced_disclosure_the_rest_narrative(db):
    org, run = _ready(db)
    r = tcfd_report(db, org.id, run_id=run.id)
    discs = _all_disclosures(r)
    assert len(discs) == 11                                    # the full TCFD framework shown
    produced = [d for d in discs if d["produced"]]
    assert len(produced) == 1 and produced[0]["ref"] == "Metrics & Targets (b)"
    # every non-produced disclosure is explicitly a preparer narrative, never blank
    for d in discs:
        if not d["produced"]:
            assert "narrative" in d["source"]


def test_metrics_b_sourced_verbatim_from_issb_s2(db):
    """One source of truth: the GHG block must be byte-identical to the ISSB S2 renderer's."""
    org, run = _ready(db)
    r = tcfd_report(db, org.id, run_id=run.id)
    s2 = issb_s2_report(db, org.id, run_id=run.id)
    mt_b = next(d for d in _all_disclosures(r) if d["ref"] == "Metrics & Targets (b)")
    assert mt_b["ghg_emissions_tco2e"] == s2["ghg_emissions_tco2e"]
    assert mt_b["quantitative_core_ready"] is True
    assert r["metrics_and_targets_b_quantitative_ready"] is True
    assert r["metrics_and_targets_b_quantitative_blockers"] == []


def test_report_scope_disclaims_a_complete_tcfd_report(db):
    org, run = _ready(db)
    r = tcfd_report(db, org.id, run_id=run.id)
    assert "NOT a complete TCFD" in r["report_scope"]
    # never publishes a bare whole-report readiness boolean that could be misread
    assert "disclosure_ready" not in r
    assert len(r["narrative_disclosures_not_produced"]) >= 6


def test_non_ready_run_surfaces_blockers_not_a_ready_flag(db):
    """An AR5 run is not S2-ready; the TCFD map must carry that through, not paper over it."""
    org = _org(db)
    f = _factor(db, "electricity", "kWh", 0.17)
    _activity(db, org.id, f.id, "electricity", 100, "kWh")
    run5 = compute_co2e(db, org.id, gwp_set="AR5")
    r = tcfd_report(db, org.id, run_id=run5.id)
    assert r["metrics_and_targets_b_quantitative_ready"] is False
    assert any("AR6" in b for b in r["metrics_and_targets_b_quantitative_blockers"])
    mt_b = next(d for d in _all_disclosures(r) if d["ref"] == "Metrics & Targets (b)")
    assert mt_b["quantitative_core_ready"] is False
    assert mt_b["ghg_emissions_tco2e"] is not None             # numbers still shown, with blockers


def test_no_run_is_graceful(db):
    org = _org(db)
    r = tcfd_report(db, org.id)
    assert r["metrics_and_targets_b_quantitative_ready"] is False
    assert any("no calculation run" in b for b in r["metrics_and_targets_b_quantitative_blockers"])
    mt_b = next(d for d in _all_disclosures(r) if d["ref"] == "Metrics & Targets (b)")
    assert mt_b["ghg_emissions_tco2e"] is None                 # nothing to show, no crash


def test_jurisdiction_reference_recorded(db):
    org, run = _ready(db)
    r = tcfd_report(db, org.id, run_id=run.id, jurisdiction_reference="California SB 261")
    assert r["jurisdiction_reference"] == "California SB 261"
    r2 = tcfd_report(db, org.id, run_id=run.id, jurisdiction_reference="   ")
    assert r2["jurisdiction_reference"] is None


def test_template_not_mutated_across_calls(db):
    """The module-level _PILLARS template must not accumulate per-request data."""
    org, run = _ready(db)
    tcfd_report(db, org.id, run_id=run.id)
    from app.reports.tcfd import _PILLARS
    mt_b = next(d for p in _PILLARS for d in p["disclosures"]
                if d["ref"] == "Metrics & Targets (b)")
    assert "ghg_emissions_tco2e" not in mt_b                    # template stayed clean


# --- endpoint ---

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
    key = c.post("/organisations", params={"name": "TcfdCo"}).json()["api_key"]
    yield c, {"X-API-Key": key}, Session
    main_mod.app.dependency_overrides.clear()


def test_endpoint_and_guidance(client):
    c, hdr, Session = client
    db = Session()
    org = db.query(Organisation).first()
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id, "electricity", 1200, "kWh")
    run = compute_co2e(db, org.id, gwp_set="AR6")
    run_id = run.id
    db.close()
    resp = c.get("/reports/tcfd", headers=hdr, params={"run_id": run_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["framework"].startswith("TCFD")
    assert len(_all_disclosures(body)) == 11
    assert body.get("guidance", {}).get("key") == "tcfd"
