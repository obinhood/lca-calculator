"""Per-key fixed-window rate limiting.

Unit tests pin the window arithmetic with an injected clock; the integration test drives the
middleware through the real app and — critically — DISABLES the limiter again in teardown so
the rest of the suite (which fires thousands of requests) is never throttled.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app import ratelimit
from app.ratelimit import FixedWindowLimiter, configure, get_limiter


def test_within_limit_allows():
    lim = FixedWindowLimiter(2, 60)
    assert lim.check("k", 0.0) == (True, 0)
    assert lim.check("k", 1.0) == (True, 0)


def test_over_limit_blocks_with_retry_after():
    lim = FixedWindowLimiter(2, 60)
    lim.check("k", 0.0); lim.check("k", 1.0)
    allowed, retry = lim.check("k", 2.0)          # 3rd in the window
    assert allowed is False and retry >= 1


def test_window_reset_allows_again():
    lim = FixedWindowLimiter(2, 60)
    lim.check("k", 0.0); lim.check("k", 0.0)
    assert lim.check("k", 0.0)[0] is False
    assert lim.check("k", 61.0)[0] is True        # new window


def test_keys_are_independent():
    lim = FixedWindowLimiter(1, 60)
    assert lim.check("a", 0.0)[0] is True
    assert lim.check("a", 0.0)[0] is False
    assert lim.check("b", 0.0)[0] is True          # different tenant, own bucket


def test_configure_disables_when_non_positive():
    configure(5); assert get_limiter() is not None
    configure(0); assert get_limiter() is None


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
    key = c.post("/organisations", params={"name": "RLCo"}).json()["api_key"]
    yield c, {"X-API-Key": key}
    main_mod.app.dependency_overrides.clear()
    configure(0)   # ALWAYS disable again so no other test is throttled


def test_middleware_429s_over_limit_then_recovers_per_key(client):
    c, hdr = client
    configure(2, 60)              # 2 requests / window for this test only
    try:
        assert c.get("/runs", headers=hdr).status_code == 200
        assert c.get("/runs", headers=hdr).status_code == 200
        blocked = c.get("/runs", headers=hdr)
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        # a DIFFERENT key has its own bucket and is not affected
        other = c.post("/organisations", params={"name": "RLCo2"}).json()["api_key"]
        assert c.get("/runs", headers={"X-API-Key": other}).status_code == 200
    finally:
        configure(0)

    # with the limiter disabled, requests flow again
    assert c.get("/runs", headers=hdr).status_code == 200
    assert ratelimit.get_limiter() is None
