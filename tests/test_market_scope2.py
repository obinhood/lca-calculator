"""Market-based Scope 2: grid/market matching + deterministic allocation.

The headline finding: a contractual instrument was applied org-wide with no grid
match, so a US REC could zero German consumption. It now only covers same-market
load; a NULL-market instrument still applies (backward compatible) but is flagged
market_unverified.
"""
import pytest

from app.models import Organisation, ActivityRecord, EmissionFactor, MarketInstrument
from app.services.calc import compute_co2e
from app.reports.summary import summary


def _org(db, name="Co"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, geo, value):
    f = EmissionFactor(source="T", version="1", geography=geo, year=2024, category="electricity",
                       subcategory="", unit="kWh", gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _elec(db, org_id, factor_id, geo, kwh=1000.0):
    a = ActivityRecord(organisation_id=org_id, date="2025-06-01", category="electricity",
                       subcategory="", description="", quantity=kwh, unit="kWh",
                       geo=geo, factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _rec(db, org_id, market=None, coverage=1000.0, rate=0.0):
    inst = MarketInstrument(organisation_id=org_id, instrument_type="rec",
                            kg_co2e_per_kwh=rate, coverage_kwh=coverage, gwp_set="AR6",
                            market=market, start_date="2025-01-01", end_date="2025-12-31")
    db.add(inst); db.commit(); db.refresh(inst)
    return inst


def _market(db, org):
    return summary(db, organisation_id=org.id)["scope2"]


# --- The headline bug ---------------------------------------------------------

def test_us_rec_does_not_cover_german_consumption(db):
    org = _org(db)
    _elec(db, org.id, _factor(db, "DE", 0.4).id, "DE", 1000)   # 400 kg location
    rec = _rec(db, org.id, market="US")                        # a US REC
    compute_co2e(db, org.id)
    m = _market(db, org)
    assert m["location_based"] == pytest.approx(400.0)
    assert m["market_based"] == pytest.approx(400.0)           # NOT 0 — the REC can't apply
    assert rec.id in m["instruments_excluded_by_market"]


def test_matching_market_rec_does_cover(db):
    org = _org(db)
    _elec(db, org.id, _factor(db, "DE", 0.4).id, "DE", 1000)
    _rec(db, org.id, market="DE")                              # a German REC
    compute_co2e(db, org.id)
    m = _market(db, org)
    assert m["market_based"] == pytest.approx(0.0)             # correctly covered
    assert m["kwh_contractual"] == pytest.approx(1000.0)
    assert m["instruments_excluded_by_market"] == []


def test_market_match_is_case_insensitive(db):
    org = _org(db)
    _elec(db, org.id, _factor(db, "gb", 0.2).id, "gb", 500)
    _rec(db, org.id, market="GB", coverage=500)
    compute_co2e(db, org.id)
    assert _market(db, org)["market_based"] == pytest.approx(0.0)


# --- NULL market = applies but flagged ---------------------------------------

def test_null_market_instrument_applies_but_is_flagged(db):
    org = _org(db)
    _elec(db, org.id, _factor(db, "DE", 0.4).id, "DE", 1000)
    _rec(db, org.id, market=None)                              # legacy: no declared market
    compute_co2e(db, org.id)
    m = _market(db, org)
    assert m["market_based"] == pytest.approx(0.0)             # still applied (backward compat)
    assert m["kwh_market_unverified"] == pytest.approx(1000.0)  # ...but flagged


def test_unknown_activity_geo_is_unverified_not_excluded(db):
    org = _org(db)
    a = _elec(db, org.id, _factor(db, "DE", 0.4).id, "DE", 1000)
    a.geo = None                                              # consumption grid unknown
    db.commit()
    _rec(db, org.id, market="DE")
    compute_co2e(db, org.id)
    m = _market(db, org)
    assert m["market_based"] == pytest.approx(0.0)
    assert m["kwh_market_unverified"] == pytest.approx(1000.0)


# --- Determinism --------------------------------------------------------------

def test_market_total_is_order_independent(db):
    """Two markets, a REC only for one: the market total must not depend on which
    activity is processed first."""
    org = _org(db)
    _elec(db, org.id, _factor(db, "DE", 0.5).id, "DE", 1000)   # 500 kg
    _elec(db, org.id, _factor(db, "FR", 0.1).id, "FR", 1000)   # 100 kg (low-carbon grid)
    _rec(db, org.id, market="DE", coverage=1000)              # covers only DE
    r1 = compute_co2e(db, org.id)
    r2 = compute_co2e(db, org.id)
    # DE covered -> 0; FR uncovered -> 100. Total market = 100, both runs identical.
    assert r1.total_co2e_market == pytest.approx(100.0)
    assert r2.total_co2e_market == pytest.approx(r1.total_co2e_market)


def test_excluded_by_market_is_complete_regardless_of_coverage(db):
    """Adversarial-review finding: a market-mismatched instrument must be recorded
    as excluded even when a matching instrument already covered the load."""
    org = _org(db)
    _elec(db, org.id, _factor(db, "DE", 0.4).id, "DE", 100)
    _rec(db, org.id, market="DE", coverage=100)               # id=1, covers the load fully
    us = _rec(db, org.id, market="US", coverage=100)          # id=2, sits past coverage
    compute_co2e(db, org.id)
    m = _market(db, org)
    assert m["market_based"] == pytest.approx(0.0)            # DE REC covered it
    assert us.id in m["instruments_excluded_by_market"]       # US REC still flagged excluded


def test_partial_coverage_within_market(db):
    org = _org(db)
    _elec(db, org.id, _factor(db, "GB", 0.2).id, "GB", 1000)   # 200 kg location
    _rec(db, org.id, market="GB", coverage=600)               # covers 600 of 1000 kWh
    compute_co2e(db, org.id)
    m = _market(db, org)
    # 600 kWh at 0, 400 kWh at grid 0.2 = 80 kg
    assert m["market_based"] == pytest.approx(80.0)
    assert m["kwh_contractual"] == pytest.approx(600.0)
    assert m["kwh_grid_fallback"] == pytest.approx(400.0)
