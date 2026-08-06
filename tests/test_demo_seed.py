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
    assert body["seeded_activities"] == 7
    assert body["already_had_activities"] is False
    assert body["run_id"] is not None
    assert body["total_co2e_kg"] == pytest.approx(204.0)   # electricity 1200 * 0.17 (only mapped one)


def test_seed_is_idempotent(client):
    c, hdr, Session = client
    c.post("/demo/seed", headers=hdr)
    r2 = c.post("/demo/seed", headers=hdr)               # second click
    assert r2.status_code == 200
    body = r2.json()
    assert body["seeded_activities"] == 0                # did not re-insert
    assert body["already_had_activities"] is True
    assert body["run_id"] is not None                    # but still (re)computed a run
    db = Session()
    org = db.query(ActivityRecord).count()
    db.close()
    assert org == 7                                       # exactly one demo set, not doubled


def test_seed_requires_api_key(client):
    c, _, _ = client
    # org-scoped: rejected without a valid key (422 = header missing, 401 = key invalid)
    assert c.post("/demo/seed").status_code in (401, 422)
    assert c.post("/demo/seed", headers={"X-API-Key": "bogus"}).status_code == 401
