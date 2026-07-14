"""Audit Phase 0 — 'stop wrong numbers' regression tests.

Covers: fail-closed emission-factor values (NULL/inf no longer crash or poison a
run), expanded + flagged scope classification, case-insensitive GWP vintage,
the exact_global grid-factor review gate, boundary validation (gwp_set, period
dates), the DB non-negativity CHECKs, and the TNFD water-unit warning.
"""
import math
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401
from app.models import (
    Organisation, ActivityRecord, EmissionFactor, MarketInstrument,
    NatureSite, NatureImpactDependency,
)
from app.services.calc import compute_co2e
from app.services.resolver import propose_mapping, auto_map_activity
from app.services.nature import leap_assessment
from app.reports.summary import summary
from app import main as main_mod


def _org(db, name="Co"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", unit="kWh", value=0.2, geography="GB",
            gwp_set="AR6", **kw):
    f = EmissionFactor(source="T", version="1", geography=geography, year=2024,
                       category=category, subcategory=kw.pop("subcategory", ""),
                       unit=unit, gwp_set=gwp_set, value=value, **kw)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category="electricity", quantity=100.0,
              unit="kWh", geo="GB", scope=None):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo=geo, factor_id=factor_id, scope=scope)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- Fail-closed factor values -----------------------------------------------

def test_null_factor_value_does_not_crash_run(db):
    org = _org(db)
    good = _factor(db, value=0.2)
    bad = _factor(db, value=None)          # NULL value (per-gas factors legitimately do this)
    _activity(db, org.id, good.id, quantity=100)     # 20 kg
    _activity(db, org.id, bad.id, quantity=100)      # bad -> data_errors
    run = compute_co2e(db, org.id)
    assert run.status == "complete"
    assert run.mapped == 1 and run.data_errors == 1
    assert run.total_co2e == pytest.approx(20.0)     # only the good line


def test_inf_factor_value_does_not_poison_total(db):
    org = _org(db)
    good = _factor(db, value=0.2)
    bad = _factor(db, value=float("inf"))            # passes CHECK(value>=0), guarded in calc
    _activity(db, org.id, good.id, quantity=100)
    _activity(db, org.id, bad.id, quantity=100)
    run = compute_co2e(db, org.id)
    assert math.isfinite(run.total_co2e)
    assert run.total_co2e == pytest.approx(20.0)
    assert run.data_errors == 1


def test_per_gas_non_finite_mass_buckets(db):
    org = _org(db)
    bad = _factor(db, value=None, kg_co2=float("nan"))   # per-gas path, non-finite mass
    _activity(db, org.id, bad.id, quantity=100)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.mapped == 0 and run.data_errors == 1
    assert run.total_co2e == pytest.approx(0.0)


# --- Scope classification -----------------------------------------------------

def test_expanded_scope_rules(db):
    org = _org(db)
    steam = _factor(db, category="steam")
    refrig = _factor(db, category="refrigerant")
    _activity(db, org.id, steam.id, category="steam")
    _activity(db, org.id, refrig.id, category="refrigerant")
    compute_co2e(db, org.id)
    s = summary(db, organisation_id=org.id)
    by_scope = {r["scope"]: r["co2e"] for r in s["by_scope"]}
    assert "2" in by_scope        # steam -> Scope 2 (was silently Scope 3 before)
    assert "1" in by_scope        # refrigerant -> Scope 1


def test_unknown_category_is_flagged_not_silent(db):
    org = _org(db)
    f = _factor(db, category="widgets")
    _activity(db, org.id, f.id, category="widgets")
    compute_co2e(db, org.id)
    s = summary(db, organisation_id=org.id)
    # still computed (as Scope 3) but the assumption is SURFACED
    assert s["scope_assumptions"] is not None
    assert "widgets" in s["scope_assumptions"]["assumed_scope3_by_category"]


def test_known_category_has_no_scope_assumption(db):
    org = _org(db)
    f = _factor(db, category="electricity")
    _activity(db, org.id, f.id, category="electricity")
    compute_co2e(db, org.id)
    assert summary(db, organisation_id=org.id)["scope_assumptions"] is None


# --- GWP vintage --------------------------------------------------------------

def test_gwp_set_compare_is_case_insensitive(db):
    org = _org(db)
    f = _factor(db, gwp_set="ar6")           # stored lower-case
    _activity(db, org.id, f.id)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.gwp_mismatch == 0 and run.mapped == 1


# --- DB non-negativity CHECKs -------------------------------------------------

def test_negative_factor_value_rejected_by_db(db):
    with pytest.raises(IntegrityError):
        db.add(EmissionFactor(source="T", version="1", geography="GB", year=2024,
                              category="electricity", subcategory="", unit="kWh",
                              gwp_set="AR6", value=-0.1))
        db.commit()
    db.rollback()


def test_negative_instrument_rate_rejected_by_db(db):
    org = _org(db)
    with pytest.raises(IntegrityError):
        db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                                kg_co2e_per_kwh=-1.0))
        db.commit()
    db.rollback()


# --- Resolver: Global grid factor review gate --------------------------------

def test_global_grid_factor_routes_to_review(db):
    org = _org(db)
    _factor(db, category="electricity", geography="Global", value=0.5)
    hit = propose_mapping(db, "electricity", "", None, "GB", gwp_set="AR6")
    assert hit is not None
    _factor_obj, basis, conf = hit
    assert basis == "global_geo_sensitive" and conf < 0.95
    a = _activity(db, org.id, None, category="electricity")
    a.factor_id = None
    assert auto_map_activity(db, a) == "needs_review"
    assert a.factor_id is None and a.suggested_factor_id is not None


def test_global_combustion_factor_auto_binds(db):
    org = _org(db)
    f = _factor(db, category="diesel", geography="Global", unit="L", value=2.5)
    hit = propose_mapping(db, "diesel", "", None, "GB", gwp_set="AR6")
    assert hit[1] == "exact_global" and hit[2] == 0.95
    a = _activity(db, org.id, None, category="diesel", unit="L")
    a.factor_id = None
    assert auto_map_activity(db, a) == "auto"
    assert a.factor_id == f.id


# --- Nature water-unit warning ------------------------------------------------

def test_water_withdrawal_missing_unit_flagged(db):
    org = _org(db)
    s = NatureSite(organisation_id=org.id, name="mill", water_stress="extreme",
                   area_hectares=1.0)
    db.add(s); db.commit(); db.refresh(s)
    db.add(NatureImpactDependency(site_id=s.id, kind="impact", driver="freshwater_use",
                                  materiality="high", metric_value=1000.0, metric_unit=None))
    db.commit()
    r = leap_assessment(db, org.id)
    assert any("no metric_unit" in w.lower() or "NO metric_unit" in w for w in r["warnings"])


# --- API boundary validation --------------------------------------------------

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
    yield c, {"X-API-Key": key}
    main_mod.app.dependency_overrides.clear()


def test_calc_run_rejects_unknown_gwp_set(client):
    c, hdr = client
    assert c.post("/calculate/run", params={"gwp_set": "AR9"}, headers=hdr).status_code == 400


def test_calc_run_normalises_gwp_set_case(client):
    c, hdr = client
    assert c.post("/calculate/run", params={"gwp_set": "ar6"}, headers=hdr).status_code == 200


def test_reporting_period_rejects_bad_dates(client):
    c, hdr = client
    assert c.post("/reporting_periods", params={"label": "x", "start_date": "not-a-date"},
                  headers=hdr).status_code == 400
    assert c.post("/reporting_periods",
                  params={"label": "x", "start_date": "2025-12-31", "end_date": "2025-01-01"},
                  headers=hdr).status_code == 400
    assert c.post("/reporting_periods",
                  params={"label": "x", "start_date": "2025-01-01", "end_date": "2025-12-31"},
                  headers=hdr).status_code == 200
