"""ISO 14064-2 project GHG quantification renderer.

A project reduction is (baseline scenario - project scenario - leakage) across two immutable
runs. These tests pin: the exact delta, the fail-closed gates that make the assertion
credible (justified baseline, quantified leakage, GWP/residual comparability, no partial
scenario), the honest handling of a NET INCREASE (not dressed up as a reduction), tenant
isolation, and the ever-present double-counting firewall against the corporate inventory.
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
from app.reports.iso_14064_2 import iso_14064_2_report

JUSTIFY = ("Baseline = continuation of the pre-retrofit gas boiler at metered pre-project "
           "load; no policy or price driver would have changed it absent the project.")


def _org(db, name="ProjOrg"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, value, gwp_set="AR6"):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set=gwp_set, value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, qty):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category="electricity",
                       subcategory="", description="", quantity=qty, unit="kWh",
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _two_scenarios(db, org, base_kwh=1000.0, proj_kwh=600.0):
    """Build a baseline run then a project run for the same org, over the SAME activity
    whose quantity is lowered to model the intervention. Both non-partial, both AR6."""
    f = _factor(db, 0.17)
    a = _activity(db, org.id, f.id, base_kwh)
    base = compute_co2e(db, org.id, gwp_set="AR6")          # 1000 * 0.17 = 170 kg
    a.quantity = proj_kwh
    db.commit()
    proj = compute_co2e(db, org.id, gwp_set="AR6")          # 600 * 0.17 = 102 kg
    return base, proj


def test_happy_path_net_reduction(db):
    org = _org(db)
    base, proj = _two_scenarios(db, org)
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.01, baseline_justification=JUSTIFY,
                           project_name="Boiler retrofit")
    assert r["disclosure_ready"] is True, r["blockers"]
    q = r["quantification_tco2e"]
    assert q["baseline_scenario_location_based"] == pytest.approx(0.170)
    assert q["project_scenario_location_based"] == pytest.approx(0.102)
    assert q["gross_reduction_location_based"] == pytest.approx(0.068)
    assert q["leakage"] == pytest.approx(0.01)
    assert q["net_reduction_location_based"] == pytest.approx(0.058)
    # no contractual instruments -> market leg tracks location; both are reductions
    assert q["is_reduction_location_based"] is True
    assert q["is_reduction_market_based"] is True
    assert r["assertion_status"] == "unverified_quantification"
    # the firewall against corporate-inventory double counting is always present
    assert "corporate GHG inventory" in r["double_counting_warning"]
    assert r["project"]["baseline_justification"] == JUSTIFY


def test_missing_baseline_justification_blocks(db):
    org = _org(db)
    base, proj = _two_scenarios(db, org)
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0, baseline_justification="   ")
    assert r["disclosure_ready"] is False
    assert any("baseline_justification" in b for b in r["blockers"])


def test_missing_leakage_blocks(db):
    org = _org(db)
    base, proj = _two_scenarios(db, org)
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=None, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("leakage" in b for b in r["blockers"])
    # net is None when leakage is unquantified — never silently treated as zero
    assert r["quantification_tco2e"]["net_reduction_location_based"] is None


def test_negative_leakage_blocks_and_never_inflates(db):
    """A negative leakage is not just blocked — the net figure must NOT be computed with it
    (subtracting a negative would inflate the surfaced reduction ~8x). It nulls, like NaN/inf."""
    org = _org(db)
    base, proj = _two_scenarios(db, org)
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=-0.5, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("negative" in b for b in r["blockers"])
    q = r["quantification_tco2e"]
    assert q["net_reduction_location_based"] is None      # NOT 0.068 - (-0.5) = 0.568
    assert q["net_reduction_market_based"] is None
    assert q["is_reduction_location_based"] is False
    assert q["is_reduction_market_based"] is False


def test_infinite_leakage_blocks(db):
    org = _org(db)
    base, proj = _two_scenarios(db, org)
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=float("inf"), baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("finite" in b for b in r["blockers"])


def test_same_run_blocks(db):
    org = _org(db)
    base, _ = _two_scenarios(db, org)
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=base.id,
                           leakage_tco2e=0.0, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("own baseline" in b or "same run" in b for b in r["blockers"])


def test_gwp_vintage_mismatch_blocks(db):
    """A reduction spanning an AR5 baseline and an AR6 project mixes a GWP-vintage artefact
    into the delta — blocked, same guard as GRI 305-5."""
    org = _org(db)
    f5 = _factor(db, 0.17, gwp_set="AR5")
    f6 = _factor(db, 0.17, gwp_set="AR6")
    a = _activity(db, org.id, f5.id, 1000.0)
    base = compute_co2e(db, org.id, gwp_set="AR5")
    a.factor_id = f6.id
    db.commit()
    proj = compute_co2e(db, org.id, gwp_set="AR6")
    assert base.gwp_set == "AR5" and proj.gwp_set == "AR6"
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("GWP" in b and "comparable" in b for b in r["blockers"])


def test_net_increase_is_honest_not_a_reduction(db):
    """The project scenario emitting MORE than the baseline is a legitimate result, not a
    disclosure defect: is_reduction is False and the net is negative, but it is NOT blocked
    and is never presented as abatement."""
    org = _org(db)
    base, proj = _two_scenarios(db, org, base_kwh=600.0, proj_kwh=1000.0)  # project worse
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is True, r["blockers"]
    q = r["quantification_tco2e"]
    assert q["gross_reduction_location_based"] == pytest.approx(-0.068)
    assert q["net_reduction_location_based"] == pytest.approx(-0.068)
    assert q["is_reduction_location_based"] is False
    assert q["is_reduction_market_based"] is False


def test_tenant_isolation_other_org_run_not_found(db):
    org_a = _org(db, "A")
    org_b = _org(db, "B")
    base, proj = _two_scenarios(db, org_a)
    # org_b tries to use org_a's runs
    r = iso_14064_2_report(db, org_b.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("not found" in b for b in r["blockers"])


def test_partial_scenario_run_blocks(db):
    """A baseline run that silently dropped an unmapped activity understates its side of the
    delta — blocked."""
    org = _org(db)
    f = _factor(db, 0.17)
    _activity(db, org.id, f.id, 1000.0)
    _activity(db, org.id, None, 500.0)                     # unmapped -> run is partial
    base = compute_co2e(db, org.id, gwp_set="AR6")
    # a clean project run: map the second activity too
    for a in db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id):
        a.factor_id = f.id
    db.commit()
    proj = compute_co2e(db, org.id, gwp_set="AR6")
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("PARTIAL" in b and "baseline" in b for b in r["blockers"])


def test_empty_scenario_run_blocks(db):
    """A run computed over ZERO activities is complete-looking (partial = 0<0 = False) with a
    0 kg total. As a project scenario that 0 would read as a 100%-of-baseline reduction — the
    one incompleteness `partial` cannot catch. Must fail closed."""
    org = _org(db)
    empty = compute_co2e(db, org.id, gwp_set="AR6")         # zero activities -> 0 kg
    _activity(db, org.id, _factor(db, 0.17).id, 1000.0)
    base = compute_co2e(db, org.id, gwp_set="AR6")          # 170 kg baseline
    assert (empty.total_activities or 0) == 0 and empty.total_co2e == 0.0
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=empty.id,
                           leakage_tco2e=0.0, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("ZERO activities" in b and "project" in b for b in r["blockers"])


def test_reduction_status_is_per_basis_when_legs_diverge(db):
    """The location- and market-based nets can disagree in sign (a baseline holding
    contractual instruments the project drops is a location reduction but a market increase).
    Each basis must carry its OWN reduction flag so a market increase is never labelled
    abatement by a location-only flag."""
    org = _org(db)
    base, proj = _two_scenarios(db, org)                    # loc: 170 -> 102 (reduction)
    # Force the market leg the other way: project's market total rises above the baseline's.
    proj.total_co2e_market = 300.0
    db.commit()
    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           leakage_tco2e=0.0, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is True, r["blockers"]
    q = r["quantification_tco2e"]
    assert q["net_reduction_location_based"] == pytest.approx(0.068)      # a reduction
    assert q["net_reduction_market_based"] == pytest.approx(-0.130)       # an INCREASE
    assert q["is_reduction_location_based"] is True
    assert q["is_reduction_market_based"] is False                        # never mislabelled


def test_missing_run_ids_blocks(db):
    org = _org(db)
    r = iso_14064_2_report(db, org.id, baseline_run_id=None, project_run_id=None,
                           leakage_tco2e=0.0, baseline_justification=JUSTIFY)
    assert r["disclosure_ready"] is False
    assert any("required" in b for b in r["blockers"])


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
    key = c.post("/organisations", params={"name": "ProjCo"}).json()["api_key"]
    yield c, {"X-API-Key": key}, Session
    main_mod.app.dependency_overrides.clear()


def test_endpoint_happy_path_and_guidance(client):
    c, hdr, Session = client
    db = Session()
    org = db.query(Organisation).first()
    base, proj = _two_scenarios(db, org)
    base_id, proj_id = base.id, proj.id
    db.close()
    resp = c.get("/reports/iso_14064_2", headers=hdr, params={
        "baseline_run_id": base_id, "project_run_id": proj_id,
        "leakage_tco2e": 0.01, "baseline_justification": JUSTIFY,
        "project_name": "Retrofit"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["disclosure_ready"] is True, body["blockers"]
    assert body["quantification_tco2e"]["net_reduction_location_based"] == pytest.approx(0.058)
    assert "double_counting_warning" in body
    # guidance attached on the framework prefix
    assert body.get("guidance", {}).get("key") == "iso_14064_2"


def test_endpoint_rejects_non_finite_leakage(client):
    c, hdr, Session = client
    db = Session()
    org = db.query(Organisation).first()
    base, proj = _two_scenarios(db, org)
    base_id, proj_id = base.id, proj.id
    db.close()
    resp = c.get("/reports/iso_14064_2", headers=hdr, params={
        "baseline_run_id": base_id, "project_run_id": proj_id,
        "leakage_tco2e": float("inf"), "baseline_justification": JUSTIFY})
    # rejected as a client error, whether by the endpoint's finite check (400) or by
    # request validation (422) — either way a non-finite leakage never reaches the delta
    assert resp.status_code in (400, 422)
