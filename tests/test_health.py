"""/healthz — liveness + DB readiness for deploy targets."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod


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
    yield TestClient(main_mod.app)
    main_mod.app.dependency_overrides.clear()


def test_healthz_ok_when_db_reachable(client):
    r = client.get("/healthz")               # unauthenticated by design
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["version"]                    # reports the app version


def test_healthz_needs_no_api_key(client):
    # No X-API-Key header — health must be reachable by a load balancer.
    assert client.get("/healthz").status_code == 200
