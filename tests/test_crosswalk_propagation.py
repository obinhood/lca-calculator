"""Crosswalk uncertainty reaching the Monte Carlo band.

This closes the half of the crosswalk work that was missing. services/crosswalk.py
could MEASURE a hop's uncertainty, but nothing consumed it: the calculator was
standalone and the band never widened for a spend line mapped through three
ambiguous hops. The task claimed it fed the propagation. It did not.

The test that matters is the last one: it measures the interval with and without
the chain and asserts it actually widened.
"""
import json

import pytest

from app.models import (
    ActivityRecord, EmissionFactor, EmissionLineItem, Organisation, PriceIndex,
)
from app.services.calc import compute_co2e
from app.services.crosswalk import activity_verdict, add_mappings, register
from app.services.uncertainty import propagate

_SEQ = [0]


def _org(db):
    _SEQ[0] += 1
    o = Organisation(name=f"XwOrg{_SEQ[0]}")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _price_index(db):
    """A spend line needs a price index to inflation-adjust to the factor's base
    year — the engine refuses rather than assuming parity, which is correct."""
    for year, idx in ((2024, 100.0), (2027, 112.0)):
        if not db.query(PriceIndex).filter(PriceIndex.currency == "USD",
                                           PriceIndex.year == year).first():
            db.add(PriceIndex(currency="USD", year=year, index_value=idx))
    db.commit()


def _spend_factor(db, code, value):
    _price_index(db)
    f = EmissionFactor(source="USEEIO supply chain", version="1", geography="US",
                       year=2024, category="spend", subcategory=code, unit="USD",
                       gwp_set="AR6", value=value, method_type="spend_based",
                       base_year=2024)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org, factor, chain=None, quantity=1000.0):
    a = ActivityRecord(organisation_id=org.id, date="2027-01-01", category="spend",
                       subcategory="", description="purchased services",
                       quantity=quantity, unit="USD", geo="US",
                       factor_id=factor.id, mapping_basis="category_only",
                       mapping_status="approved",
                       crosswalk_chain=json.dumps(chain) if chain else None)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _ambiguous_chain(db, spread=(1.0, 21.0)):
    """A registered hop whose candidates disperse widely."""
    cid = register(db, from_scheme="ISIC", to_scheme="NAICS", source="UNSD",
                   table_version="rev4-2017")["id"]
    codes = [f"c{i}" for i in range(len(spread))]
    add_mappings(db, cid, [{"from_code": "2394", "to_code": c} for c in codes])
    for c, v in zip(codes, spread):
        _spend_factor(db, c, v)
    return [{"from_scheme": "ISIC", "from_code": "2394", "to_scheme": "NAICS"}]


# --- the verdict is frozen onto the line -------------------------------------

def test_no_declared_chain_freezes_nothing(db):
    org = _org(db)
    a = _activity(db, org, _spend_factor(db, "base", 2.0))
    assert activity_verdict(db, a) is None


def test_a_declared_chain_is_frozen_onto_the_line(db):
    org = _org(db)
    chain = _ambiguous_chain(db)
    a = _activity(db, org, _spend_factor(db, "base", 2.0), chain=chain)
    run = compute_co2e(db, org.id)
    line = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).first()
    xw = json.loads(line.details)["crosswalk"]
    assert xw["declared"] is True and xw["quantifiable"] is True
    assert xw["total_variance"] > 0
    assert xw["hops"][0]["cardinality"] == 2


def test_a_line_without_a_chain_freezes_null(db):
    org = _org(db)
    _activity(db, org, _spend_factor(db, "base", 2.0))
    run = compute_co2e(db, org.id)
    line = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).first()
    assert json.loads(line.details)["crosswalk"] is None


def test_a_malformed_chain_is_unquantifiable_not_zero(db):
    org = _org(db)
    a = _activity(db, org, _spend_factor(db, "base", 2.0))
    a.crosswalk_chain = "{not json"
    db.commit()
    v = activity_verdict(db, a)
    assert v["declared"] is True and v["quantifiable"] is False


def test_an_unresolvable_chain_is_unquantifiable(db):
    org = _org(db)
    a = _activity(db, org, _spend_factor(db, "base", 2.0),
                  chain=[{"from_scheme": "ISIC", "from_code": "nope",
                          "to_scheme": "NAICS"}])
    v = activity_verdict(db, a)
    assert v["quantifiable"] is False
    assert v["unresolved_hops"]


# --- the propagation -----------------------------------------------------------

def test_the_flag_is_off_by_default_and_says_so(db):
    org = _org(db)
    chain = _ambiguous_chain(db)
    for _ in range(4):
        _activity(db, org, _spend_factor(db, "base", 2.0), chain=chain)
    run = compute_co2e(db, org.id)
    r = propagate(db, run.id, iterations=2000)
    assert r["crosswalk"]["included"] is False
    assert "NOT INCLUDED" in r["crosswalk"]["note"]


def test_the_payload_counts_declared_and_undeclared_lines(db):
    org = _org(db)
    chain = _ambiguous_chain(db)
    base = _spend_factor(db, "base", 2.0)
    for _ in range(3):
        _activity(db, org, base, chain=chain)
    for _ in range(2):
        _activity(db, org, base)                    # no chain
    run = compute_co2e(db, org.id)
    xw = propagate(db, run.id, iterations=2000, include_crosswalk=True)["crosswalk"]
    assert xw["lines_with_declared_chain"] == 3
    assert xw["lines_with_quantified_chain"] == 3
    assert xw["lines_without_a_declared_chain"] == 2
    assert "NOT the same as carrying no crosswalk error" in xw["undeclared_note"]


def test_an_undeclared_chain_is_unquantified_not_absent(db):
    org = _org(db)
    base = _spend_factor(db, "base", 2.0)
    for _ in range(4):
        _activity(db, org, base)
    run = compute_co2e(db, org.id)
    r = propagate(db, run.id, iterations=2000, include_crosswalk=True)
    assert r["crosswalk"]["lines_without_a_declared_chain"] == 4
    assert r["crosswalk"]["lines_with_quantified_chain"] == 0


def test_the_fingerprint_moves_with_the_flag(db):
    """Two different bands must never share a hash."""
    org = _org(db)
    chain = _ambiguous_chain(db)
    for _ in range(4):
        _activity(db, org, _spend_factor(db, "base", 2.0), chain=chain)
    run = compute_co2e(db, org.id)
    off = propagate(db, run.id, iterations=2000)["reproducibility"]["input_fingerprint"]
    on = propagate(db, run.id, iterations=2000,
                   include_crosswalk=True)["reproducibility"]["input_fingerprint"]
    assert off != on


def test_a_one_to_one_chain_adds_exactly_zero(db):
    """An unambiguous mapping adds exactly zero — the whole point of measuring
    rather than scoring.

    Asserted on the SIGMAS rather than the sampled percentiles: the flag changes
    the seed (deliberately, so two different bands cannot share a hash), so the
    two runs draw different samples and comparing percentiles would be comparing
    Monte Carlo noise.
    """
    from app.services.uncertainty import _load_lines
    org = _org(db)
    cid = register(db, from_scheme="CPA", to_scheme="CPC", source="Eurostat",
                   table_version="2.1")["id"]
    add_mappings(db, cid, [{"from_code": "A01", "to_code": "only"}])
    _spend_factor(db, "only", 3.0)
    chain = [{"from_scheme": "CPA", "from_code": "A01", "to_scheme": "CPC"}]
    for _ in range(5):
        _activity(db, org, _spend_factor(db, "base", 2.0), chain=chain)
    run = compute_co2e(db, org.id)

    off = [l["sigma"] for l in _load_lines(db, run.id, "location", False)]
    on = [l["sigma"] for l in _load_lines(db, run.id, "location", True)]
    assert on == off

    r = propagate(db, run.id, iterations=2000, include_crosswalk=True)
    assert r["crosswalk"]["lines_with_declared_chain"] == 5
    assert r["crosswalk"]["lines_with_quantified_chain"] == 0   # zero variance


def test_an_ambiguous_chain_raises_every_line_sigma(db):
    """The deterministic counterpart: sigma itself moves, before any sampling."""
    from app.services.uncertainty import _load_lines
    org = _org(db)
    chain = _ambiguous_chain(db, spread=(1.0, 21.0))
    for _ in range(5):
        _activity(db, org, _spend_factor(db, "base", 2.0), chain=chain)
    run = compute_co2e(db, org.id)

    off = [l["sigma"] for l in _load_lines(db, run.id, "location", False)]
    on = [l["sigma"] for l in _load_lines(db, run.id, "location", True)]
    assert all(b > a for a, b in zip(off, on))


def test_an_ambiguous_chain_widens_the_band(db):
    """THE POINT, measured rather than asserted. The registry has recorded since
    the beginning that a chart-of-accounts to NAICS mapping often carries more
    error than the factor itself. This is where that finally shows up in the
    number."""
    org = _org(db)
    chain = _ambiguous_chain(db, spread=(1.0, 21.0))
    for _ in range(6):
        _activity(db, org, _spend_factor(db, "base", 2.0), chain=chain)
    run = compute_co2e(db, org.id)

    off = propagate(db, run.id, iterations=20000)
    on = propagate(db, run.id, iterations=20000, include_crosswalk=True)
    off_w = off["interval"]["relative_half_width_pct"]
    on_w = on["interval"]["relative_half_width_pct"]
    assert on_w > off_w * 1.2, (off_w, on_w)
    assert on["crosswalk"]["included"] is True


# --- endpoint ------------------------------------------------------------------

@pytest.fixture
def env():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient
    from app.database import Base
    from app import main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    client = TestClient(main_mod.app)
    key = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    hdr = {"X-API-Key": key}

    seed = TestingSession()
    org = seed.query(Organisation).filter(Organisation.name == "A").one()
    chain = _ambiguous_chain(seed)
    for _ in range(4):
        _activity(seed, org, _spend_factor(seed, "base", 2.0))
    seed.close()
    yield client, hdr, chain
    main_mod.app.dependency_overrides.clear()


def test_declaring_a_chain_needs_a_selector(env):
    client, hdr, chain = env
    r = client.post("/activities/crosswalk_chain", headers=hdr, json=chain)
    assert r.status_code == 400
    assert "defeat the purpose" in r.json()["detail"]


def test_declaring_a_chain_returns_its_resolved_uncertainty(env):
    client, hdr, chain = env
    r = client.post("/activities/crosswalk_chain", headers=hdr,
                    params={"category": "spend"}, json=chain)
    assert r.status_code == 200
    body = r.json()
    assert body["activities_updated"] == 4
    assert body["chain"]["quantifiable"] is True
    assert "frozen onto each line at the NEXT calculation" in body["note"]


def test_an_empty_chain_is_refused(env):
    client, hdr, _ = env
    r = client.post("/activities/crosswalk_chain", headers=hdr,
                    params={"category": "spend"}, json=[])
    assert r.status_code == 400


def test_the_uncertainty_endpoint_exposes_the_flag(env):
    client, hdr, chain = env
    client.post("/activities/crosswalk_chain", headers=hdr,
                params={"category": "spend"}, json=chain)
    run_id = client.post("/calculate/run", headers=hdr).json()["run"]["id"]
    body = client.get(f"/runs/{run_id}/uncertainty", headers=hdr,
                      params={"iterations": 2000,
                              "include_crosswalk": True}).json()
    assert body["crosswalk"]["included"] is True
    assert body["crosswalk"]["lines_with_quantified_chain"] == 4
