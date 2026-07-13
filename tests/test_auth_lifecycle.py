"""API-key rotation, revocation, and gated registration."""
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401
from app import main as main_mod


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()
    main_mod.app.dependency_overrides[main_mod.get_db] = override
    yield TestClient(main_mod.app)
    main_mod.app.dependency_overrides.clear()


def test_rotate_key_invalidates_old(client):
    key = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    hdr = {"X-API-Key": key}
    assert client.get("/runs", headers=hdr).status_code == 200
    new_key = client.post("/organisations/rotate_key", headers=hdr).json()["api_key"]
    assert new_key != key
    # old key no longer works; new key does
    assert client.get("/runs", headers={"X-API-Key": key}).status_code == 401
    assert client.get("/runs", headers={"X-API-Key": new_key}).status_code == 200


def test_revoke_disables_key(client):
    key = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    hdr = {"X-API-Key": key}
    # wrong confirmation name rejected
    assert client.post("/organisations/revoke_key", params={"confirm_org_name": "B"},
                       headers=hdr).status_code == 400
    assert client.post("/organisations/revoke_key", params={"confirm_org_name": "A"},
                       headers=hdr).status_code == 200
    # key now rejected everywhere
    assert client.get("/runs", headers=hdr).status_code == 401
    # an admin can re-issue via a restored key path? here rotate is blocked (revoked)
    assert client.post("/organisations/rotate_key", headers=hdr).status_code == 401


def test_registration_gate(client, monkeypatch):
    # no token configured -> open registration
    monkeypatch.delenv("REGISTRATION_TOKEN", raising=False)
    assert client.post("/organisations", params={"name": "Open"}).status_code == 200
    # token configured -> registration requires it
    monkeypatch.setenv("REGISTRATION_TOKEN", "s3cret")
    assert client.post("/organisations", params={"name": "NoTok"}).status_code == 401
    assert client.post("/organisations", params={"name": "BadTok"},
                       headers={"X-Registration-Token": "wrong"}).status_code == 401
    ok = client.post("/organisations", params={"name": "GoodTok"},
                     headers={"X-Registration-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()["api_key"]
