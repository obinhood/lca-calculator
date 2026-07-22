"""Temporal straddle proration: a consumption window spanning a period boundary.

An ActivityRecord carries a single `date`, so a supply invoice covering 15 Dec - 15 Jan
was attributed WHOLLY to whichever fiscal year that one date fell in. Declaring the window
lets a period-scoped run prorate the quantity by the overlapping share, so the emissions
land in the year they occurred.
"""
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod

from app.models import (
    Organisation, ActivityRecord, EmissionFactor, EmissionLineItem, ReportingPeriod,
)
from app.services.calc import compute_co2e, coverage_overlap, activities_in_scope
from tests.scope3_util import make_period


def _org(db, name="Co"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, value=1.0):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2025,
                       category="gas", subcategory="", unit="kWh", gwp_set="AR6",
                       value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id, qty=1000.0, date="2025-01-05",
         coverage_start=None, coverage_end=None):
    a = ActivityRecord(organisation_id=org_id, date=date, category="gas", subcategory="",
                       description="", quantity=qty, unit="kWh", geo="GB",
                       factor_id=factor_id, coverage_start=coverage_start,
                       coverage_end=coverage_end)
    db.add(a); db.commit(); db.refresh(a)
    return a


class _A:
    def __init__(self, cs, ce):
        self.coverage_start, self.coverage_end = cs, ce


def _d(s):
    from app.services.calc import _parse_iso_date
    return _parse_iso_date(s)


# --- The overlap arithmetic ---------------------------------------------------------

def test_no_window_or_no_period_is_a_no_op():
    """The whole backward-compatibility mechanism: without a declared window nothing
    changes, because the platform cannot infer a window it was not told."""
    assert coverage_overlap(_A(None, None), _d("2025-01-01"), _d("2025-12-31")) == (1.0, None)
    assert coverage_overlap(_A("2025-01-01", "2025-01-31"), None, None) == (1.0, None)
    # a window wholly inside the period needs no proration either
    assert coverage_overlap(_A("2025-03-01", "2025-03-31"),
                            _d("2025-01-01"), _d("2025-12-31")) == (1.0, None)
    # malformed (end before start) is never guessed at
    assert coverage_overlap(_A("2025-03-31", "2025-03-01"),
                            _d("2025-01-01"), _d("2025-12-31")) == (1.0, None)


def test_a_december_january_invoice_splits_by_inclusive_calendar_days():
    """15 Dec - 15 Jan is 32 inclusive days; 17 of them fall in the year ending 31 Dec."""
    frac, ev = coverage_overlap(_A("2024-12-15", "2025-01-15"),
                                _d("2024-01-01"), _d("2024-12-31"))
    assert ev["coverage_days"] == 32 and ev["days_in_period"] == 17
    assert frac == pytest.approx(17 / 32)
    assert ev["proration_basis"] == "inclusive_calendar_days"
    # ...and the following year takes the rest, so the two shares are exhaustive.
    frac2, _ = coverage_overlap(_A("2024-12-15", "2025-01-15"),
                                _d("2025-01-01"), _d("2025-12-31"))
    assert frac + frac2 == pytest.approx(1.0)


# --- End to end ---------------------------------------------------------------------

def test_a_straddling_invoice_is_prorated_into_the_period(db):
    org = _org(db)
    p = make_period(db, org.id, start="2024-01-01", end="2024-12-31")
    # 1000 kWh @ 1.0 over 15 Dec 2024 - 15 Jan 2025; date sits in the NEXT year.
    _act(db, org.id, _factor(db).id, qty=1000.0, date="2025-01-05",
         coverage_start="2024-12-15", coverage_end="2025-01-15")
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert run.total_co2e == pytest.approx(1000.0 * 17 / 32)
    d = json.loads(db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).first().details)
    assert d["quantity_as_recorded"] == pytest.approx(1000.0)
    assert d["quantity"] == pytest.approx(1000.0 * 17 / 32)
    assert d["temporal_proration"]["days_in_period"] == 17


def test_the_two_adjacent_years_together_account_for_the_whole_invoice(db):
    """No double count and no gap — the point of prorating rather than picking a year."""
    org = _org(db)
    f = _factor(db).id
    p24 = make_period(db, org.id, label="FY24", start="2024-01-01", end="2024-12-31")
    p25 = make_period(db, org.id, label="FY25", start="2025-01-01", end="2025-12-31")
    _act(db, org.id, f, qty=1000.0, date="2025-01-05",
         coverage_start="2024-12-15", coverage_end="2025-01-15")
    r24 = compute_co2e(db, org.id, reporting_period_id=p24.id)
    r25 = compute_co2e(db, org.id, reporting_period_id=p25.id)
    assert r24.total_co2e + r25.total_co2e == pytest.approx(1000.0)


def test_a_window_puts_a_record_in_scope_even_when_its_date_is_outside(db):
    """Membership follows the WINDOW when one is declared: the December consumption
    belongs to FY24 even though the invoice is dated in January."""
    org = _org(db)
    p = make_period(db, org.id, start="2024-01-01", end="2024-12-31")
    a = _act(db, org.id, _factor(db).id, date="2025-01-05",
             coverage_start="2024-12-15", coverage_end="2025-01-15")
    assert a.id in {x.id for x in activities_in_scope(db, org.id, p)}
    # ...and a window entirely outside the period is still excluded.
    b = _act(db, org.id, _factor(db).id, date="2026-03-01",
             coverage_start="2026-02-01", coverage_end="2026-02-28")
    assert b.id not in {x.id for x in activities_in_scope(db, org.id, p)}


def test_proration_is_applied_consistently_to_every_derived_quantity(db):
    """The emissions figure, the biogenic pool and the Scope 2 kWh must share ONE basis —
    a line whose energy is full-period but whose emissions are prorated contradicts itself."""
    org = _org(db)
    p = make_period(db, org.id, start="2024-01-01", end="2024-12-31")
    f = EmissionFactor(source="T", version="1", geography="GB", year=2025,
                       category="waste", subcategory="", unit="kg", gwp_set="AR6",
                       value=1.0, kg_co2_biogenic=2.0)
    db.add(f); db.commit(); db.refresh(f)
    a = ActivityRecord(organisation_id=org.id, date="2025-01-05", category="waste",
                       subcategory="", description="", quantity=1000.0, unit="kg",
                       geo="GB", factor_id=f.id, coverage_start="2024-12-15",
                       coverage_end="2025-01-15")
    db.add(a); db.commit()
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    share = 17 / 32
    assert run.total_co2e == pytest.approx(1000.0 * share)
    assert run.total_biogenic_co2e == pytest.approx(2000.0 * share)


def test_the_window_is_part_of_the_activity_fingerprint(db):
    """Declaring a window changes the RESULT, so an existing run must read as STALE
    rather than silently changing underneath a filed figure."""
    from app.services.calc import activities_fingerprint, FINGERPRINT_VERSION
    org = _org(db)
    a = _act(db, org.id, _factor(db).id)
    before = activities_fingerprint([a])
    assert before.startswith(f"{FINGERPRINT_VERSION}:")
    a.coverage_start, a.coverage_end = "2024-12-15", "2025-01-15"
    db.commit()
    assert activities_fingerprint([a]) != before


def test_an_org_wide_run_never_prorates(db):
    """With no reporting period there is no boundary to straddle."""
    org = _org(db)
    _act(db, org.id, _factor(db).id, qty=1000.0,
         coverage_start="2024-12-15", coverage_end="2025-01-15")
    run = compute_co2e(db, org.id)
    assert run.total_co2e == pytest.approx(1000.0)


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
    key = c.post("/organisations", params={"name": "A"}).json()["api_key"]
    yield c, {"X-API-Key": key}, Session
    main_mod.app.dependency_overrides.clear()


def test_the_endpoint_validates_and_requires_a_recompute(client):
    c, hdr, _ = client
    bad = c.post("/activities/coverage_window",
                 params={"coverage_start": "2025-01-31", "coverage_end": "2025-01-01"},
                 headers=hdr)
    assert bad.status_code == 400 and "precede" in bad.json()["detail"]
    assert c.post("/activities/coverage_window",
                  params={"coverage_start": "nonsense", "coverage_end": "2025-01-01"},
                  headers=hdr).status_code == 400
    ok = c.post("/activities/coverage_window",
                params={"coverage_start": "2024-12-15", "coverage_end": "2025-01-15"},
                headers=hdr)
    assert ok.status_code == 200 and "STALE" in ok.json()["note"]


# --- Adversarial-review regressions --------------------------------------------------

def _period(db, org_id, label, start, end):
    p = ReportingPeriod(organisation_id=org_id, label=label,
                        start_date=start, end_date=end, frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    return p


def test_an_open_bounded_period_does_not_double_count(db):
    """Regression (review, HIGH): membership admitted a windowed record using only the
    period bound that EXISTS, but the overlap bailed to a 1.0 fraction whenever EITHER
    bound was NULL — so 100% landed in the open-bounded period ON TOP of the adjacent
    period's prorated share. A 1000 kg record was booked as 1468.75 kg."""
    org = _org(db)
    f = _factor(db).id
    fy24 = _period(db, org.id, "FY24", None, "2024-12-31")          # open START
    fy25 = _period(db, org.id, "FY25", "2025-01-01", "2025-12-31")
    _act(db, org.id, f, qty=1000.0, date="2025-01-05",
         coverage_start="2024-12-15", coverage_end="2025-01-15")
    r24 = compute_co2e(db, org.id, reporting_period_id=fy24.id)
    r25 = compute_co2e(db, org.id, reporting_period_id=fy25.id)
    assert r24.total_co2e == pytest.approx(1000.0 * 17 / 32)
    assert r24.total_co2e + r25.total_co2e == pytest.approx(1000.0)


def test_an_open_ended_period_does_not_double_count(db):
    """The mirror case: the LATER period left open-ended."""
    org = _org(db)
    f = _factor(db).id
    fy23 = _period(db, org.id, "FY23", "2023-01-01", "2023-12-31")
    fy24 = _period(db, org.id, "FY24", "2024-01-01", None)          # open END
    _act(db, org.id, f, qty=1000.0, date="2024-01-05",
         coverage_start="2023-12-15", coverage_end="2024-01-15")
    r23 = compute_co2e(db, org.id, reporting_period_id=fy23.id)
    r24 = compute_co2e(db, org.id, reporting_period_id=fy24.id)
    assert r23.total_co2e + r24.total_co2e == pytest.approx(1000.0)


def test_energy_is_on_the_same_prorated_basis_as_its_emissions(db):
    """Regression (review, HIGH): _energy_kwh read the LIVE ActivityRecord.quantity, so
    the energy figure in SECR / ESOS / ESRS E1-5 / GRI 302 sat on the GROSS basis beside
    prorated emissions — a wrong implied intensity, and 2000 kWh reported across two
    periods for a 1000 kWh invoice."""
    from app.reports.secr import _energy_kwh
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2025,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.2)
    db.add(f); db.commit(); db.refresh(f)
    db.add(ActivityRecord(organisation_id=org.id, date="2025-01-05", category="electricity",
                          subcategory="", description="", quantity=1000.0, unit="kWh",
                          geo="GB", factor_id=f.id, coverage_start="2024-12-15",
                          coverage_end="2025-01-15"))
    db.commit()
    fy24 = _period(db, org.id, "FY24", "2024-01-01", "2024-12-31")
    fy25 = _period(db, org.id, "FY25", "2025-01-01", "2025-12-31")
    r24 = compute_co2e(db, org.id, reporting_period_id=fy24.id)
    r25 = compute_co2e(db, org.id, reporting_period_id=fy25.id)
    e24 = _energy_kwh(db, r24, scopes=("1", "2"))["total_kwh"]
    e25 = _energy_kwh(db, r25, scopes=("1", "2"))["total_kwh"]
    assert e24 + e25 == pytest.approx(1000.0)           # not 2000
    # ...and the implied intensity reconciles to the real factor in EACH period.
    assert (r24.total_co2e / e24) == pytest.approx(0.2)
    assert (r25.total_co2e / e25) == pytest.approx(0.2)


def test_a_prorated_share_is_priced_on_the_period_it_landed_in(db):
    """Regression (review): the share was priced on the record's single `date`, so the
    FY24 slice of a Dec-Jan invoice took FY25's residual mix and could not be covered by
    an FY24 contractual instrument."""
    from app.models import MarketInstrument, ResidualMixRate, RunResidualMixStatement
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="DE", year=2025,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.4)
    db.add(f); db.commit(); db.refresh(f)
    db.add(ActivityRecord(organisation_id=org.id, date="2025-01-05", category="electricity",
                          subcategory="", description="", quantity=1000.0, unit="kWh",
                          geo="DE", factor_id=f.id, coverage_start="2024-12-15",
                          coverage_end="2025-01-15"))
    # Different published residual mixes per year — the slice must take its OWN year's.
    for yr, kg in ((2024, 0.60), (2025, 0.50)):
        db.add(ResidualMixRate(market="DE", year=yr, kg_co2e_per_kwh=kg, status="published",
                               gas_basis="co2e", publisher="AIB"))
    db.commit()
    fy24 = _period(db, org.id, "FY24", "2024-01-01", "2024-12-31")
    r24 = compute_co2e(db, org.id, reporting_period_id=fy24.id)
    st = db.query(RunResidualMixStatement).filter_by(run_id=r24.id).one()
    assert st.year_key == 2024                                  # not 2025
    assert st.rate_kg_co2e_per_kwh == pytest.approx(0.60)
    assert r24.total_co2e_market == pytest.approx(1000.0 * 17 / 32 * 0.60)


def test_a_prorated_share_can_be_covered_by_an_instrument_from_its_own_year(db):
    from app.models import MarketInstrument
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="DE", year=2025,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.4)
    db.add(f); db.commit(); db.refresh(f)
    db.add(ActivityRecord(organisation_id=org.id, date="2025-01-05", category="electricity",
                          subcategory="", description="", quantity=1000.0, unit="kWh",
                          geo="DE", factor_id=f.id, coverage_start="2024-12-15",
                          coverage_end="2025-01-15"))
    # A REC valid only in 2024 must cover the FY24 slice, which occurred in Dec 2024.
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=None, market="DE",
                            start_date="2024-01-01", end_date="2024-12-31"))
    db.commit()
    fy24 = _period(db, org.id, "FY24", "2024-01-01", "2024-12-31")
    r24 = compute_co2e(db, org.id, reporting_period_id=fy24.id)
    assert r24.total_co2e_market == pytest.approx(0.0)      # fully covered
