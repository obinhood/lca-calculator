"""Monte Carlo propagation of pedigree uncertainty to an inventory interval.

The tests that matter here are the honesty ones: determinism (an assurer must get
the same interval twice), correlation ordering (independence is the flattering
assumption and must never be the default), coverage disclosure (an interval over
part of an inventory must say so), and fail-closed behaviour on unscorable lines.
"""
import json
import math

import pytest

from app.models import (
    ActivityRecord, CalculationRun, EmissionFactor, EmissionLineItem, Organisation,
    FinancedPosition, RunFinancedLine, RemovalRecord, RunRemovalLine,
)
from app.services.calc import compute_co2e
from app.services.uncertainty import (
    propagate, SIGMA_UNSCORED, PROPAGATION_VERSION, DEFAULT_CORRELATION,
    MIN_ITERATIONS, MAX_ITERATIONS,
)


# --- fixtures ---------------------------------------------------------------

def _org(db, name="UncertaintyOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", value=0.17, unit="kWh", geography="GB",
            year=2024, method_type="average_data"):
    f = EmissionFactor(source="TEST", version="1", geography=geography, year=year,
                       category=category, subcategory="", unit=unit, gwp_set="AR6",
                       value=value, method_type=method_type)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor, quantity=100.0, category="electricity",
              geo="GB", mapping_basis="exact"):
    a = ActivityRecord(organisation_id=org_id, date="2024-06-01", category=category,
                       subcategory="grid", description="metered", quantity=quantity,
                       unit=factor.unit, geo=geo, factor_id=factor.id,
                       mapping_basis=mapping_basis)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _run_with(db, n_activities=6, factors=1):
    """A completed run over `n_activities` activities spread across `factors` factors."""
    org = _org(db)
    fs = [_factor(db, value=0.1 + 0.01 * i) for i in range(factors)]
    for i in range(n_activities):
        _activity(db, org.id, fs[i % factors], quantity=100.0 + i)
    run = compute_co2e(db, org.id)
    return org, run


# --- the interval exists and brackets the total -----------------------------

def test_interval_brackets_the_deterministic_total(db):
    _, run = _run_with(db, n_activities=6)
    r = propagate(db, run.id, iterations=MIN_ITERATIONS)
    assert r["available"] is True
    total = r["deterministic_total_co2e_kg"]
    assert total > 0
    assert r["interval"]["low"] < total < r["interval"]["high"]
    assert r["propagation_version"] == PROPAGATION_VERSION
    assert r["correlation"] == DEFAULT_CORRELATION == "by_factor"


def test_percentiles_follow_the_confidence_level(db):
    _, run = _run_with(db)
    r95 = propagate(db, run.id, iterations=MIN_ITERATIONS, confidence=0.95)
    r50 = propagate(db, run.id, iterations=MIN_ITERATIONS, confidence=0.50)
    assert r95["interval"]["percentiles"] == {"low": 2.5, "high": 97.5}
    assert r50["interval"]["percentiles"] == {"low": 25.0, "high": 75.0}
    # A tighter confidence level must produce a narrower band.
    w95 = r95["interval"]["high"] - r95["interval"]["low"]
    w50 = r50["interval"]["high"] - r50["interval"]["low"]
    assert w50 < w95


# --- determinism: the assurance requirement ---------------------------------

def test_same_inputs_return_bit_identical_numbers(db):
    _, run = _run_with(db)
    a = propagate(db, run.id, iterations=MIN_ITERATIONS)
    b = propagate(db, run.id, iterations=MIN_ITERATIONS)
    assert a["interval"] == b["interval"]
    assert a["correlation_bounds"] == b["correlation_bounds"]
    assert a["sensitivity"] == b["sensitivity"]
    assert a["reproducibility"]["input_fingerprint"] == b["reproducibility"]["input_fingerprint"]
    assert a["reproducibility"]["seed"] == b["reproducibility"]["seed"]


def test_fingerprint_moves_when_an_input_moves(db):
    _, run = _run_with(db)
    base = propagate(db, run.id, iterations=MIN_ITERATIONS)["reproducibility"]["input_fingerprint"]
    # Same run, different parameters -> different fingerprint (and different seed).
    assert propagate(db, run.id, iterations=MIN_ITERATIONS,
                     confidence=0.90)["reproducibility"]["input_fingerprint"] != base
    assert propagate(db, run.id, iterations=MIN_ITERATIONS,
                     correlation="perfect")["reproducibility"]["input_fingerprint"] != base
    assert propagate(db, run.id, iterations=MIN_ITERATIONS * 2
                     )["reproducibility"]["input_fingerprint"] != base


def test_fingerprint_is_insensitive_to_row_order(db):
    """Two runs whose lines carry the same medians/sigmas/groups in a different
    order must fingerprint identically — otherwise the seed depends on insertion
    order and the 'reproducible' claim is false."""
    from app.services.uncertainty import _fingerprint
    a = {"group": "factor:1", "median": 10.0, "sigma": 0.5}
    b = {"group": "factor:2", "median": 20.0, "sigma": 0.25}
    fwd = _fingerprint(1, "location", "by_factor", 1000, 0.95, [a, b])
    rev = _fingerprint(1, "location", "by_factor", 1000, 0.95, [b, a])
    assert fwd == rev


# --- correlation: independence must never be the flattering default ---------

def test_correlation_modes_are_ordered_independent_narrowest(db):
    """The core methodological claim. Independent sampling cancels error across
    lines and produces the narrowest band; perfect correlation the widest; the
    by-factor default sits between. If this ordering ever inverts, the default is
    overstating precision."""
    _, run = _run_with(db, n_activities=24, factors=4)
    r = propagate(db, run.id, iterations=4000)
    b = r["correlation_bounds"]
    width = lambda k: b[k]["high"] - b[k]["low"]
    assert width("independent") < width("by_factor") < width("perfect")


def test_independence_shrinks_the_band_as_lines_multiply(db):
    """Why the default is not 'independent': splitting ONE quantity across many
    lines must not manufacture precision. Under independent sampling it does
    (~sqrt(n)); under the by_factor default — same factor, so common-mode — it
    does not."""
    org = _org(db)
    f = _factor(db)
    for _ in range(40):
        _activity(db, org.id, f, quantity=10.0)   # 40 lines x 10 == 400 units
    many = compute_co2e(db, org.id)

    org2 = _org(db, "Lumped")
    f2 = _factor(db)
    _activity(db, org2.id, f2, quantity=400.0)    # 1 line x 400 == same quantity
    one = compute_co2e(db, org2.id)

    m_ind = propagate(db, many.id, correlation="independent", iterations=4000)
    o_ind = propagate(db, one.id, correlation="independent", iterations=4000)
    m_fac = propagate(db, many.id, correlation="by_factor", iterations=4000)
    o_fac = propagate(db, one.id, correlation="by_factor", iterations=4000)

    # Same total either way.
    assert m_ind["deterministic_total_co2e_kg"] == pytest.approx(
        o_ind["deterministic_total_co2e_kg"], rel=1e-9)

    # Independent: splitting the SAME quantity into 40 lines fabricates precision.
    assert m_ind["interval"]["relative_half_width_pct"] < \
        0.5 * o_ind["interval"]["relative_half_width_pct"]

    # by_factor: the 40 lines share one factor draw, so the band barely moves.
    assert m_fac["interval"]["relative_half_width_pct"] == pytest.approx(
        o_fac["interval"]["relative_half_width_pct"], rel=0.15)


def test_perfect_correlation_withholds_variance_shares(db):
    """Under one common-mode source there is no independent-contribution
    decomposition — the module must refuse the ranking, not fabricate one."""
    _, run = _run_with(db, n_activities=6, factors=3)
    r = propagate(db, run.id, correlation="perfect", iterations=MIN_ITERATIONS)
    assert r["sensitivity"]["available"] is False
    assert r["sensitivity"]["contributors"] == []
    assert "decomposition" in r["sensitivity"]["reason"]


# --- skew: mean above median is correct, not a bug --------------------------

def test_simulated_mean_exceeds_the_median_total(db):
    """A pedigree GSD makes the reported total the MEDIAN of a right-skewed
    lognormal, so the simulated mean sits above it. Asserted so nobody later
    'fixes' the total toward the mean."""
    _, run = _run_with(db, n_activities=8)
    r = propagate(db, run.id, iterations=8000)
    assert r["interval"]["simulated_mean"] > r["deterministic_total_co2e_kg"]
    assert r["interval"]["simulated_median"] == pytest.approx(
        r["deterministic_total_co2e_kg"], rel=0.08)
    assert "right-skewed" in r["skew_note"]


# --- coverage: an interval over part of an inventory must say so ------------

def test_full_coverage_when_nothing_is_excluded(db):
    _, run = _run_with(db)
    cov = propagate(db, run.id, iterations=MIN_ITERATIONS)["coverage"]
    assert cov["reconciles_to_run_total"] is True
    assert cov["reconciliation_drift_kg"] is None
    assert cov["covers_full_inventory"] is True
    assert cov["excluded_pools"] == []


def test_financed_emissions_are_disclosed_as_excluded(db):
    """PCAF Cat 15 lives outside EmissionLineItem and carries no pedigree sigma.
    The interval cannot cover it, so the payload must name it and its amount
    rather than implying whole-inventory coverage."""
    org, run = _run_with(db)
    pos = FinancedPosition(organisation_id=org.id, asset_class="listed_equity",
                           investee_name="Acme", outstanding_amount=1_000_000.0,
                           attribution_denominator=5_000_000.0,
                           currency="USD", data_quality_score=4)
    db.add(pos); db.commit(); db.refresh(pos)
    db.add(RunFinancedLine(run_id=run.id, position_id=pos.id, ghgp_category=15,
                           co2e=250_000.0, details=json.dumps({"frozen": True})))
    db.commit()

    cov = propagate(db, run.id, iterations=MIN_ITERATIONS)["coverage"]
    assert cov["covers_full_inventory"] is False
    pools = {p["pool"]: p for p in cov["excluded_pools"]}
    assert "financed_emissions_cat15" in pools
    assert pools["financed_emissions_cat15"]["co2e_kg"] == 250_000.0
    assert cov["note"]
    # The propagated figure still reconciles to the run total — financed lines were
    # never part of it. Both facts have to be visible at once.
    assert cov["reconciles_to_run_total"] is True


def test_removals_are_disclosed_as_excluded(db):
    org, run = _run_with(db)
    rec = RemovalRecord(organisation_id=org.id, removal_category="technological",
                        method="engineered", scope="1", quantity_tco2e=10.0,
                        quantification_method="measured", record_kind="removal")
    db.add(rec); db.commit(); db.refresh(rec)
    db.add(RunRemovalLine(run_id=run.id, removal_record_id=rec.id,
                          removal_category="technological", scope="1",
                          record_kind="removal", co2e=10_000.0,
                          details=json.dumps({"frozen": True})))
    db.commit()
    cov = propagate(db, run.id, iterations=MIN_ITERATIONS)["coverage"]
    pools = {p["pool"]: p for p in cov["excluded_pools"]}
    assert pools["removals"]["co2e_kg"] == 10_000.0
    assert cov["covers_full_inventory"] is False


def test_reconciliation_drift_is_surfaced_not_absorbed(db):
    """If the frozen lines no longer sum to the run total, the module reports the
    drift instead of quietly presenting an interval around a different number."""
    _, run = _run_with(db)
    run.total_co2e = (run.total_co2e or 0.0) + 12345.0
    db.commit()
    cov = propagate(db, run.id, iterations=MIN_ITERATIONS)["coverage"]
    assert cov["reconciles_to_run_total"] is False
    assert cov["reconciliation_drift_kg"] == pytest.approx(-12345.0, rel=1e-6)
    assert cov["covers_full_inventory"] is False


# --- unscored lines fail conservative, never flattering ---------------------

def test_line_without_a_pedigree_score_uses_the_all_poor_sigma(db):
    _, run = _run_with(db, n_activities=3)
    line = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).order_by(EmissionLineItem.id).first()
    detail = json.loads(line.details)
    detail.pop("data_quality")
    line.details = json.dumps(detail)
    db.commit()

    r = propagate(db, run.id, iterations=MIN_ITERATIONS)
    assert r["lines"]["unscored"] == 1
    # Payload rounds to 4 dp for readability; the applied sigma is the full value.
    assert r["lines"]["unscored_sigma_applied"] == pytest.approx(SIGMA_UNSCORED, abs=5e-5)
    assert "widens the band" in r["lines"]["unscored_note"]


def test_unscored_sigma_is_worse_than_any_scored_line(db):
    """The conservative default must actually be conservative: dropping a line's
    score has to widen the interval, never narrow it."""
    _, run = _run_with(db, n_activities=4)
    before = propagate(db, run.id, iterations=8000)["interval"]["relative_half_width_pct"]
    for line in db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id).all():
        detail = json.loads(line.details)
        detail.pop("data_quality", None)
        line.details = json.dumps(detail)
    db.commit()
    after = propagate(db, run.id, iterations=8000)["interval"]["relative_half_width_pct"]
    assert after > before


def test_malformed_sigma_is_treated_as_unscored(db):
    _, run = _run_with(db, n_activities=2)
    for bad in (None, "0.4", float("nan"), -1.0):
        line = db.query(EmissionLineItem).filter(
            EmissionLineItem.run_id == run.id).order_by(EmissionLineItem.id).first()
        detail = json.loads(line.details)
        detail["data_quality"]["sigma_log"] = bad
        line.details = json.dumps(detail)
        db.commit()
        r = propagate(db, run.id, iterations=MIN_ITERATIONS)
        assert r["lines"]["unscored"] >= 1, bad


# --- sensitivity ------------------------------------------------------------

def test_variance_shares_rank_and_sum_to_one_hundred(db):
    _, run = _run_with(db, n_activities=12, factors=4)
    s = propagate(db, run.id, iterations=4000)["sensitivity"]
    assert s["available"] is True
    shares = [c["variance_share_pct"] for c in s["contributors"]]
    assert shares == sorted(shares, reverse=True)
    assert sum(shares) == pytest.approx(100.0, abs=0.05)


def test_top_n_limits_the_contributor_list(db):
    _, run = _run_with(db, n_activities=12, factors=6)
    s = propagate(db, run.id, iterations=MIN_ITERATIONS, top_n=2)["sensitivity"]
    assert len(s["contributors"]) == 2


def test_the_dominant_factor_ranks_first(db):
    """A factor carrying most of the emissions must lead the ranking."""
    org = _org(db)
    big, small = _factor(db, value=10.0), _factor(db, value=0.001)
    _activity(db, org.id, big, quantity=1000.0)
    for _ in range(5):
        _activity(db, org.id, small, quantity=1.0)
    run = compute_co2e(db, org.id)
    s = propagate(db, run.id, iterations=4000)["sensitivity"]
    assert s["contributors"][0]["group"] == f"factor:{big.id}"
    assert s["contributors"][0]["variance_share_pct"] > 90.0


# --- refusals ---------------------------------------------------------------

def test_unknown_run_refuses(db):
    r = propagate(db, 999_999)
    assert r["available"] is False
    assert "not found" in r["reason"]


def test_run_with_no_lines_refuses_with_run_context(db):
    org = _org(db)
    run = CalculationRun(organisation_id=org.id, status="complete", total_co2e=0.0)
    db.add(run); db.commit(); db.refresh(run)
    r = propagate(db, run.id)
    assert r["available"] is False
    assert r["run_id"] == run.id
    assert "no emission lines on the location basis" in r["reason"]


def test_zero_sigma_inventory_refuses_instead_of_restating_the_point(db):
    _, run = _run_with(db, n_activities=3)
    for line in db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id).all():
        detail = json.loads(line.details)
        detail["data_quality"]["sigma_log"] = 0.0
        line.details = json.dumps(detail)
    db.commit()
    r = propagate(db, run.id)
    assert r["available"] is False
    assert "no quantified uncertainty" in r["reason"]
    # It still tells the caller the total it would have described.
    assert r["deterministic_total_co2e_kg"] > 0


@pytest.mark.parametrize("kwargs, needle", [
    ({"correlation": "wishful"}, "correlation must be"),
    ({"method": "sideways"}, "method must be"),
    ({"iterations": MIN_ITERATIONS - 1}, "iterations must be"),
    ({"iterations": MAX_ITERATIONS + 1}, "iterations must be"),
    ({"confidence": 1.0}, "confidence must be"),
    ({"confidence": 0.4}, "confidence must be"),
])
def test_bad_parameters_refuse(db, kwargs, needle):
    _, run = _run_with(db, n_activities=2)
    r = propagate(db, run.id, **kwargs)
    assert r["available"] is False
    assert needle in r["reason"]
    # A parameter refusal carries no run context — that is how the endpoint
    # distinguishes a 400 from a legitimate 200 "this run cannot be propagated".
    assert "run_id" not in r


# --- market basis -----------------------------------------------------------

def _mixed_scope_run(db):
    """One Scope 2 activity (gets a market line) and one Scope 1 (does not)."""
    org = _org(db)
    elec = _factor(db, category="electricity", value=0.2, unit="kWh")
    gas = _factor(db, category="natural_gas", value=2.0, unit="m3")
    _activity(db, org.id, elec, quantity=1000.0, category="electricity")
    _activity(db, org.id, gas, quantity=5000.0, category="natural_gas")
    return org, compute_co2e(db, org.id)


def test_market_basis_covers_the_whole_market_total(db):
    """REGRESSION. The engine writes a market line only for Scope 2; every other
    activity carries its LOCATION figure into total_co2e_market. Selecting on
    method == 'market' therefore returned Scope 2 alone — on this inventory it
    propagated 200 of 10,200 kg and reported the missing 98% as reconciliation
    drift, i.e. an interval over 2% of the inventory presented as a failed
    reconciliation rather than as a wrong basis."""
    _, run = _mixed_scope_run(db)
    r = propagate(db, run.id, method="market", iterations=MIN_ITERATIONS)
    assert r["available"] is True
    cov = r["coverage"]
    assert cov["propagated_co2e_kg"] == pytest.approx(run.total_co2e_market, rel=1e-9)
    assert cov["reconciles_to_run_total"] is True
    assert cov["covers_full_inventory"] is True


def test_market_basis_declares_how_it_was_assembled(db):
    _, run = _mixed_scope_run(db)
    comp = propagate(db, run.id, method="market",
                     iterations=MIN_ITERATIONS)["coverage"]["basis_composition"]
    assert comp["market_priced_lines"] == 1       # the Scope 2 electricity line
    assert comp["location_carried_lines"] == 1    # the Scope 1 gas line
    assert "only Scope 2" in comp["note"]


def test_location_basis_is_unaffected_by_the_market_assembly(db):
    _, run = _mixed_scope_run(db)
    cov = propagate(db, run.id, method="location",
                    iterations=MIN_ITERATIONS)["coverage"]
    assert cov["propagated_co2e_kg"] == pytest.approx(run.total_co2e, rel=1e-9)
    assert cov["reconciles_to_run_total"] is True
    assert "basis_composition" not in cov


def test_market_and_location_differ_only_where_scope_2_is_repriced(db):
    org = _org(db)
    f = _factor(db, category="electricity")
    _activity(db, org.id, f, quantity=500.0, category="electricity")
    run = compute_co2e(db, org.id)
    loc = propagate(db, run.id, method="location", iterations=MIN_ITERATIONS)
    mkt = propagate(db, run.id, method="market", iterations=MIN_ITERATIONS)
    assert loc["available"] and mkt["available"]
    assert mkt["method"] == "market"
    assert mkt["coverage"]["run_total_co2e_kg"] == pytest.approx(
        run.total_co2e_market, rel=1e-9)


# --- bounds are always complete ---------------------------------------------

@pytest.mark.parametrize("requested", ["independent", "by_factor", "perfect"])
def test_all_three_correlation_bounds_are_always_reported(db, requested):
    """Whichever mode was asked for, the reader needs all three to judge how much
    the answer rests on the correlation assumption."""
    _, run = _run_with(db, n_activities=8, factors=2)
    r = propagate(db, run.id, correlation=requested, iterations=MIN_ITERATIONS)
    assert set(r["correlation_bounds"]) == {"independent", "by_factor", "perfect"}
    # The requested mode's bound is the headline interval itself, not a re-draw.
    assert r["correlation_bounds"][requested] == r["interval"]


def test_negative_lines_keep_their_sign(db):
    """A negative line is sampled on its magnitude with the sign carried through —
    a lognormal must never be asked to describe a negative quantity."""
    _, run = _run_with(db, n_activities=2)
    for line in db.query(EmissionLineItem).filter(
            EmissionLineItem.run_id == run.id).all():
        line.co2e = -abs(line.co2e)
    db.commit()
    r = propagate(db, run.id, iterations=4000)
    assert r["deterministic_total_co2e_kg"] < 0
    assert r["interval"]["low"] < 0 and r["interval"]["high"] < 0
    assert r["interval"]["low"] < r["deterministic_total_co2e_kg"] < r["interval"]["high"]
    # Half-width is a magnitude, never negative, even on a negative total.
    assert r["interval"]["relative_half_width_pct"] > 0


# --- endpoint ---------------------------------------------------------------

@pytest.fixture
def env():
    """Two orgs over one shared in-memory DB; org B owns a completed run."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient

    from app.database import Base
    from app import main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    client = TestClient(main_mod.app)
    key_a = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    key_b = client.post("/organisations", params={"name": "B"}).json()["api_key"]
    hdr_a, hdr_b = {"X-API-Key": key_a}, {"X-API-Key": key_b}

    seed = TestingSession()
    org_b = seed.query(Organisation).filter(Organisation.name == "B").one()
    f = _factor(seed)
    for i in range(4):
        _activity(seed, org_b.id, f, quantity=250.0 + i)
    run_b_id = compute_co2e(seed, org_b.id).id
    seed.close()

    yield client, hdr_a, hdr_b, run_b_id
    main_mod.app.dependency_overrides.clear()


def test_endpoint_returns_the_interval(env):
    client, _, hdr_b, run_id = env
    r = client.get(f"/runs/{run_id}/uncertainty",
                   params={"iterations": MIN_ITERATIONS}, headers=hdr_b)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    total = body["deterministic_total_co2e_kg"]
    assert body["interval"]["low"] < total < body["interval"]["high"]
    assert body["reproducibility"]["input_fingerprint"]


def test_endpoint_rejects_a_bad_parameter_with_400(env):
    client, _, hdr_b, run_id = env
    r = client.get(f"/runs/{run_id}/uncertainty",
                   params={"correlation": "wishful"}, headers=hdr_b)
    assert r.status_code == 400


def test_endpoint_scopes_runs_to_the_calling_organisation(env):
    """A run belonging to another organisation must 404, not leak an interval."""
    client, hdr_a, hdr_b, run_id = env
    assert client.get(f"/runs/{run_id}/uncertainty",
                      params={"iterations": MIN_ITERATIONS},
                      headers=hdr_b).status_code == 200
    assert client.get(f"/runs/{run_id}/uncertainty",
                      params={"iterations": MIN_ITERATIONS},
                      headers=hdr_a).status_code == 404


def test_corrupt_sigma_is_clamped_and_counted(db):
    """A sigma far above what the pedigree matrix can produce is corrupt data, not
    a poor score. Sampling it would overflow exp() and turn the interval into nan;
    the module clamps, counts and says so."""
    from app.services.uncertainty import SIGMA_CEILING
    _, run = _run_with(db, n_activities=3)
    line = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).order_by(EmissionLineItem.id).first()
    detail = json.loads(line.details)
    detail["data_quality"]["sigma_log"] = 900.0
    line.details = json.dumps(detail)
    db.commit()

    r = propagate(db, run.id, iterations=MIN_ITERATIONS)
    assert r["available"] is True
    assert r["lines"]["sigma_clamped"] == 1
    assert str(SIGMA_CEILING) in r["lines"]["sigma_clamped_note"]
    # The interval stays finite — the whole point of the clamp.
    assert math.isfinite(r["interval"]["low"])
    assert math.isfinite(r["interval"]["high"])
    assert math.isfinite(r["interval"]["simulated_mean"])


def test_clean_run_reports_no_clamping(db):
    _, run = _run_with(db, n_activities=3)
    r = propagate(db, run.id, iterations=MIN_ITERATIONS)
    assert r["lines"]["sigma_clamped"] == 0
    assert r["lines"]["sigma_clamped_note"] is None


# --- cross-renderer reconciliation ------------------------------------------

def test_summary_band_equals_the_perfect_correlation_bound(db):
    """RENDERER AGREEMENT. summary.py already publishes a closed-form
    emissions-weighted 95% band that assumes FULLY CORRELATED line errors. Under a
    single shared draw the inventory total is monotone in it, so that band is
    exactly this module's 'perfect' bound: sum(m_i * ci95_mult_i). Two uncertainty
    figures that disagreed would be a defect the reader could not adjudicate, so
    the identity is asserted rather than assumed."""
    from app.reports.summary import summary
    org = _org(db)
    facs = [_factor(db, category=f"c{i}", value=0.1 * (i + 1), year=2020 + i)
            for i in range(5)]
    for i in range(25):
        _activity(db, org.id, facs[i % 5], quantity=100.0 + i * 7,
                  category=f"c{i % 5}")
    run = compute_co2e(db, org.id)

    s = summary(db, organisation_id=org.id)["data_quality"]
    perfect = propagate(db, run.id, correlation="perfect",
                        iterations=100_000)["correlation_bounds"]["perfect"]

    # Agreement is limited only by sampling noise at the 2.5/97.5 percentiles.
    assert perfect["low"] == pytest.approx(s["approx_ci95_low"], rel=2e-3)
    assert perfect["high"] == pytest.approx(s["approx_ci95_high"], rel=2e-3)


def test_each_uncertainty_figure_points_at_the_other(db):
    """Neither figure may stand alone: a reader meeting one must be told how it
    relates to the other."""
    from app.reports.summary import summary
    org, run = _run_with(db, n_activities=4)
    s = summary(db, organisation_id=org.id)["data_quality"]
    assert s["full_propagation"]["endpoint"] == "/runs/{run_id}/uncertainty"
    assert "perfect" in s["full_propagation"]["relationship"]

    r = propagate(db, run.id, iterations=MIN_ITERATIONS)
    assert "approx_ci95" in r["reconciles_with"]["summary_data_quality_band"]
    assert "by_factor" in r["reconciles_with"]["relationship"]
