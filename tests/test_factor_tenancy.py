"""A factor belongs to the platform or to one tenant, and nobody else may read it.

`GET /factors` was the only route in the application that returned rows from a
customer-populated table with no credential at all, and `emission_factors` had no
tenant column — so a supplier's PACT product footprint, materialised into that table
by services/pact_factor.materialise, was readable by anonymous callers, and an
operator-licensed catalogue was republished to the internet.

The rule is models.factor_visible_to(): a NULL organisation_id is platform reference
data every tenant may read; a set one is that tenant's alone. These tests pin the READ
PATHS, not just the rule, because the failure mode is a new path that forgets to apply
it — that leak is silent, and nothing else in the suite would notice.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)
from app import main as main_mod
from app.models import (
    ActivityRecord, EmissionFactor, Organisation, ProductFootprint, factor_visible_to,
)
from app.services.pact_factor import materialise
from app.services.pact_store import import_footprint
from app.services.resolver import auto_map_activity, propose_mapping
from tests.test_pact_factor import _doc


@pytest.fixture
def env():
    """A real app and a session over ONE in-memory database."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override
    try:
        yield TestClient(main_mod.app), db
    finally:
        main_mod.app.dependency_overrides.clear()
        db.close()


def _org(db, name, key):
    o = Organisation(name=name, api_key_hash=main_mod._hash_key(key))
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, *, org_id=None, category="electricity", value=0.17, geo="GB",
            method="average_data"):
    f = EmissionFactor(organisation_id=org_id, source="T", version="1", geography=geo,
                       year=2024, category=category, subcategory="", unit="kWh",
                       gwp_set="AR6", value=value, method_type=method)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, category="electricity", geo="GB"):
    a = ActivityRecord(organisation_id=org_id, date="2025-06-01", category=category,
                       subcategory="", description="", quantity=100.0, unit="kWh", geo=geo)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- the endpoint ------------------------------------------------------------------------

def test_factors_requires_a_key(env):
    client, db = env
    _factor(db)
    # Deterministic now: a missing key is 401, exactly like a bogus one.
    assert client.get("/factors").status_code == 401


def test_factors_rejects_a_bogus_key(env):
    client, db = env
    _factor(db)
    assert client.get("/factors", headers={"X-API-Key": "not-a-key"}).status_code == 401


def test_factors_shows_platform_rows_but_not_another_tenants(env):
    client, db = env
    _org(db, "Alpha", "ka"); b = _org(db, "Beta", "kb")
    glob = _factor(db)
    mine = _factor(db, org_id=b.id, category="supplier_widget", value=42.0)

    r = client.get("/factors", headers={"X-API-Key": "ka"})
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["factors"]}
    assert glob.id in ids, "platform reference data must stay readable by every tenant"
    assert mine.id not in ids, "Beta's private factor leaked to Alpha"

    ids_b = {row["id"] for row in
             client.get("/factors", headers={"X-API-Key": "kb"}).json()["factors"]}
    assert {glob.id, mine.id} <= ids_b, "a tenant must still see their own rows"


# --- the rule itself ---------------------------------------------------------------------

def test_visibility_rule_is_null_plus_own(env):
    _, db = env
    a = _org(db, "Alpha", "ka"); b = _org(db, "Beta", "kb")
    glob = _factor(db)
    fa = _factor(db, org_id=a.id, category="a")
    fb = _factor(db, org_id=b.id, category="b")

    def seen(oid):
        return {f.id for f in db.query(EmissionFactor).filter(factor_visible_to(oid)).all()}

    assert seen(a.id) == {glob.id, fa.id}
    assert seen(b.id) == {glob.id, fb.id}
    # None is the unauthenticated / platform context: global rows only.
    assert seen(None) == {glob.id}


# --- the resolver: binding a factor is a read of its value -------------------------------

def test_resolver_will_not_auto_bind_another_tenants_factor(env):
    _, db = env
    a = _org(db, "Alpha", "ka"); b = _org(db, "Beta", "kb")
    # Beta's private row is an EXACT match for Alpha's activity and outranks any
    # average-data factor on the resolver's own ordering (supplier_specific first).
    secret = _factor(db, org_id=b.id, value=99.0, method="supplier_specific")
    act = _activity(db, a.id)

    status = auto_map_activity(db, act)
    assert act.factor_id != secret.id, \
        "Alpha's line bound Beta's supplier factor — the value leaks through the run"
    assert status == "unmapped" and act.factor_id is None

    # A platform factor still binds normally.
    glob = _factor(db, value=0.17)
    auto_map_activity(db, act)
    assert act.factor_id == glob.id


def test_propose_mapping_defaults_to_platform_data_only(env):
    _, db = env
    b = _org(db, "Beta", "kb")
    _factor(db, org_id=b.id, method="supplier_specific")
    # No org_id: a caller that cannot name a tenant must not be handed a tenant's row.
    assert propose_mapping(db, "electricity", "", None, "GB") is None
    assert propose_mapping(db, "electricity", "", None, "GB", org_id=b.id) is not None


# --- binding by id: the 404 must not distinguish "someone else's" from "no such row" -----

def test_override_cannot_name_another_tenants_factor(env):
    client, db = env
    a = _org(db, "Alpha", "ka"); b = _org(db, "Beta", "kb")
    secret = _factor(db, org_id=b.id, value=99.0)
    act = _activity(db, a.id)

    r = client.post(f"/mappings/{act.id}/override?factor_id={secret.id}",
                    headers={"X-API-Key": "ka"})
    missing = client.post(f"/mappings/{act.id}/override?factor_id=999999",
                          headers={"X-API-Key": "ka"})
    assert r.status_code == 404
    assert missing.status_code == 404
    assert r.json() == missing.json(), "the response is an existence oracle over the table"


# --- PACT materialisation stamps the tenant ----------------------------------------------

def _held(db, org):
    r = import_footprint(db, org.id, _doc())
    assert r["stored"] is True, r
    return db.get(ProductFootprint, r["id"])


def test_materialised_pact_factor_is_private_to_its_importer(env):
    _, db = env
    a = _org(db, "Alpha", "ka"); b = _org(db, "Beta", "kb")
    res = materialise(db, _held(db, b))
    assert res.get("materialised") is True, res

    f = db.get(EmissionFactor, res["factor_id"])
    assert f.organisation_id == b.id, "a supplier PCF under NDA became platform data"
    assert f.id not in {x.id for x in db.query(EmissionFactor)
                        .filter(factor_visible_to(a.id)).all()}


def test_two_tenants_may_hold_the_same_supplier_footprint(env):
    _, db = env
    a = _org(db, "Alpha", "ka"); b = _org(db, "Beta", "kb")
    doc = _doc()
    ra = import_footprint(db, a.id, doc)
    rb = import_footprint(db, b.id, doc)
    assert ra["stored"] and rb["stored"]

    res_a = materialise(db, db.get(ProductFootprint, ra["id"]))
    res_b = materialise(db, db.get(ProductFootprint, rb["id"]))
    assert res_a.get("materialised") is True, res_a
    # Idempotency is per tenant: the second buyer mints their own row rather than
    # being handed the first buyer's private one.
    assert res_b.get("materialised") is True, res_b
    assert res_b["factor_id"] != res_a["factor_id"]


def test_materialise_is_still_idempotent_within_one_tenant(env):
    _, db = env
    b = _org(db, "Beta", "kb")
    row = _held(db, b)
    first = materialise(db, row)
    second = materialise(db, row)
    assert first.get("materialised") is True, first
    assert second.get("idempotent") is True, second
    assert second["factor_id"] == first["factor_id"]
