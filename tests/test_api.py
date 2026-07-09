"""HTTP-level tests for the multi-tenant read paths (the surface where the
Phase 2a cross-tenant leaks lived)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401  (register tables)
from app.models import Organisation, EmissionFactor, ActivityRecord
from app import main as main_mod
from app.services.calc import compute_co2e


@pytest.fixture
def client_env():
    # One shared in-memory DB across requests.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db

    seed = TestingSession()
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh", gwp_set="AR6", value=0.17)
    seed.add(f)
    org_a = Organisation(name="A")
    org_b = Organisation(name="B")
    seed.add_all([org_a, org_b]); seed.commit(); seed.refresh(org_a); seed.refresh(org_b); seed.refresh(f)
    seed.add(ActivityRecord(organisation_id=org_b.id, date="2025-01-01", category="electricity",
                            subcategory="", description="", quantity=9999, unit="kWh", geo="GB",
                            factor_id=f.id))
    seed.commit()
    run_b = compute_co2e(seed, org_b.id)
    run_b_id = run_b.id
    seed.close()

    client = TestClient(main_mod.app)
    yield client, run_b_id
    main_mod.app.dependency_overrides.clear()


def test_unknown_org_returns_404(client_env):
    client, _ = client_env
    r = client.get("/results/summary", params={"org_name": "does-not-exist"})
    assert r.status_code == 404  # must NOT fall through to the global latest run


def test_cross_tenant_run_id_is_blocked_over_http(client_env):
    client, run_b_id = client_env
    # OrgA (exists, no runs) requests OrgB's run id.
    r = client.get("/results/summary", params={"org_name": "A", "run_id": run_b_id})
    assert r.status_code == 200
    body = r.json()
    assert body["run"] is None        # OrgB's data is NOT returned
    assert body["total_co2e"] == 0.0


def test_owner_can_read_own_run(client_env):
    client, run_b_id = client_env
    r = client.get("/results/summary", params={"org_name": "B", "run_id": run_b_id})
    assert r.status_code == 200
    body = r.json()
    assert body["run"] is not None
    assert body["total_co2e"] == pytest.approx(9999 * 0.17)


def test_non_finite_instrument_rate_rejected(client_env):
    """C3: inf/nan rates must 400 BEFORE any DB write (they poison totals)."""
    client, _ = client_env
    for bad in ("inf", "nan", "-inf"):
        r = client.post("/market_instruments",
                        params={"org_name": "B", "instrument_type": "residual_mix",
                                "kg_co2e_per_kwh": bad})
        assert r.status_code in (400, 422), bad


def test_contractual_instrument_requires_dates(client_env):
    client, _ = client_env
    r = client.post("/market_instruments",
                    params={"org_name": "B", "instrument_type": "rec",
                            "kg_co2e_per_kwh": 0.0})
    assert r.status_code == 400
    assert "start_date" in r.json()["detail"]
    # with valid dates it succeeds
    r2 = client.post("/market_instruments",
                     params={"org_name": "B", "instrument_type": "rec",
                             "kg_co2e_per_kwh": 0.0, "coverage_kwh": 100,
                             "start_date": "2025-01-01", "end_date": "2025-12-31"})
    assert r2.status_code == 200
