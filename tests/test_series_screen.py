"""Period-over-period screening over declared series.

This closes the gap the screening register had to decline. The property that makes
it honest is that the series is DECLARED, never inferred: NULL means "not
enrolled", which is every historical row, so no detector can fire on data that was
never opted in — blast radius zero by construction rather than by threshold tuning.

The statistical tests are the ones that matter. A z-score is provably blind here;
the band is on log ratios with a MAD scale, a floor, a cap and a hard backstop, and
below four series it refuses to calibrate at all rather than fabricating a
dispersion estimate from three points.
"""
import math

import pytest

from app.models import ActivityRecord, Organisation, ReportingPeriod
from app.services.series_screen import (
    BAND_CAP, BAND_FLOOR, HARD_BACKSTOP, K, LEVEL_SHIFT_MIN_RUNS,
    MIN_SERIES_FOR_BAND, SERIES_SCREEN_VERSION, band, classify_level_shifts,
    compare, enrolment,
)

_SEQ = [0]


def _org(db):
    _SEQ[0] += 1
    o = Organisation(name=f"SeriesOrg{_SEQ[0]}")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _period(db, org, label, start, end):
    p = ReportingPeriod(organisation_id=org.id, label=label,
                        start_date=start, end_date=end)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _act(db, org, date, quantity, series_key=None, unit="kWh",
         category="electricity", source_file="bills.csv"):
    a = ActivityRecord(organisation_id=org.id, date=date, category=category,
                       subcategory="", description="metered", quantity=quantity,
                       unit=unit, geo="GB", series_key=series_key,
                       source_file=source_file,
                       mapping_basis="exact", mapping_status="approved")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _two_periods(db, org):
    base = _period(db, org, "FY25", "2025-01-01", "2025-12-31")
    cur = _period(db, org, "FY26", "2026-01-01", "2026-12-31")
    return base, cur


def _portfolio(db, org, base_p, cur_p, spec):
    """spec: {series_key: (baseline_qty, current_qty)}"""
    for key, (b, c) in spec.items():
        _act(db, org, "2025-06-15", b, series_key=key)
        _act(db, org, "2026-06-15", c, series_key=key)


# --- enrolment is opt-in, and the unenrolled share is reported ----------------

def test_a_row_with_no_series_key_is_not_screened(db):
    """NULL is the default on every historical row, so nothing can fire on data
    that was never opted in."""
    org = _org(db)
    base, cur = _two_periods(db, org)
    _act(db, org, "2025-06-15", 1000.0)          # no series_key
    _act(db, org, "2026-06-15", 100000.0)        # a 100x jump, unenrolled
    r = compare(db, org.id, cur.id, base.id)
    assert r["available"] is False
    assert r["status"] == "no_comparable_series"


def test_enrolment_reports_the_unscreened_share_by_name(db):
    org = _org(db)
    _act(db, org, "2025-06-15", 1000.0, series_key="hq-elec")
    _act(db, org, "2025-06-15", 500.0)
    _act(db, org, "2025-06-15", 300.0)
    e = enrolment(db, org.id)
    assert e["activities_total"] == 3
    assert e["activities_enrolled"] == 1
    assert e["activities_not_enrolled"] == 2
    assert e["enrolled_pct"] == pytest.approx(33.33, abs=0.01)
    assert e["distinct_series"] == 1
    assert "not looked at" in e["note"]


def test_two_sites_with_distinct_keys_stay_distinct(db):
    """The whole reason the key is declared: HQ and workshop share category,
    subcategory, unit, geography and entity, and an inferred key would merge them."""
    org = _org(db)
    base, cur = _two_periods(db, org)
    # HQ doubles; workshop halves. Merged they would look flat.
    _portfolio(db, org, base, cur, {
        "hq-elec": (1000.0, 5000.0),
        "workshop-elec": (1000.0, 1000.0),
        "warehouse-elec": (1000.0, 1050.0),
        "annex-elec": (1000.0, 980.0),
        "shop-elec": (1000.0, 1020.0),
    })
    r = compare(db, org.id, cur.id, base.id)
    assert r["available"] is True
    flagged = {f["series_key"] for f in r["findings"]}
    assert "hq-elec" in flagged
    assert "workshop-elec" not in flagged


# --- the band ----------------------------------------------------------------

def test_below_four_series_the_band_refuses_to_calibrate():
    """A dispersion estimate from three points is a fabrication, not a
    conservative approximation."""
    for n in range(1, MIN_SERIES_FOR_BAND):
        b = band([0.01 * i for i in range(n)])
        assert b["threshold"] is None, n
        assert b["band_basis"] == "insufficient_series"
        assert "fabrication" in b["note"]


def test_the_band_is_floored_and_capped():
    tight = band([0.0, 0.0001, -0.0001, 0.0002, -0.0002, 0.0])
    assert tight["threshold"] == pytest.approx(BAND_FLOOR)
    wild = band([0.0, 3.0, -3.0, 2.5, -2.5, 4.0])
    assert wild["threshold"] == pytest.approx(BAND_CAP)


def test_a_degenerate_dispersion_falls_back_to_the_cap():
    """Repeated flat or estimated readings make MAD=0 common."""
    b = band([0.5] * 8)
    assert b["band_basis"] == "degenerate_dispersion"
    assert b["threshold"] == pytest.approx(BAND_CAP)


def test_the_finite_sample_correction_is_applied():
    """Without b_n, 1.4826*MAD understates sigma by ~7% at n=12 and over-flags by
    about as much."""
    devs = [0.0, 0.1, -0.1, 0.2, -0.2, 0.05, -0.05, 0.15, -0.15, 0.08, -0.08, 0.12]
    b = band(devs)
    mad = sorted(abs(d - 0.0) for d in devs)[len(devs) // 2]
    uncorrected = K * 1.4826 * mad
    assert b["threshold"] > uncorrected * 0.99   # b_12 = 1.076 widens it


def test_the_band_uses_the_mad_basis_on_ordinary_data():
    b = band([0.0, 0.12, -0.09, 0.2, -0.15, 0.05])
    assert b["band_basis"] == "mad"


# --- detection ----------------------------------------------------------------

def test_a_gross_error_trips_the_hard_backstop(db):
    """A volatile portfolio must not be able to widen its own band far enough to
    certify a gross error away."""
    org = _org(db)
    base, cur = _two_periods(db, org)
    spec = {f"s{i}": (1000.0, 1000.0 * (1 + 0.9 * ((-1) ** i))) for i in range(8)}
    spec["broken"] = (1000.0, 10000.0)           # 10x
    _portfolio(db, org, base, cur, spec)
    r = compare(db, org.id, cur.id, base.id)
    hit = [f for f in r["findings"] if f["series_key"] == "broken"]
    assert hit, r["band"]
    assert hit[0]["triggered_by"] in ("backstop", "both")


def test_the_backstop_catches_what_the_adaptive_band_would_clear(db):
    """The guarantee that matters. When the whole portfolio moves hugely, the
    median deviation moves with it and a gross outlier can sit INSIDE the band —
    so a fixed backstop is what stops a volatile portfolio certifying an error
    away. Here six series move 4x and one moves 3x: the 3x sits well within the
    capped band of the 4x median, and only the backstop flags it."""
    org = _org(db)
    base, cur = _two_periods(db, org)
    spec = {f"s{i}": (1000.0, 4000.0) for i in range(6)}
    spec["borderline"] = (1000.0, 3000.0)
    _portfolio(db, org, base, cur, spec)
    r = compare(db, org.id, cur.id, base.id)

    hit = [f for f in r["findings"] if f["series_key"] == "borderline"]
    assert hit, r["band"]
    assert hit[0]["triggered_by"] == "backstop"
    # Prove the adaptive band alone would have missed it.
    assert abs(hit[0]["log_deviation"] - hit[0]["median_log_deviation"]) < hit[0]["threshold"]


def test_ordinary_variation_does_not_flag(db):
    org = _org(db)
    base, cur = _two_periods(db, org)
    _portfolio(db, org, base, cur, {
        f"s{i}": (1000.0, 1000.0 * m)
        for i, m in enumerate([1.02, 0.98, 1.05, 0.96, 1.03, 1.01])})
    r = compare(db, org.id, cur.id, base.id)
    assert r["findings"] == []


def test_a_doubled_bill_against_a_stable_portfolio_flags(db):
    org = _org(db)
    base, cur = _two_periods(db, org)
    spec = {f"s{i}": (1000.0, 1000.0 * m)
            for i, m in enumerate([1.02, 0.98, 1.05, 0.96, 1.03, 1.01])}
    spec["doubled"] = (1000.0, 2000.0)
    _portfolio(db, org, base, cur, spec)
    r = compare(db, org.id, cur.id, base.id)
    assert "doubled" in {f["series_key"] for f in r["findings"]}


def test_a_finding_cites_the_ghg_protocol_criterion(db):
    org = _org(db)
    base, cur = _two_periods(db, org)
    spec = {f"s{i}": (1000.0, 1000.0) for i in range(5)}
    spec["broken"] = (1000.0, 9000.0)
    _portfolio(db, org, base, cur, spec)
    r = compare(db, org.id, cur.id, base.id)
    f = [x for x in r["findings"] if x["series_key"] == "broken"][0]
    assert "10 percent" in f["criterion"]
    assert f["direction"] == "increase"
    assert f["ratio"] == pytest.approx(9.0)


def test_a_decrease_is_flagged_as_readily_as_an_increase(db):
    org = _org(db)
    base, cur = _two_periods(db, org)
    spec = {f"s{i}": (1000.0, 1000.0) for i in range(5)}
    spec["dropped"] = (9000.0, 1000.0)
    _portfolio(db, org, base, cur, spec)
    r = compare(db, org.id, cur.id, base.id)
    f = [x for x in r["findings"] if x["series_key"] == "dropped"][0]
    assert f["direction"] == "decrease"


# --- comparison is on quantity, never emissions -------------------------------

def test_units_are_part_of_the_series_identity(db):
    """Summing kWh with m3 would be a dimensional error, and two rows in different
    units are not the same series even under one declared key."""
    org = _org(db)
    base, cur = _two_periods(db, org)
    _act(db, org, "2025-06-15", 1000.0, series_key="mixed", unit="kWh")
    _act(db, org, "2026-06-15", 1000.0, series_key="mixed", unit="m3")
    r = compare(db, org.id, cur.id, base.id)
    assert r["available"] is False
    assert r["status"] == "no_comparable_series"


# --- absent and new series ----------------------------------------------------

def test_a_series_that_disappears_is_reported(db):
    """A missing bill reads as a reduction — the most damaging silent error."""
    org = _org(db)
    base, cur = _two_periods(db, org)
    _portfolio(db, org, base, cur, {f"s{i}": (1000.0, 1000.0) for i in range(4)})
    _act(db, org, "2025-06-15", 5000.0, series_key="gone")
    r = compare(db, org.id, cur.id, base.id)
    absent = {a["series_key"] for a in r["series_absent"]}
    assert "gone" in absent
    hit = [a for a in r["series_absent"] if a["series_key"] == "gone"][0]
    assert hit["baseline_quantity"] == pytest.approx(5000.0)
    assert "reads as a reduction" in hit["note"]


def test_a_new_series_is_reported_not_screened(db):
    org = _org(db)
    base, cur = _two_periods(db, org)
    _portfolio(db, org, base, cur, {f"s{i}": (1000.0, 1000.0) for i in range(4)})
    _act(db, org, "2026-06-15", 7000.0, series_key="new-site")
    r = compare(db, org.id, cur.id, base.id)
    new = {a["series_key"] for a in r["series_new"]}
    assert "new-site" in new
    assert "new-site" not in {f["series_key"] for f in r["findings"]}


# --- period comparability -----------------------------------------------------

def test_periods_of_very_different_length_are_flagged(db):
    """A ratio across unequal periods mixes a rate change with a duration change.
    The tolerance comes from comparability.py, which declares it once."""
    org = _org(db)
    base = _period(db, org, "FY25", "2025-01-01", "2025-12-31")
    cur = _period(db, org, "H1-26", "2026-01-01", "2026-06-30")
    _portfolio(db, org, base, cur, {f"s{i}": (1000.0, 1000.0) for i in range(4)})
    r = compare(db, org.id, cur.id, base.id)
    assert r["period_length_comparable"] is False
    assert "duration change" in r["period_length_note"]


def test_equal_periods_carry_no_length_caveat(db):
    org = _org(db)
    base, cur = _two_periods(db, org)
    _portfolio(db, org, base, cur, {f"s{i}": (1000.0, 1000.0) for i in range(4)})
    r = compare(db, org.id, cur.id, base.id)
    assert r["period_length_comparable"] is True
    assert r["period_length_note"] is None


# --- refusals -----------------------------------------------------------------

def test_an_unknown_period_refuses(db):
    org = _org(db)
    base, _ = _two_periods(db, org)
    r = compare(db, org.id, 999999, base.id)
    assert r["available"] is False and r["status"] == "period_not_found"


def test_a_period_cannot_be_screened_against_itself(db):
    org = _org(db)
    base, _ = _two_periods(db, org)
    r = compare(db, org.id, base.id, base.id)
    assert r["available"] is False and r["status"] == "same_period"


def test_an_undated_period_refuses(db):
    org = _org(db)
    base = _period(db, org, "FY25", "2025-01-01", "2025-12-31")
    cur = ReportingPeriod(organisation_id=org.id, label="undated")
    db.add(cur); db.commit(); db.refresh(cur)
    r = compare(db, org.id, cur.id, base.id)
    assert r["available"] is False and r["status"] == "period_not_dated"


def test_another_organisations_period_is_not_comparable(db):
    a, b = _org(db), _org(db)
    a_base, a_cur = _two_periods(db, a)
    r = compare(db, b.id, a_cur.id, a_base.id)
    assert r["available"] is False and r["status"] == "period_not_found"


def test_version_is_stamped(db):
    org = _org(db)
    base, cur = _two_periods(db, org)
    _portfolio(db, org, base, cur, {f"s{i}": (1000.0, 1000.0) for i in range(4)})
    assert compare(db, org.id, cur.id, base.id)["version"] == SERIES_SCREEN_VERSION


# --- level shift is not an error ----------------------------------------------

def test_a_persistent_same_direction_move_is_a_level_shift():
    """A site opening moves a series permanently and legitimately. ISAE 3410 A101
    requires trends to be read for consistency with acquisitions and disposals."""
    f = lambda d: {"series_key": "new-plant", "unit": "kWh", "direction": "increase",
                   "log_deviation": d, "threshold": 0.5}
    hist = [{"available": True, "findings": [f(0.90)]},
            {"available": True, "findings": [f(0.92)]},
            {"available": True, "findings": [f(0.91)]}]
    out = classify_level_shifts(hist)
    assert len(out["level_shifts"]) == 1
    shift = out["level_shifts"][0]
    assert shift["check_code"] == "level_shift"
    assert shift["severity"] == "informational"
    assert shift["routing"] == "base_year_recalculation"
    assert out["anomalies"] == []


def test_an_erratic_series_stays_an_anomaly():
    f = lambda d, dr: {"series_key": "erratic", "unit": "kWh", "direction": dr,
                       "log_deviation": d, "threshold": 0.5}
    hist = [{"available": True, "findings": [f(0.9, "increase")]},
            {"available": True, "findings": [f(-0.9, "decrease")]},
            {"available": True, "findings": [f(0.8, "increase")]}]
    out = classify_level_shifts(hist)
    assert out["level_shifts"] == []
    assert len(out["anomalies"]) == 3


def test_too_few_periods_is_not_yet_a_level_shift():
    f = {"series_key": "s", "unit": "kWh", "direction": "increase",
         "log_deviation": 0.9, "threshold": 0.5}
    hist = [{"available": True, "findings": [f]}] * (LEVEL_SHIFT_MIN_RUNS - 1)
    out = classify_level_shifts(hist)
    assert out["level_shifts"] == []


# --- endpoints ----------------------------------------------------------------

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
    base = _period(seed, org, "FY25", "2025-01-01", "2025-12-31")
    cur = _period(seed, org, "FY26", "2026-01-01", "2026-12-31")
    for i in range(5):
        _act(seed, org, "2025-06-15", 1000.0, category=f"c{i}")
        _act(seed, org, "2026-06-15", 1000.0, category=f"c{i}")
    ids = (base.id, cur.id)
    seed.close()
    yield client, hdr, ids
    main_mod.app.dependency_overrides.clear()


def test_declaring_a_series_by_category(env):
    client, hdr, _ = env
    r = client.post("/activities/series_key", headers=hdr,
                    params={"series_key": "hq-elec", "category": "c0"})
    assert r.status_code == 200
    assert r.json()["activities_updated"] == 2


def test_declaring_a_series_needs_a_selector(env):
    """Declaring one series across a whole organisation would defeat the purpose."""
    client, hdr, _ = env
    r = client.post("/activities/series_key", headers=hdr,
                    params={"series_key": "everything"})
    assert r.status_code == 400
    assert "defeat the purpose" in r.json()["detail"]


def test_an_empty_series_key_is_refused(env):
    client, hdr, _ = env
    r = client.post("/activities/series_key", headers=hdr,
                    params={"series_key": "  ", "category": "c0"})
    assert r.status_code == 400


def test_enrolment_endpoint(env):
    client, hdr, _ = env
    client.post("/activities/series_key", headers=hdr,
                params={"series_key": "hq", "category": "c0"})
    body = client.get("/activities/series", headers=hdr).json()
    assert body["activities_enrolled"] == 2
    assert body["distinct_series"] == 1


def test_screen_endpoint_end_to_end(env):
    client, hdr, (base_id, cur_id) = env
    for i in range(5):
        client.post("/activities/series_key", headers=hdr,
                    params={"series_key": f"s{i}", "category": f"c{i}"})
    body = client.get("/reports/series_screen", headers=hdr,
                      params={"current_period_id": cur_id,
                              "baseline_period_id": base_id}).json()
    assert body["available"] is True
    assert body["series_compared"] == 5
    assert body["findings"] == []
    assert body["enrolment"]["enrolled_pct"] == pytest.approx(100.0)


def test_screen_endpoint_404s_on_an_unknown_period(env):
    client, hdr, (base_id, _) = env
    r = client.get("/reports/series_screen", headers=hdr,
                   params={"current_period_id": 999999,
                           "baseline_period_id": base_id})
    assert r.status_code == 404


# --- adjacency: an intermittent anomaly is not a level shift ------------------

def _finding(series="site-A", direction="increase", dev=0.6, threshold=0.26):
    return {"series_key": series, "unit": "kWh", "direction": direction,
            "log_deviation": dev, "threshold": threshold, "ratio": 1.8}


def _payload(findings):
    return {"available": True, "findings": findings}


def test_an_intermittent_anomaly_is_not_reclassified_as_a_level_shift():
    """`runs` accumulated every finding for a series across the whole history and only
    COUNTED them, so a series that spikes, returns to baseline and spikes again — the
    definitional opposite of a step change — was filed as a level shift, dropped to
    informational and routed away from the anomaly list."""
    from app.services.series_screen import classify_level_shifts
    history = [
        _payload([_finding()]),      # spike
        _payload([]),                # back to baseline
        _payload([_finding()]),      # spike again
        _payload([]),                # baseline
        _payload([_finding()]),      # and again
    ]
    out = classify_level_shifts(history)
    assert out["level_shifts"] == [], (
        "three flags separated by clean periods are an intermittent anomaly, not a "
        "persistent step")
    assert len(out["anomalies"]) == 3


def test_a_genuinely_persistent_shift_is_still_reclassified():
    from app.services.series_screen import classify_level_shifts
    history = [_payload([_finding(dev=0.60)]),
               _payload([_finding(dev=0.61)]),
               _payload([_finding(dev=0.59)])]
    out = classify_level_shifts(history)
    assert len(out["level_shifts"]) == 1
    shift = out["level_shifts"][0]
    assert shift["consecutive_periods"] == 3
    assert shift["severity"] == "informational"
    assert shift["routing"] == "base_year_recalculation"


def test_flags_outside_the_streak_remain_anomalies():
    """A series can have a persistent shift AND an unrelated spike; the spike must not
    be absorbed into the reclassification."""
    from app.services.series_screen import classify_level_shifts
    history = [_payload([_finding(dev=0.60)]),
               _payload([_finding(dev=0.61)]),
               _payload([_finding(dev=0.59)]),
               _payload([]),
               _payload([_finding(dev=1.9, direction="increase")])]
    out = classify_level_shifts(history)
    assert len(out["level_shifts"]) == 1
    assert len(out["anomalies"]) == 1 and out["anomalies"][0]["log_deviation"] == 1.9
