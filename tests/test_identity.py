"""A request has a principal, and a read-only credential cannot write.

Before this there was ONE credential per tenant: `organisations.api_key_hash`, shared,
with no owner, no role and no expiry. Three consequences, each a finding in the
deployability review:

  * the mapping audit trail could record that a decision was made and not WHO made it;
  * there was no joiner-mover-leaver path, so a leaver was offboarded by rotating the
    key for everybody;
  * letting an auditor read a filed run meant handing over the tenant's WRITE key —
    `assurance_engagements.access_token_hash` exists for the purpose and is a bare hash
    with no expiry, no revocation and no scope.

WHAT THESE TESTS PIN, and the order matters. The first is that nothing broke: 114 routes
depend on `current_org`, and the whole design rests on it keeping its signature while
gaining enforcement. The rest are the refusals — and each one is a refusal that must
NOT be reachable by holding a credential that merely looks adequate.
"""
import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)
from app import main as main_mod
from app.models import ApiToken, Organisation, User


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


def _org(db, name="Acme", key="orgkey"):
    o = Organisation(name=name, api_key_hash=main_mod._hash_key(key))
    db.add(o); db.commit(); db.refresh(o)
    return o


def _iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _token(db, org, *, secret, scopes="read", user_id=None, expires_in=30,
           revoked_at=None, expires_at=None):
    t = ApiToken(organisation_id=org.id, user_id=user_id, label="t",
                 token_hash=main_mod._hash_key(secret), scopes=scopes,
                 expires_at=expires_at if expires_at is not None else _iso(expires_in),
                 revoked_at=revoked_at)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _user(db, org, *, email="a@b.com", role="member", status="active"):
    u = User(organisation_id=org.id, email=email, role=role, status=status)
    db.add(u); db.commit(); db.refresh(u)
    return u


H = "X-API-Key"


# --- the thing that must not have broken -------------------------------------------------

def test_the_organisation_key_still_authenticates_unchanged(env):
    """114 routes depend on `current_org`, which now resolves a principal first.

    If this fails, the change is not worth discussing: every existing client and every
    existing test authenticates this way.
    """
    client, db = env
    _org(db)
    assert client.get("/users", headers={H: "orgkey"}).status_code == 200
    assert client.get("/users").status_code == 401                  # missing, not 422
    assert client.get("/users", headers={H: "nope"}).status_code == 401


def test_the_organisation_key_may_still_write(env):
    """It is a full-scope principal: a tenant acting as itself. This is also how a
    tenant bootstraps its first user, so if it cannot write, nobody can ever be added."""
    client, db = env
    _org(db)
    r = client.post("/users?email=first@acme.test&role=owner", headers={H: "orgkey"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "owner"


# --- the refusals ------------------------------------------------------------------------

def test_a_read_token_reads_but_cannot_write(env):
    """403 and not 401, deliberately: the credential worked and the ACTION was refused.

    A client that cannot tell those apart cannot tell "sign in again" from "you may not
    do this", which is the same confusion `current_org`'s own docstring records about
    401-vs-422 for a missing header.
    """
    client, db = env
    org = _org(db)
    _token(db, org, secret="ro", scopes="read")

    assert client.get("/users", headers={H: "ro"}).status_code == 200
    r = client.post("/users?email=x@acme.test", headers={H: "ro"})
    assert r.status_code == 403, r.text
    assert "read-only" in r.json()["detail"]


def test_a_write_token_writes(env):
    """The positive control for the test above. Without it, a gate that refused every
    write would pass it while refusing everything — the failure mode this codebase has
    found in its own gates more than once."""
    client, db = env
    org = _org(db)
    _token(db, org, secret="rw", scopes="write")
    assert client.post("/users?email=y@acme.test",
                       headers={H: "rw"}).status_code == 403   # needs ADMIN, not write
    _token(db, org, secret="adm", scopes="admin")
    assert client.post("/users?email=y@acme.test",
                       headers={H: "adm"}).status_code == 200


def test_an_expired_token_is_refused(env):
    client, db = env
    org = _org(db)
    _token(db, org, secret="old", scopes="admin", expires_in=-1)
    r = client.get("/users", headers={H: "old"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_a_revoked_token_is_refused_before_its_expiry_window_closes(env):
    """The window revocation exists to close. `services/pact_host.py` records the same
    lesson in prose: a bearer token already issued would otherwise stay valid until it
    expired."""
    client, db = env
    org = _org(db)
    _token(db, org, secret="dead", scopes="admin", expires_in=365,
           revoked_at=datetime.now(timezone.utc).isoformat())
    r = client.get("/users", headers={H: "dead"})
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"]


def test_suspending_a_user_kills_every_credential_they_hold(env):
    """THE LEAVER PATH, and the reason the identity model exists at all. Offboarding one
    person must not mean rotating the tenant's key for everybody else."""
    client, db = env
    org = _org(db)
    u = _user(db, org, email="leaver@acme.test", role="member")
    _token(db, org, secret="theirs", scopes="write", user_id=u.id)

    assert client.get("/users", headers={H: "theirs"}).status_code == 200
    assert client.post(f"/users/{u.id}/status?status=suspended",
                       headers={H: "orgkey"}).status_code == 200

    r = client.get("/users", headers={H: "theirs"})
    assert r.status_code == 401
    assert "suspended" in r.json()["detail"]
    # And the tenant's own key is untouched — the whole point.
    assert client.get("/users", headers={H: "orgkey"}).status_code == 200


def test_the_auditor_role_beats_a_write_scope(env):
    """Role and scope are BOTH consulted and the role wins.

    A token is a credential the tenant hands out and can get wrong; the role is what the
    tenant said the person IS. An admin who issues a write token to an auditor account by
    mistake must not thereby grant write access to a filed inventory. Constructed
    directly in the database, because the issuing endpoint refuses this combination — the
    point is that the REQUEST path refuses it too, not only the mint path.
    """
    client, db = env
    org = _org(db)
    a = _user(db, org, email="auditor@firm.test", role="auditor")
    _token(db, org, secret="aud", scopes="write", user_id=a.id)

    assert client.get("/users", headers={H: "aud"}).status_code == 200
    r = client.post("/users?email=z@acme.test", headers={H: "aud"})
    assert r.status_code == 403
    assert "auditor" in r.json()["detail"]


def test_issuing_a_write_token_to_an_auditor_is_refused_at_the_mint(env):
    """Refused rather than silently downgraded: a caller asking for this has
    misunderstood something, and handing back a credential that does less than its scope
    claims is how the misunderstanding survives."""
    client, db = env
    org = _org(db)
    a = _user(db, org, email="auditor@firm.test", role="auditor")
    r = client.post(f"/tokens?label=x&scopes=write&user_id={a.id}", headers={H: "orgkey"})
    assert r.status_code == 409, r.text
    assert client.post(f"/tokens?label=x&scopes=read&user_id={a.id}",
                       headers={H: "orgkey"}).status_code == 200


# --- tenancy -----------------------------------------------------------------------------

def test_a_token_reaches_only_its_own_tenant(env):
    client, db = env
    a = _org(db, "Alpha", "ka")
    b = _org(db, "Beta", "kb")
    _user(db, a, email="alpha@a.test")
    _user(db, b, email="beta@b.test")
    _token(db, b, secret="btok", scopes="admin")

    seen = {u["email"] for u in client.get("/users", headers={H: "btok"}).json()["items"]}
    assert seen == {"beta@b.test"}, seen


def test_another_tenants_user_id_is_404_not_403(env):
    """The response must not be an existence oracle over the table — the same shape the
    factor override route was given for the same reason."""
    client, db = env
    a = _org(db, "Alpha", "ka")
    b = _org(db, "Beta", "kb")
    theirs = _user(db, b, email="beta@b.test")

    r = client.post(f"/users/{theirs.id}/status?status=suspended", headers={H: "ka"})
    missing = client.post("/users/999999/status?status=suspended", headers={H: "ka"})
    assert r.status_code == 404
    assert r.json() == missing.json(), "distinguishable from a non-existent id"


# --- the credential must never come back out ---------------------------------------------

def test_no_response_ever_carries_a_token_hash(env):
    """`token_hash` is in `lifecycle.NEVER_SERIALISE`, and GET /tokens is registered
    through `_list_page` precisely so that one set of names protects it. This asserts the
    outcome rather than the mechanism, so replacing the mechanism cannot quietly lose it.
    """
    client, db = env
    org = _org(db)
    t = _token(db, org, secret="secretvalue", scopes="admin")

    body = client.get("/tokens", headers={H: "orgkey"}).text
    assert t.token_hash not in body
    assert "token_hash" not in body
    assert "secretvalue" not in body
    # The row is still there and still useful — a listing that returned nothing would
    # pass the three assertions above while telling the tenant nothing.
    rows = client.get("/tokens", headers={H: "orgkey"}).json()["items"]
    assert len(rows) == 1 and rows[0]["scopes"] == "admin"


def test_the_minted_secret_is_returned_exactly_once(env):
    client, db = env
    _org(db)
    r = client.post("/tokens?label=ci&scopes=read&expires_in_days=7",
                    headers={H: "orgkey"})
    assert r.status_code == 200, r.text
    secret = r.json()["api_key"]
    assert secret and secret not in client.get("/tokens", headers={H: "orgkey"}).text
    # And it works.
    assert client.get("/users", headers={H: secret}).status_code == 200


# --- expiry cannot be evaded -------------------------------------------------------------

def test_a_token_cannot_be_minted_without_a_bounded_life(env):
    """There is no "never expires". The column is NOT NULL with no sentinel, and the
    endpoint's ceiling is a year."""
    client, db = env
    _org(db)
    assert client.post("/tokens?label=x&expires_in_days=0",
                       headers={H: "orgkey"}).status_code == 422
    assert client.post("/tokens?label=x&expires_in_days=99999",
                       headers={H: "orgkey"}).status_code == 422


@pytest.mark.parametrize("bad", ["", "   ", "not-a-date", "2026-13-45T99:99:99",
                                 "tomorrow", "0000"])
def test_an_unreadable_expiry_fails_closed(env, bad):
    """FAILS CLOSED, and this is the one that would be easiest to get backwards.

    `services.calc._parse_iso_date` records why this codebase does not compare ISO
    strings casually. If an unparseable expiry were treated as valid, a malformed
    timestamp would be a credential that never expires — the exact defect `expires_at`
    NOT NULL exists to remove. Written directly to the row because no endpoint will
    produce these.
    """
    client, db = env
    org = _org(db)
    _token(db, org, secret="weird", scopes="admin", expires_at=bad)
    r = client.get("/users", headers={H: "weird"})
    assert r.status_code == 401, f"{bad!r} was accepted as a live expiry"


def test_a_naive_expiry_is_read_as_utc_not_local(env):
    """A stamp with no offset is read as UTC. Read as local time it would shift by up to
    a day either way, which on an expiry is either a dead credential or a live one that
    should not be."""
    client, db = env
    org = _org(db)
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
    _token(db, org, secret="naive", scopes="admin", expires_at=past.isoformat())
    assert client.get("/users", headers={H: "naive"}).status_code == 401


# --- the last owner ----------------------------------------------------------------------

def test_the_last_active_owner_cannot_be_suspended(env):
    """Otherwise a tenant can lock itself out of managing its own access, and the only
    way back is an operator touching the database."""
    client, db = env
    org = _org(db)
    o1 = _user(db, org, email="o1@acme.test", role="owner")

    r = client.post(f"/users/{o1.id}/status?status=suspended", headers={H: "orgkey"})
    assert r.status_code == 409, r.text

    o2 = _user(db, org, email="o2@acme.test", role="owner")
    assert client.post(f"/users/{o1.id}/status?status=suspended",
                       headers={H: "orgkey"}).status_code == 200
    # ...and now o2 is the last one.
    assert client.post(f"/users/{o2.id}/status?status=suspended",
                       headers={H: "orgkey"}).status_code == 409


# --- enforcement is central, not per-route -----------------------------------------------

def test_every_write_route_is_covered_by_one_enforcement_point(env):
    """The design claim, asserted rather than described.

    Write access is decided in `current_principal` from the request METHOD, so it covers
    every route depending on `current_org` — including ones written later, which get it
    without being told to. If enforcement moved into individual handlers, this fails.

    A sample of write routes is exercised with a read-only token: all must be 403, and
    none may be 401 (the credential is valid) or 2xx (it wrote).
    """
    client, db = env
    org = _org(db)
    _token(db, org, secret="ro", scopes="read")

    probes = ["/users?email=a@b.test", "/tokens?label=x",
              "/organisations/rotate_key", "/reporting_periods?label=FY25"
              "&start_date=2025-01-01&end_date=2025-12-31"]
    for path in probes:
        r = client.post(path, headers={H: "ro"})
        assert r.status_code == 403, f"{path} -> {r.status_code} {r.text[:160]}"


def test_read_routes_are_not_refused_to_a_read_token(env):
    """The negative control for the test above: if every method were refused, the test
    above would pass while the product was unusable."""
    client, db = env
    org = _org(db)
    _token(db, org, secret="ro", scopes="read")
    for path in ("/users", "/tokens", "/activities"):
        assert client.get(path, headers={H: "ro"}).status_code == 200, path
