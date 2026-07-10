"""Cross-framework consistency: one immutable run must render identically-
agreeing numbers in SECR, SB 253, and ESRS E1 — and none of them may drift
when live state changes after the run (frozen-run doctrine)."""
import math
import pytest

from app.models import EmissionFactor, ActivityRecord, Organisation, MarketInstrument
from app.services.calc import compute_co2e
from app.reports.secr import secr_report
from app.reports.sb253 import sb253_report
from app.reports.esrs_e1 import esrs_e1_report


def _org(db, name="DemoOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, subcategory="", **kw):
    f = EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography="GB", year=2024,
                       category=category, subcategory=subcategory, unit=unit,
                       gwp_set="AR6", value=value, **kw)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category, quantity, unit, scope=None, subcategory=""):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory=subcategory, description="", quantity=quantity,
                       unit=unit, geo="GB", factor_id=factor_id, scope=scope)
    db.add(a); db.commit(); db.refresh(a)
    return a


@pytest.fixture
def mixed_run(db):
    """Scopes 1/2/3, partial REC, biogenic waste — the consistency stress case."""
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id,
              "electricity", 1200, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 800, "kWh")
    f_bio = _factor(db, "waste", "kg", 0.01, subcategory="composting",
                    kg_co2_biogenic=0.2)
    _activity(db, org.id, f_bio.id, "waste", 100, "kg", subcategory="composting")
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=700.0,
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    return org, run


def _all_three(db, org, run):
    return (secr_report(db, org.id, run_id=run.id, intensity_denominator=2.0),
            sb253_report(db, org.id, run_id=run.id, assurance_level="limited"),
            esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=2.0))


def test_three_renderers_agree_exactly(db, mixed_run):
    org, run = mixed_run
    secr, sb, esrs = _all_three(db, org, run)
    e_secr, e_sb = secr["emissions_tco2e"], sb["emissions_tco2e"]
    e_esrs = esrs["e1_6_gross_ghg_emissions_tco2e"]
    for key_secr, key_sb, key_esrs in (
            ("scope1", "scope1", "scope1"),
            ("scope2_location_based",) * 3,
            ("scope2_market_based",) * 3,
            ("scope3_voluntary", "scope3", "scope3"),
            ("total_location_based",) * 3,
            ("total_market_based",) * 3,
            ("biogenic_co2_separate", "biogenic_co2_separate", "biogenic_co2_separate")):
        assert e_secr[key_secr] == e_sb[key_sb] == e_esrs[key_esrs], key_secr
    assert e_secr["biogenic_co2_separate"] == pytest.approx(0.02)   # 100 kg * 0.2
    # Scope arithmetic holds in the shared numbers.
    assert e_esrs["scope1"] + e_esrs["scope2_location_based"] + e_esrs["scope3"] == \
        pytest.approx(e_esrs["total_location_based"])


def test_methodology_frozen_after_remap_and_unmap(db, mixed_run):
    """F1 regression: a post-run re-map/un-map must not rewrite an immutable
    run's methodology statement in ANY renderer."""
    org, run = mixed_run
    before = [r["methodology_statement"] for r in _all_three(db, org, run)]
    assert all("DEFRA_DEMO v2024.1" in m for m in before)

    # Re-map one activity to a different factor version, then un-map another.
    f_new = _factor(db, "electricity", "kWh", 0.5)
    f_new.version = "2099.9"; db.commit()
    acts = db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id).all()
    acts[0].factor_id = f_new.id
    acts[1].factor_id = None
    db.commit()

    after = [r["methodology_statement"] for r in _all_three(db, org, run)]
    assert after == before                       # byte-identical, frozen
    assert all("2099.9" not in m for m in after)
    assert all("none" not in m.split("Emission factors: ")[1][:6] for m in after)


def test_esrs_scope3_categories_respect_frozen_scope(db):
    """F2 regression: preset scopes must drive the category split, not names."""
    org = _org(db)
    # A non-carrier category explicitly in scope 1 (e.g. refrigerants)...
    f_ref = _factor(db, "refrigerants", "kg", 1430.0)
    _activity(db, org.id, f_ref.id, "refrigerants", 2, "kg", scope="1")
    # ...and a gas activity explicitly preset to scope 3 (downstream use).
    f_gas = _factor(db, "gas", "kWh", 0.184)
    _activity(db, org.id, f_gas.id, "gas", 300, "kWh", scope="3")
    run = compute_co2e(db, org.id)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    e = r["e1_6_gross_ghg_emissions_tco2e"]
    assert "refrigerants" not in e["scope3_by_category"]          # scope 1, excluded
    assert e["scope3_by_category"]["gas"] == pytest.approx(0.0552)  # scope 3, included
    # The breakdown must sum to the scope-3 total exactly.
    assert sum(e["scope3_by_category"].values()) == pytest.approx(e["scope3"])


def test_esrs_energy_is_scope_bounded(db):
    """F5 regression: a scope-3 gas activity must not inflate E1-5 energy."""
    org = _org(db)
    f_gas = _factor(db, "gas", "kWh", 0.184)
    _activity(db, org.id, f_gas.id, "gas", 800, "kWh")            # scope 1 (default)
    _activity(db, org.id, f_gas.id, "gas", 300, "kWh", scope="3")  # value-chain gas
    run = compute_co2e(db, org.id)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    assert r["e1_5_energy_consumption"]["by_carrier_mwh"]["gas"] == pytest.approx(0.8)
    # SECR's UK-energy figure remains deliberately scope-agnostic.
    s = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0)
    assert s["energy_use_kwh"]["gas"] == pytest.approx(1100.0)


def test_infinite_denominators_fail_closed_at_report_layer(db, mixed_run):
    """F4 regression: +inf must not yield a 'disclosure-ready' zero intensity."""
    org, run = mixed_run
    s = secr_report(db, org.id, run_id=run.id, intensity_denominator=math.inf)
    assert s["disclosure_ready"] is False
    e = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=math.inf)
    assert e["disclosure_ready"] is False
