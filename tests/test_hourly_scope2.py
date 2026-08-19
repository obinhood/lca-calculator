"""Hourly Scope 2 temporal matching.

The property this module exists to enforce: SURPLUS IN ONE HOUR NEVER OFFSETS A
DEFICIT IN ANOTHER. Annual market-based accounting nets a whole year, so 100 MWh of
June solar cancels 100 MWh of December coal and the figure can read zero for an
operation that ran on fossil power every winter night. Hourly matching forbids that,
and every other rule here follows from it.

The three refusals are equally load-bearing: an unmetered hour is missing rather
than zero-load, unmatched load is never priced at the grid average when no residual
rate exists, and a certificate never crosses a deliverability boundary on its own
authority.
"""
import io

import pytest

from app.models import (
    DeliverabilityLink, GranularCertificate, HourlyGridIntensity, HourlyLoad,
    Organisation, ReportingPeriod,
)
from app.services.hourly_scope2 import (
    HOURLY_SCOPE2_VERSION, _hours_between, _parse_hour, match,
)


# --- fixtures ---------------------------------------------------------------

_SEQ = [0]


def _org(db):
    _SEQ[0] += 1
    o = Organisation(name=f"HourlyOrg{_SEQ[0]}")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _period(db, org, start="2027-01-01", end="2027-01-01"):
    p = ReportingPeriod(organisation_id=org.id, label="D1",
                        start_date=start, end_date=end)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _load(db, org, hour, kwh, region="GB", point="default"):
    db.add(HourlyLoad(organisation_id=org.id, metering_point=point,
                      hour_start=hour, kwh=kwh, grid_region=region))
    db.commit()


def _cert(db, org, hour, kwh, region="GB", ref=None, intensity=0.0, span_hours=1):
    _SEQ[0] += 1
    start = _parse_hour(hour)
    end = start.replace(microsecond=0)
    from datetime import timedelta
    end = start + timedelta(hours=span_hours)
    c = GranularCertificate(
        organisation_id=org.id, issuer="EnergyTagCo",
        certificate_ref=ref or f"GC-{_SEQ[0]}",
        production_start=hour, production_end=end.strftime("%Y-%m-%dT%H:%M:%S"),
        kwh=kwh, technology="wind", grid_region=region, kg_co2e_per_kwh=intensity)
    db.add(c); db.commit(); db.refresh(c)
    return c


def _intensity(db, hour, avg=0.4, residual=0.5, region="GB"):
    db.add(HourlyGridIntensity(grid_region=region, hour_start=hour,
                               kg_co2e_per_kwh_average=avg,
                               kg_co2e_per_kwh_residual=residual, source="TEST"))
    db.commit()


def _hours(n=24, day="2027-01-01"):
    return [f"{day}T{h:02d}:00:00" for h in range(n)]


def _full_day(db, org, load_kwh=100.0, avg=0.4, residual=0.5):
    """A metered 24-hour day with intensity for every hour and no certificates."""
    for h in _hours():
        _load(db, org, h, load_kwh)
        _intensity(db, h, avg=avg, residual=residual)


# --- the central property ---------------------------------------------------

def test_surplus_in_one_hour_never_offsets_a_deficit_in_another(db):
    """THE reform, in one test. The same annual certificate volume that would net to
    100% under annual market-based accounting scores 50% hourly, because half of it
    was produced in hours with no load to match."""
    org = _org(db)
    p = _period(db, org)
    hrs = _hours()
    for h in hrs:
        _load(db, org, h, 100.0)
        _intensity(db, h)
    # 2400 kWh of certificates — exactly the day's consumption — but all of it
    # produced in the first 12 hours.
    for h in hrs[:12]:
        _cert(db, org, h, 200.0)

    r = match(db, org.id, p.id)
    assert r["available"] is True
    assert r["cfe"]["total_load_kwh"] == pytest.approx(2400.0)
    # Annual netting would call this 100%. Hourly matching calls it 50%.
    assert r["cfe"]["matched_kwh"] == pytest.approx(1200.0)
    assert r["cfe"]["cfe_score_pct"] == pytest.approx(50.0)
    assert r["cfe"]["hours_fully_matched"] == 12
    # And it names the quantity the difference rests on.
    assert r["certificates"]["surplus_kwh_not_carried_forward"] == pytest.approx(1200.0)


def test_perfectly_matched_day_scores_one_hundred(db):
    org = _org(db)
    p = _period(db, org)
    for h in _hours():
        _load(db, org, h, 100.0)
        _intensity(db, h)
        _cert(db, org, h, 100.0)
    r = match(db, org.id, p.id)
    assert r["cfe"]["cfe_score_pct"] == pytest.approx(100.0)
    assert r["cfe"]["unmatched_kwh"] == pytest.approx(0.0)
    assert r["certificates"]["surplus_kwh_not_carried_forward"] == pytest.approx(0.0)
    assert r["emissions"]["hourly_market_based_kg_co2e"] == pytest.approx(0.0)


def test_no_certificates_prices_every_hour_at_the_residual(db):
    org = _org(db)
    p = _period(db, org)
    _full_day(db, org, load_kwh=100.0, avg=0.4, residual=0.5)
    r = match(db, org.id, p.id)
    assert r["cfe"]["cfe_score_pct"] == pytest.approx(0.0)
    # 2400 kWh at the RESIDUAL rate, not the 0.4 average.
    assert r["emissions"]["hourly_market_based_kg_co2e"] == pytest.approx(1200.0)


def test_surplus_is_reported_even_when_the_score_is_perfect(db):
    org = _org(db)
    p = _period(db, org)
    for h in _hours(3):
        _load(db, org, h, 50.0)
        _intensity(db, h)
        _cert(db, org, h, 80.0)
    r = match(db, org.id, p.id)
    assert r["cfe"]["cfe_score_pct"] == pytest.approx(100.0)
    assert r["certificates"]["surplus_kwh_not_carried_forward"] == pytest.approx(90.0)


# --- an unmetered hour is missing, never zero -------------------------------

def test_partial_hour_coverage_is_flagged_not_silently_scored(db):
    """An hour with no meter reading scored as 'load 0, matched 0' would count as
    perfectly matched and drag every CFE score toward 100%."""
    org = _org(db)
    p = _period(db, org)
    for h in _hours()[:6]:
        _load(db, org, h, 100.0)
        _intensity(db, h)
        _cert(db, org, h, 100.0)
    r = match(db, org.id, p.id)
    cov = r["hour_coverage"]
    assert cov["hours_in_period"] == 24
    assert cov["hours_with_load_data"] == 6
    assert cov["complete"] is False
    assert "MISSING" in cov["warning"]
    # The score is still reported, but explicitly over the metered hours only.
    assert r["cfe"]["cfe_score_pct"] == pytest.approx(100.0)
    assert "metered hours" in cov["warning"]


def test_complete_coverage_carries_no_warning(db):
    org = _org(db)
    p = _period(db, org)
    _full_day(db, org)
    cov = match(db, org.id, p.id)["hour_coverage"]
    assert cov["complete"] is True
    assert cov["warning"] is None


def test_no_hourly_load_refuses_rather_than_deriving_from_annual(db):
    org = _org(db)
    p = _period(db, org)
    r = match(db, org.id, p.id)
    assert r["available"] is False
    assert "cannot be derived from annual consumption" in r["reason"]


# --- unmatched load is never priced at the average --------------------------

def test_missing_residual_leaves_the_hour_unpriced_not_averaged(db):
    """Residual is always >= average, so substituting the average would UNDERSTATE
    — the failure direction this platform refuses above all others."""
    org = _org(db)
    p = _period(db, org)
    for h in _hours(4):
        _load(db, org, h, 100.0)
    # Average published, residual withheld.
    for h in _hours(4):
        db.add(HourlyGridIntensity(grid_region="GB", hour_start=h,
                                   kg_co2e_per_kwh_average=0.4,
                                   kg_co2e_per_kwh_residual=None, source="TEST"))
    db.commit()

    r = match(db, org.id, p.id)
    em = r["emissions"]
    assert em["priced_completely"] is False
    assert em["hourly_market_based_kg_co2e"] is None   # withheld, not 160.0
    assert em["unpriced_kwh"] == pytest.approx(400.0)
    assert em["unpriced_hours"] == 4
    assert "not substituted" in em["unpriced_reason"] or "NOT substituted" in em["unpriced_reason"]
    assert any("no residual rate" in g for g in em["residual_gaps"])


def test_no_intensity_row_at_all_is_distinguished_from_a_missing_residual(db):
    org = _org(db)
    p = _period(db, org)
    for h in _hours(2):
        _load(db, org, h, 100.0)
    r = match(db, org.id, p.id)
    assert any("no intensity row" in g for g in r["emissions"]["residual_gaps"])


def test_fully_matched_hours_need_no_residual(db):
    """Nothing is unmatched, so no residual rate is required and the total is priced."""
    org = _org(db)
    p = _period(db, org)
    for h in _hours(3):
        _load(db, org, h, 100.0)
        _cert(db, org, h, 100.0)
    r = match(db, org.id, p.id)
    assert r["emissions"]["priced_completely"] is True
    assert r["emissions"]["hourly_market_based_kg_co2e"] == pytest.approx(0.0)


def test_certificate_intensity_is_respected_not_assumed_zero(db):
    """A certificate is an attribute claim; hard-coding zero would quietly convert
    it into an emissions claim."""
    org = _org(db)
    p = _period(db, org)
    for h in _hours(2):
        _load(db, org, h, 100.0)
        _intensity(db, h)
        _cert(db, org, h, 100.0, intensity=0.02)
    r = match(db, org.id, p.id)
    assert r["emissions"]["matched_component_kg_co2e"] == pytest.approx(4.0)
    assert r["emissions"]["hourly_market_based_kg_co2e"] == pytest.approx(4.0)


# --- deliverability ---------------------------------------------------------

def test_certificate_from_another_region_is_rejected_by_default(db):
    org = _org(db)
    p = _period(db, org)
    for h in _hours(3):
        _load(db, org, h, 100.0, region="GB")
        _intensity(db, h, region="GB")
        _cert(db, org, h, 100.0, region="FR")
    r = match(db, org.id, p.id)
    assert r["cfe"]["cfe_score_pct"] == pytest.approx(0.0)
    assert r["deliverability"]["rejected_undeliverable_kwh"] == pytest.approx(300.0)


def test_a_declared_link_makes_it_deliverable(db):
    org = _org(db)
    p = _period(db, org)
    db.add(DeliverabilityLink(organisation_id=org.id, from_region="FR", to_region="GB",
                              basis="interconnector", rationale="IFA-2"))
    db.commit()
    for h in _hours(3):
        _load(db, org, h, 100.0, region="GB")
        _intensity(db, h, region="GB")
        _cert(db, org, h, 100.0, region="FR")
    r = match(db, org.id, p.id)
    assert r["cfe"]["cfe_score_pct"] == pytest.approx(100.0)
    assert r["deliverability"]["rejected_undeliverable_kwh"] == pytest.approx(0.0)
    assert r["deliverability"]["cross_region_matched_kwh"] == pytest.approx(300.0)
    assert r["deliverability"]["declared_links"] == {"GB": ["FR"]}


def test_links_are_directional(db):
    """A link FR->GB must not let a GB certificate serve French load."""
    org = _org(db)
    p = _period(db, org)
    db.add(DeliverabilityLink(organisation_id=org.id, from_region="FR", to_region="GB",
                              basis="interconnector"))
    db.commit()
    for h in _hours(2):
        _load(db, org, h, 100.0, region="FR")
        _intensity(db, h, region="FR")
        _cert(db, org, h, 100.0, region="GB")
    r = match(db, org.id, p.id)
    assert r["cfe"]["cfe_score_pct"] == pytest.approx(0.0)
    assert r["deliverability"]["rejected_undeliverable_kwh"] == pytest.approx(200.0)


def test_region_matching_is_case_and_whitespace_insensitive(db):
    org = _org(db)
    p = _period(db, org)
    for h in _hours(2):
        _load(db, org, h, 100.0, region=" gb ")
        _intensity(db, h, region="GB")
        _cert(db, org, h, 100.0, region="Gb")
    assert match(db, org.id, p.id)["cfe"]["cfe_score_pct"] == pytest.approx(100.0)


# --- double counting --------------------------------------------------------

def test_certificate_retired_against_another_period_is_excluded(db):
    org = _org(db)
    p1 = _period(db, org, "2027-01-01", "2027-01-01")
    p2 = ReportingPeriod(organisation_id=org.id, label="D2",
                         start_date="2027-01-02", end_date="2027-01-02")
    db.add(p2); db.commit(); db.refresh(p2)

    for h in _hours(3):
        _load(db, org, h, 100.0)
        _intensity(db, h)
    c = _cert(db, org, _hours(3)[0], 100.0)
    c.retired_for_period_id = p2.id
    db.commit()

    r = match(db, org.id, p1.id)
    assert r["cfe"]["matched_kwh"] == pytest.approx(0.0)
    assert r["certificates"]["excluded"]["retired_for_another_period"] == 1


def test_certificate_retired_against_this_period_counts(db):
    org = _org(db)
    p = _period(db, org)
    for h in _hours(3):
        _load(db, org, h, 100.0)
        _intensity(db, h)
    c = _cert(db, org, _hours(3)[0], 100.0)
    c.retired_for_period_id = p.id
    db.commit()
    r = match(db, org.id, p.id)
    assert r["cfe"]["matched_kwh"] == pytest.approx(100.0)
    assert r["certificates"]["excluded"]["retired_for_another_period"] == 0


def test_unretired_certificates_are_usable(db):
    """Retirement pins a certificate to one period; an unretired one is not yet
    claimed anywhere and may be matched."""
    org = _org(db)
    p = _period(db, org)
    _load(db, org, _hours(1)[0], 100.0)
    _intensity(db, _hours(1)[0])
    _cert(db, org, _hours(1)[0], 100.0)
    assert match(db, org.id, p.id)["cfe"]["matched_kwh"] == pytest.approx(100.0)


# --- window handling --------------------------------------------------------

def test_multi_hour_certificate_is_apportioned_and_disclosed(db):
    org = _org(db)
    p = _period(db, org)
    for h in _hours(4):
        _load(db, org, h, 100.0)
        _intensity(db, h)
    _cert(db, org, _hours(4)[0], 400.0, span_hours=4)
    r = match(db, org.id, p.id)
    # 100 kWh in each of four hours, not 400 in the first.
    assert r["cfe"]["matched_kwh"] == pytest.approx(400.0)
    assert r["certificates"]["excluded"]["apportioned_multi_hour"] == 1


def test_a_lumped_certificate_cannot_cover_a_later_hour(db):
    """The apportionment must not let one hour's production serve the whole day."""
    org = _org(db)
    p = _period(db, org)
    for h in _hours(4):
        _load(db, org, h, 100.0)
        _intensity(db, h)
    _cert(db, org, _hours(4)[0], 400.0, span_hours=1)   # all in hour 0
    r = match(db, org.id, p.id)
    assert r["cfe"]["matched_kwh"] == pytest.approx(100.0)
    assert r["certificates"]["surplus_kwh_not_carried_forward"] == pytest.approx(300.0)


def test_certificate_outside_the_period_is_excluded(db):
    org = _org(db)
    p = _period(db, org, "2027-01-01", "2027-01-01")
    _load(db, org, "2027-01-01T00:00:00", 100.0)
    _intensity(db, "2027-01-01T00:00:00")
    _cert(db, org, "2027-06-01T00:00:00", 100.0)
    r = match(db, org.id, p.id)
    assert r["cfe"]["matched_kwh"] == pytest.approx(0.0)
    assert r["certificates"]["excluded"]["outside_period"] == 1


def test_period_end_date_is_inclusive_of_its_whole_day(db):
    org = _org(db)
    p = _period(db, org, "2027-01-01", "2027-01-02")
    _load(db, org, "2027-01-02T23:00:00", 100.0)
    _intensity(db, "2027-01-02T23:00:00")
    _cert(db, org, "2027-01-02T23:00:00", 100.0)
    r = match(db, org.id, p.id)
    assert r["hour_coverage"]["hours_in_period"] == 48
    assert r["cfe"]["matched_kwh"] == pytest.approx(100.0)


def test_hours_between_is_end_exclusive():
    assert _hours_between(_parse_hour("2027-01-01T00:00:00"),
                          _parse_hour("2027-01-01T01:00:00")) == ["2027-01-01T00:00:00"]
    assert len(_hours_between(_parse_hour("2027-01-01T00:00:00"),
                              _parse_hour("2027-01-01T03:00:00"))) == 3
    assert _hours_between(_parse_hour("2027-01-01T05:00:00"),
                          _parse_hour("2027-01-01T05:00:00")) == []


@pytest.mark.parametrize("value,ok", [
    ("2027-01-01T13:00:00", True), ("2027-01-01T13:00", True),
    ("2027-01-01 13:00:00", True), ("2027-01-01T13:00:00Z", True),
    ("2027-01-01", True),
    (None, False), ("", False), ("01/01/2027", False), ("nonsense", False), (7, False),
])
def test_hour_parsing_never_guesses(value, ok):
    assert (_parse_hour(value) is not None) is ok


def test_malformed_load_rows_are_counted_not_treated_as_zero(db):
    org = _org(db)
    p = _period(db, org)
    _load(db, org, _hours(1)[0], 100.0)
    _intensity(db, _hours(1)[0])
    db.add(HourlyLoad(organisation_id=org.id, metering_point="bad",
                      hour_start="not-a-date", kwh=50.0, grid_region="GB"))
    db.commit()
    r = match(db, org.id, p.id)
    assert r["hour_coverage"]["malformed_load_rows"] == 1
    assert r["cfe"]["total_load_kwh"] == pytest.approx(100.0)


# --- refusals ---------------------------------------------------------------

def test_unknown_period_refuses(db):
    org = _org(db)
    r = match(db, org.id, 999999)
    assert r["available"] is False and "not found" in r["reason"]


def test_period_without_dates_refuses(db):
    org = _org(db)
    p = ReportingPeriod(organisation_id=org.id, label="undated")
    db.add(p); db.commit(); db.refresh(p)
    r = match(db, org.id, p.id)
    assert r["available"] is False
    assert "cannot be scoped" in r["reason"]


def test_another_organisations_period_is_not_matchable(db):
    a, b = _org(db), _org(db)
    p = _period(db, b)
    r = match(db, a.id, p.id)
    assert r["available"] is False


def test_version_is_stamped(db):
    org = _org(db)
    p = _period(db, org)
    _full_day(db, org)
    assert match(db, org.id, p.id)["version"] == HOURLY_SCOPE2_VERSION


# --- parallel method: the annual figures must not move ----------------------

def test_hourly_data_does_not_touch_the_annual_calculation(db):
    """Hourly Scope 2 is a PARALLEL method. Adding certificates and load must leave
    compute_co2e's location and market totals bit-identical."""
    from app.models import ActivityRecord, EmissionFactor
    from app.services.calc import compute_co2e

    org = _org(db)
    p = _period(db, org, "2027-01-01", "2027-12-31")
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2027,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.2)
    db.add(f); db.commit(); db.refresh(f)
    db.add(ActivityRecord(organisation_id=org.id, date="2027-03-01",
                          category="electricity", subcategory="", description="",
                          quantity=5000.0, unit="kWh", geo="GB", factor_id=f.id,
                          mapping_basis="exact"))
    db.commit()
    before = compute_co2e(db, org.id, reporting_period_id=p.id)
    before_loc, before_mkt = before.total_co2e, before.total_co2e_market

    for h in _hours():
        _load(db, org, h, 100.0)
        _intensity(db, h)
        _cert(db, org, h, 100.0)

    after = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert after.total_co2e == before_loc
    assert after.total_co2e_market == before_mkt
    # And the hourly method reports its own, separate figure.
    assert match(db, org.id, p.id)["cfe"]["cfe_score_pct"] == pytest.approx(100.0)


# --- endpoints --------------------------------------------------------------

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
    key = client.post("/organisations", params={"name": "H"}).json()["api_key"]
    hdr = {"X-API-Key": key}
    period_id = client.post("/reporting_periods", headers=hdr, params={
        "label": "D1", "start_date": "2027-01-01", "end_date": "2027-01-01"}).json()["id"]
    yield client, hdr, period_id
    main_mod.app.dependency_overrides.clear()


def test_certificate_registration_rejects_a_duplicate(env):
    client, hdr, _ = env
    params = {"issuer": "EnergyTagCo", "certificate_ref": "GC-1",
              "production_start": "2027-01-01T00:00:00",
              "production_end": "2027-01-01T01:00:00",
              "kwh": 100.0, "grid_region": "GB"}
    assert client.post("/hourly/certificates", headers=hdr, params=params).status_code == 200
    r = client.post("/hourly/certificates", headers=hdr, params=params)
    assert r.status_code == 409
    assert "already registered" in r.json()["detail"]


def test_certificate_cannot_be_retired_against_two_periods(env):
    client, hdr, period_id = env
    cid = client.post("/hourly/certificates", headers=hdr, params={
        "issuer": "E", "certificate_ref": "GC-2",
        "production_start": "2027-01-01T00:00:00",
        "production_end": "2027-01-01T01:00:00",
        "kwh": 100.0, "grid_region": "GB"}).json()["id"]
    other = client.post("/reporting_periods", headers=hdr, params={
        "label": "D2", "start_date": "2027-01-02", "end_date": "2027-01-02"}).json()["id"]

    assert client.post(f"/hourly/certificates/{cid}/retire", headers=hdr,
                       params={"reporting_period_id": period_id}).status_code == 200
    r = client.post(f"/hourly/certificates/{cid}/retire", headers=hdr,
                    params={"reporting_period_id": other})
    assert r.status_code == 409
    assert "double count" in r.json()["detail"]
    # Retiring against the SAME period again is idempotent, not an error.
    assert client.post(f"/hourly/certificates/{cid}/retire", headers=hdr,
                       params={"reporting_period_id": period_id}).status_code == 200


def test_load_upload_rejects_bad_rows_without_storing_zeros(env):
    client, hdr, _ = env
    csv = ("hour_start,kwh,grid_region\n"
           "2027-01-01T00:00:00,100,GB\n"
           "not-a-date,50,GB\n"
           "2027-01-01T01:00:00,abc,GB\n"
           "2027-01-01T02:00:00,-5,GB\n"
           "2027-01-01T03:00:00,80,\n")
    r = client.post("/hourly/loads", headers=hdr,
                    files={"file": ("load.csv", csv, "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 4
    reasons = {x["reason"] for x in body["rejections"]}
    assert "unparseable hour_start" in reasons
    assert "non-numeric kwh" in reasons
    assert "missing grid_region" in reasons
    assert "NOT stored as zero-load hours" in body["note"]


def test_load_upload_requires_the_expected_columns(env):
    client, hdr, _ = env
    r = client.post("/hourly/loads", headers=hdr,
                    files={"file": ("l.csv", "a,b\n1,2\n", "text/csv")})
    assert r.status_code == 400
    assert "missing required column" in r.json()["detail"]


def test_residual_below_average_is_refused(env):
    client, hdr, _ = env
    r = client.post("/reference/hourly_grid_intensity", headers=hdr, params={
        "grid_region": "GB", "hour_start": "2027-01-01T00:00:00",
        "kg_co2e_per_kwh_average": 0.5, "kg_co2e_per_kwh_residual": 0.3,
        "source": "TEST"})
    assert r.status_code == 400
    assert "arithmetically impossible" in r.json()["detail"]


def test_same_region_deliverability_link_is_refused(env):
    client, hdr, _ = env
    r = client.post("/hourly/deliverability_links", headers=hdr, params={
        "from_region": "GB", "to_region": "gb", "basis": "interconnector"})
    assert r.status_code == 400
    assert "implicitly" in r.json()["detail"]


def test_report_endpoint_returns_the_cfe_score(env):
    client, hdr, period_id = env
    csv = "hour_start,kwh,grid_region\n" + "".join(
        f"2027-01-01T{h:02d}:00:00,100,GB\n" for h in range(24))
    client.post("/hourly/loads", headers=hdr, files={"file": ("l.csv", csv, "text/csv")})
    for h in range(24):
        client.post("/hourly/certificates", headers=hdr, params={
            "issuer": "E", "certificate_ref": f"C{h}",
            "production_start": f"2027-01-01T{h:02d}:00:00",
            "production_end": f"2027-01-01T{h + 1:02d}:00:00" if h < 23
                              else "2027-01-02T00:00:00",
            "kwh": 100.0, "grid_region": "GB"})
    body = client.get("/reports/hourly_scope2", headers=hdr,
                      params={"reporting_period_id": period_id}).json()
    assert body["cfe"]["cfe_score_pct"] == pytest.approx(100.0)
    assert body["hour_coverage"]["complete"] is True
    assert "hours" not in body            # omitted unless asked for


def test_report_endpoint_can_include_the_hourly_series(env):
    client, hdr, period_id = env
    csv = "hour_start,kwh,grid_region\n2027-01-01T00:00:00,100,GB\n"
    client.post("/hourly/loads", headers=hdr, files={"file": ("l.csv", csv, "text/csv")})
    body = client.get("/reports/hourly_scope2", headers=hdr,
                      params={"reporting_period_id": period_id,
                              "include_hours": True}).json()
    assert len(body["hours"]) == 1
    assert body["hours"][0]["hour"] == "2027-01-01T00:00:00"
