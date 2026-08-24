"""POST /demo/seed — one-click populate: loads the sample activities and runs a calculation."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app.models import EmissionFactor, ActivityRecord


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
    # seed one factor so at least one demo activity maps and the run has a non-zero total
    s = Session()
    s.add(EmissionFactor(source="T", version="1", geography="GB", year=2024,
                         category="electricity", subcategory="", unit="kWh",
                         gwp_set="AR6", value=0.17))
    s.commit(); s.close()
    key = c.post("/organisations", params={"name": "DemoCo"}).json()["api_key"]
    yield c, {"X-API-Key": key}, Session
    main_mod.app.dependency_overrides.clear()


def test_seed_populates_activities_and_a_run(client):
    c, hdr, _ = client
    r = c.post("/demo/seed", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    from app.main import _DEMO_ACTIVITIES
    assert body["seeded_activities"] == len(_DEMO_ACTIVITIES)
    assert body["already_had_activities"] is False
    assert body["run_id"] is not None
    # only the electricity factor is seeded in this fixture, so the total is the electricity
    # rows x 0.17 — proves the sample rows actually map and compute.
    elec = sum(q for (_d, cat, _s, _desc, q, _u, _g) in _DEMO_ACTIVITIES if cat == "electricity")
    assert body["total_co2e_kg"] == pytest.approx(elec * 0.17)


def test_seed_is_idempotent(client):
    c, hdr, Session = client
    c.post("/demo/seed", headers=hdr)
    r2 = c.post("/demo/seed", headers=hdr)               # second click
    assert r2.status_code == 200
    body = r2.json()
    assert body["seeded_activities"] == 0                # did not re-insert
    assert body["already_had_activities"] is True
    assert body["run_id"] is not None                    # but still (re)computed a run
    from app.main import _DEMO_ACTIVITIES
    db = Session()
    org = db.query(ActivityRecord).count()
    db.close()
    assert org == len(_DEMO_ACTIVITIES)                   # exactly one demo set, not doubled


def test_seed_requires_api_key(client):
    c, _, _ = client
    # org-scoped: rejected without a valid key (422 = header missing, 401 = key invalid)
    assert c.post("/demo/seed").status_code in (401, 422)
    assert c.post("/demo/seed", headers={"X-API-Key": "bogus"}).status_code == 401


# --- the demo shows the platform, not a subset -------------------------------------------

def test_the_demo_seeds_the_subsystems_it_used_to_omit(db):
    """POST /demo/seed populated exactly two tables, so an evaluator landed on a
    spend-and-energy calculator and saw none of the hourly matching, series screening or
    Scope 3 screen the product is largely made of."""
    from app.models import (HourlyLoad, GranularCertificate, HourlyGridIntensity,
                            ReportingPeriod, Scope3CategoryDeclaration, ActivityRecord)
    from app.services.demo_seed import seed_all
    from app.services.calc import compute_co2e
    from app.models import Organisation

    org = Organisation(name="DemoSeedOrg"); db.add(org); db.commit(); db.refresh(org)
    db.add(ActivityRecord(organisation_id=org.id, date="2025-06-01",
                          category="electricity", subcategory="", description="",
                          quantity=1000.0, unit="kWh", geo="GB"))
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR6")
    out = seed_all(db, org.id, run=run)

    assert out["reporting_period"]["label"] == "FY2025"
    assert out["prior_period"]["label"] == "FY2024"
    assert out["hourly_scope2"]["seeded"] is True
    assert db.query(HourlyLoad).filter(HourlyLoad.organisation_id == org.id).count() == 24
    assert db.query(GranularCertificate).filter(
        GranularCertificate.organisation_id == org.id).count() > 0
    assert db.query(HourlyGridIntensity).count() == 24
    assert db.query(Scope3CategoryDeclaration).filter(
        Scope3CategoryDeclaration.organisation_id == org.id).count() == 15
    assert out["series_screening"]["activities_enrolled"] >= 1


def test_the_demo_seed_is_idempotent(db):
    from app.models import Organisation, HourlyLoad
    from app.services.demo_seed import seed_all
    org = Organisation(name="DemoIdemOrg"); db.add(org); db.commit(); db.refresh(org)
    seed_all(db, org.id)
    seed_all(db, org.id)
    seed_all(db, org.id)
    assert db.query(HourlyLoad).filter(HourlyLoad.organisation_id == org.id).count() == 24


def test_the_demo_never_declares_a_category_included_without_lines(db):
    """The first version guessed which categories carried lines and declared category 5
    included when it had none — the demo shipping the exact defect the product catches."""
    from app.models import Organisation, ActivityRecord, Scope3CategoryDeclaration
    from app.services.demo_seed import seed_all, scope3_categories_in_run
    from app.services.calc import compute_co2e

    org = Organisation(name="DemoScreenOrg"); db.add(org); db.commit(); db.refresh(org)
    db.add(ActivityRecord(organisation_id=org.id, date="2025-06-01",
                          category="electricity", subcategory="", description="",
                          quantity=1000.0, unit="kWh", geo="GB"))
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR6")
    in_run = scope3_categories_in_run(db, run)
    out = seed_all(db, org.id, run=run)

    included = {d.category for d in db.query(Scope3CategoryDeclaration).filter(
        Scope3CategoryDeclaration.organisation_id == org.id,
        Scope3CategoryDeclaration.status == "included").all()}
    assert included <= in_run, (
        f"declared included without lines: {sorted(included - in_run)}")
    assert out["scope3_screen"]["categories_declared"] == 15


def test_the_bill_validator_is_reachable():
    """463 lines of validation logic with no endpoint, no import, no caller."""
    import app.main as main_mod
    paths = {getattr(r, "path", "") for r in main_mod.app.routes}
    assert "/bills/validate" in paths
