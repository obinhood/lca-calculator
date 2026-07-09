"""HTTP-level tests for auth, tenancy isolation, and the mapping review gate."""
import io
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401  (register tables)
from app.models import EmissionFactor, ActivityRecord
from app import main as main_mod
from app.services.calc import compute_co2e


@pytest.fixture
def env():
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
    client = TestClient(main_mod.app)

    # Register two orgs through the real endpoint (keys returned once).
    key_a = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    key_b = client.post("/organisations", params={"name": "B"}).json()["api_key"]
    hdr_a, hdr_b = {"X-API-Key": key_a}, {"X-API-Key": key_b}

    # Seed a factor + one of B's activities directly; compute B's run.
    seed = TestingSession()
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.17)
    seed.add(f); seed.commit(); seed.refresh(f)
    org_b_id = client.get("/runs", headers=hdr_b).status_code  # warm-up only
    from app.models import Organisation
    org_b = seed.query(Organisation).filter(Organisation.name == "B").one()
    seed.add(ActivityRecord(organisation_id=org_b.id, date="2025-01-01",
                            category="electricity", subcategory="", description="",
                            quantity=9999, unit="kWh", geo="GB", factor_id=f.id))
    seed.commit()
    run_b = compute_co2e(seed, org_b.id)
    run_b_id = run_b.id
    seed.close()

    yield client, hdr_a, hdr_b, run_b_id
    main_mod.app.dependency_overrides.clear()


def test_missing_or_bad_key_is_401(env):
    client, _, _, _ = env
    assert client.get("/results/summary").status_code == 422        # header required
    assert client.get("/results/summary",
                      headers={"X-API-Key": "wrong"}).status_code == 401


def test_org_names_are_not_credentials(env):
    """No endpoint accepts org_name as an identity claim anymore."""
    client, hdr_a, _, _ = env
    r = client.get("/results/summary", params={"org_name": "B"}, headers=hdr_a)
    assert r.status_code == 200
    body = r.json()
    assert body["run"] is None            # A sees A's (empty) data, not B's


def test_cross_tenant_run_id_is_blocked_over_http(env):
    client, hdr_a, _, run_b_id = env
    r = client.get("/results/summary", params={"run_id": run_b_id}, headers=hdr_a)
    assert r.status_code == 200
    assert r.json()["run"] is None        # B's run is invisible to A


def test_owner_can_read_own_run(env):
    client, _, hdr_b, run_b_id = env
    r = client.get("/results/summary", params={"run_id": run_b_id}, headers=hdr_b)
    assert r.status_code == 200
    body = r.json()
    assert body["run"] is not None
    assert body["total_co2e"] == pytest.approx(9999 * 0.17)


def test_duplicate_org_registration_conflicts(env):
    client, _, _, _ = env
    assert client.post("/organisations", params={"name": "A"}).status_code == 409


def test_non_finite_instrument_rate_rejected(env):
    """inf/nan rates must 400/422 BEFORE any DB write (they poison totals)."""
    client, _, hdr_b, _ = env
    for bad in ("inf", "nan", "-inf"):
        r = client.post("/market_instruments",
                        params={"instrument_type": "residual_mix",
                                "kg_co2e_per_kwh": bad}, headers=hdr_b)
        assert r.status_code in (400, 422), bad


def test_contractual_instrument_requires_dates(env):
    client, _, hdr_b, _ = env
    r = client.post("/market_instruments",
                    params={"instrument_type": "rec", "kg_co2e_per_kwh": 0.0},
                    headers=hdr_b)
    assert r.status_code == 400
    assert "start_date" in r.json()["detail"]
    r2 = client.post("/market_instruments",
                     params={"instrument_type": "rec", "kg_co2e_per_kwh": 0.0,
                             "coverage_kwh": 100,
                             "start_date": "2025-01-01", "end_date": "2025-12-31"},
                     headers=hdr_b)
    assert r2.status_code == 200


def test_upload_maps_exact_and_queues_coarse(env):
    """Exact matches bind automatically; coarse matches go to the review queue."""
    client, hdr_a, _, _ = env
    csv_data = (
        "date,category,subcategory,description,quantity,unit,geo\n"
        "2025-01-15,electricity,,HQ power,1000,kWh,GB\n"       # exact -> auto
        "2025-02-01,electricity,,Berlin office,500,kWh,DE\n"   # geo fallback -> review
    )
    r = client.post("/activities/upload_csv", headers=hdr_a,
                    files={"file": ("acts.csv", io.BytesIO(csv_data.encode()), "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["mapping"]["auto"] == 1
    assert body["mapping"]["needs_review"] == 1

    queue = client.get("/mappings/review", headers=hdr_a).json()
    assert len(queue) == 1
    item = queue[0]
    assert item["geo"] == "DE"
    # No DE-geography factor exists, so the coarsest fallback applies.
    assert item["mapping_basis"] == "category_only"
    assert item["mapping_confidence"] == pytest.approx(0.6)
    assert item["suggested_factor"]["id"] is not None

    # Approve the suggestion; queue empties and the factor binds.
    ra = client.post(f"/mappings/{item['activity_id']}/approve", headers=hdr_a)
    assert ra.status_code == 200
    assert ra.json()["mapping_status"] == "approved"
    assert client.get("/mappings/review", headers=hdr_a).json() == []


def test_review_queue_is_org_scoped(env):
    client, hdr_a, hdr_b, _ = env
    csv_data = ("date,category,subcategory,description,quantity,unit,geo\n"
                "2025-02-01,electricity,,office,500,kWh,DE\n")
    client.post("/activities/upload_csv", headers=hdr_a,
                files={"file": ("a.csv", io.BytesIO(csv_data.encode()), "text/csv")})
    queue_a = client.get("/mappings/review", headers=hdr_a).json()
    queue_b = client.get("/mappings/review", headers=hdr_b).json()
    assert len(queue_a) == 1 and queue_b == []
    # B cannot approve A's activity.
    rb = client.post(f"/mappings/{queue_a[0]['activity_id']}/approve", headers=hdr_b)
    assert rb.status_code == 404


def test_oversized_upload_rejected(env):
    client, hdr_a, _, _ = env
    big = b"x" * (main_mod.MAX_UPLOAD_BYTES + 1)
    r = client.post("/activities/upload_csv", headers=hdr_a,
                    files={"file": ("big.csv", io.BytesIO(big), "text/csv")})
    assert r.status_code == 413
