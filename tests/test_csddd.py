"""CSDDD Article 22 climate transition-plan CARBON INPUTS readiness map.

CSDDD is a due-diligence process directive; the platform touches only the Art 22 carbon
inputs. These tests pin: readiness requires BOTH a sound GHG inventory (ISSB S2) and a sound,
1.5°C-aligned science-based target (SBTi); the loud out-of-scope framing; that a target which
merely exists but is not ambitious is NOT ready; graceful no-target handling; tenant isolation;
and that no bare whole-directive 'ready' flag is ever published.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app.models import Organisation, EmissionFactor, ActivityRecord, EmissionsTarget
from app.services.calc import compute_co2e
from app.reports.csddd import csddd_report


def _org(db, name="DueDilCo"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024, category=category,
                       subcategory="", unit=unit, gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category, qty, unit):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=qty, unit=unit, geo="GB",
                       factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _ready_org(db):
    """Org with a disclosure-ready run (electricity + gas + screened waste)."""
    from tests.scope3_util import ready_run
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id, "electricity", 1200, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 800, "kWh")
    waste = _activity(db, org.id, _factor(db, "waste", "kg", 0.480).id, "waste", 250, "kg")
    waste.ghgp_category = 5
    db.commit()
    run, _period = ready_run(db, org.id)
    return org, run


def _target(db, org_id, base_run_id, reduction_pct=0.42, base_year=2025, target_year=2035):
    t = EmissionsTarget(organisation_id=org_id, name="NT", target_type="near_term",
                        scope_coverage="1+2", base_run_id=base_run_id, base_year=base_year,
                        target_year=target_year, target_reduction_pct=reduction_pct,
                        ambition="1.5C")
    db.add(t); db.commit(); db.refresh(t)
    return t


def test_both_inputs_ready(db):
    org, run = _ready_org(db)
    t = _target(db, org.id, run.id)                        # 42% over 10y -> 1.5C-aligned
    r = csddd_report(db, org.id, run_id=run.id, target_id=t.id)
    inv = r["article_22_carbon_inputs"]["ghg_inventory"]
    tgt = r["article_22_carbon_inputs"]["science_based_targets"]
    assert inv["ready"] is True and inv["ghg_emissions_tco2e"] is not None
    assert tgt["present"] is True and tgt["ready"] is True and tgt["meets_1p5c_minimum"] is True
    assert r["climate_transition_plan_carbon_inputs_ready"] is True


def test_scope_is_loud_and_no_bare_ready_flag(db):
    org, run = _ready_org(db)
    r = csddd_report(db, org.id, run_id=run.id)
    assert "NOT the transition plan" in r["report_scope"]
    assert "due-diligence process" in r["report_scope"]
    assert "disclosure_ready" not in r                     # no misreadable whole-directive flag
    assert len(r["not_produced"]) >= 6


def test_no_target_means_inputs_not_ready(db):
    org, run = _ready_org(db)
    r = csddd_report(db, org.id, run_id=run.id)             # no target_id
    tgt = r["article_22_carbon_inputs"]["science_based_targets"]
    assert tgt["present"] is False and tgt["ready"] is False
    assert "NO emissions target" in tgt["note"]
    assert r["climate_transition_plan_carbon_inputs_ready"] is False


def test_unambitious_target_is_not_ready(db):
    """Art 22 requires 1.5°C alignment: a target that merely exists but misses the minimum
    ambition is NOT a ready Art 22 input, even with a sound inventory."""
    org, run = _ready_org(db)
    weak = _target(db, org.id, run.id, reduction_pct=0.05)  # 5% over 10y -> not 1.5C
    r = csddd_report(db, org.id, run_id=run.id, target_id=weak.id)
    tgt = r["article_22_carbon_inputs"]["science_based_targets"]
    assert tgt["present"] is True
    assert tgt["meets_1p5c_minimum"] is False
    assert tgt["ready"] is False
    assert r["climate_transition_plan_carbon_inputs_ready"] is False


def test_wb2c_target_is_not_1p5c_aligned(db):
    """A well-below-2°C target clears its OWN 2.5%/yr floor, but Art 22 requires 1.5°C (4.2%/yr).
    A WB2C target below the 1.5°C floor must NOT read as meeting the 1.5°C minimum or as ready."""
    org, run = _ready_org(db)
    # 30% over 10y = 3.0%/yr: clears WB2C (2.5%) but below the 1.5°C floor (4.2%)
    t = EmissionsTarget(organisation_id=org.id, name="WB2C", target_type="near_term",
                        scope_coverage="1+2", base_run_id=run.id, base_year=2025,
                        target_year=2035, target_reduction_pct=0.30, ambition="WB2C")
    db.add(t); db.commit(); db.refresh(t)
    r = csddd_report(db, org.id, run_id=run.id, target_id=t.id)
    tgt = r["article_22_carbon_inputs"]["science_based_targets"]
    assert tgt["present"] is True
    assert tgt["meets_1p5c_minimum"] is False              # 3.0%/yr < 4.2%/yr, despite meeting WB2C
    assert tgt["ready"] is False
    assert r["climate_transition_plan_carbon_inputs_ready"] is False


def test_trajectory_placement_blocker_does_not_drag_input_readiness(db):
    """A sound, 1.5°C-aligned target + a sound inventory stay READY even when a supplementary
    trajectory cannot be placed (e.g. current_year before the base year) — that is a pathway
    issue, not a carbon-input readiness issue."""
    org, run = _ready_org(db)
    t = _target(db, org.id, run.id)                        # sound 1.5°C target, base_year 2025
    r = csddd_report(db, org.id, run_id=run.id, target_id=t.id, current_year=2024)  # < base year
    tgt = r["article_22_carbon_inputs"]["science_based_targets"]
    assert tgt["ready"] is True                            # trajectory blocker does not drag readiness
    assert r["climate_transition_plan_carbon_inputs_ready"] is True
    assert any("before the base year" in b for b in tgt["blockers"])   # surfaced for transparency
    assert tgt["blockers_note"] is not None


def test_target_ready_without_current_year(db):
    """Passing a run for the inventory but no current_year must NOT spuriously block the target
    (SBTi errors on a current run with no year — csddd must not forward it in that case)."""
    org, run = _ready_org(db)
    t = _target(db, org.id, run.id)
    r = csddd_report(db, org.id, run_id=run.id, target_id=t.id)  # no current_year
    tgt = r["article_22_carbon_inputs"]["science_based_targets"]
    assert tgt["ready"] is True                            # not blocked by a missing year
    assert not any("current_year" in b for b in tgt.get("blockers", []))


def test_inventory_not_ready_blocks(db):
    """An AR5 run is not ISSB-S2-ready; the inventory input is not ready and neither are the
    carbon inputs as a whole."""
    org = _org(db)
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 1000, "kWh")
    run5 = compute_co2e(db, org.id, gwp_set="AR5")
    t = _target(db, org.id, run5.id)
    r = csddd_report(db, org.id, run_id=run5.id, target_id=t.id)
    inv = r["article_22_carbon_inputs"]["ghg_inventory"]
    assert inv["ready"] is False and len(inv["blockers"]) > 0
    assert r["climate_transition_plan_carbon_inputs_ready"] is False


def test_tenant_isolation_on_target(db):
    org_a, run_a = _ready_org(db)
    org_b = _org(db, "B")
    t = _target(db, org_a.id, run_a.id)
    # org_b references org_a's target
    r = csddd_report(db, org_b.id, target_id=t.id)
    tgt = r["article_22_carbon_inputs"]["science_based_targets"]
    assert tgt["present"] is False
    assert any("not found" in b for b in tgt.get("blockers", []))


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
    key = c.post("/organisations", params={"name": "DueDilCo"}).json()["api_key"]
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
    resp = c.get("/reports/csddd", headers=hdr, params={"run_id": run_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["framework"].startswith("EU CSDDD")
    assert "article_22_carbon_inputs" in body
    assert body.get("guidance", {}).get("key") == "csddd"
