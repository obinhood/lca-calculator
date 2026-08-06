"""Hand-computed oracles for the calculation paths where a bug would be hardest to spot.

Each expected value here is worked out by hand from the published rule and written as a
literal, so the test fails if the engine's arithmetic drifts — even if the engine's own
helpers all agree with each other.

Paths covered: per-gas GWP recomposition (and the published constants themselves), the
AR5/AR6 vintage switch, spend-based inflation + FX alignment, LCA allocation, and
market-based Scope 2 volume matching.
"""
import pytest

from app.models import (EmissionFactor, ActivityRecord, Organisation, MarketInstrument,
                        FxRate, PriceIndex, LcaAssessment, LcaItem)
from app.services.calc import compute_co2e, _utcnow_iso
from app.services.gwp import GWP_100, co2e_from_gases
from app.services.lca import compute_assessment


def _org(db, name="OracleCo"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _activity(db, org_id, factor, qty, unit=None, date="2025-06-15", **kw):
    a = ActivityRecord(organisation_id=org_id, date=date, category=factor.category,
                       subcategory="", description="", quantity=qty,
                       unit=unit or factor.unit, geo="GB", factor_id=factor.id, **kw)
    db.add(a); db.commit(); db.refresh(a)
    return a


# ---------------------------------------------------------------------------------------
# 1. The published GWP constants themselves.

def test_gwp_constants_match_the_published_ipcc_tables():
    """A typo here would silently mis-state every per-gas calculation. Values are IPCC
    AR5 Table 8.7 and AR6 Table 7.15 (100-year, no climate-carbon feedback on the fossil/
    non-fossil split as used for corporate reporting)."""
    assert GWP_100["AR5"]["CO2"] == 1.0
    assert GWP_100["AR5"]["CH4_fossil"] == 30.0
    assert GWP_100["AR5"]["CH4_biogenic"] == 28.0
    assert GWP_100["AR5"]["N2O"] == 265.0
    assert GWP_100["AR5"]["SF6"] == 23500.0
    assert GWP_100["AR5"]["NF3"] == 16100.0

    assert GWP_100["AR6"]["CO2"] == 1.0
    assert GWP_100["AR6"]["CH4_fossil"] == 29.8
    assert GWP_100["AR6"]["CH4_biogenic"] == 27.0
    assert GWP_100["AR6"]["N2O"] == 273.0
    assert GWP_100["AR6"]["SF6"] == 25200.0
    assert GWP_100["AR6"]["NF3"] == 17400.0

    # Directional sanity the tables must always satisfy.
    assert GWP_100["AR6"]["N2O"] > GWP_100["AR5"]["N2O"]        # N2O revised UP in AR6
    assert GWP_100["AR6"]["CH4_fossil"] < GWP_100["AR5"]["CH4_fossil"]   # CH4 revised down
    for s in ("AR5", "AR6"):
        assert GWP_100[s]["CH4_fossil"] > GWP_100[s]["CH4_biogenic"]     # fossil carbon extra


def test_per_gas_recomposition_matches_hand_arithmetic():
    """2 kg CO2 + 0.1 kg fossil CH4 + 0.01 kg N2O under AR6:
       2*1.0 + 0.1*29.8 + 0.01*273.0 = 2 + 2.98 + 2.73 = 7.71"""
    got = co2e_from_gases({"CO2": 2.0, "CH4_fossil": 0.1, "N2O": 0.01}, "AR6")
    assert got == pytest.approx(7.71, rel=1e-12)

    # Same masses under AR5: 2*1.0 + 0.1*30.0 + 0.01*265.0 = 2 + 3 + 2.65 = 7.65
    got5 = co2e_from_gases({"CO2": 2.0, "CH4_fossil": 0.1, "N2O": 0.01}, "AR5")
    assert got5 == pytest.approx(7.65, rel=1e-12)


def test_engine_applies_per_gas_gwp_at_calculation_time(db):
    """A per-gas factor must be recomposed by the RUN's vintage, not the factor's stored
    aggregate — that is what makes the AR5/AR6 switch real rather than cosmetic.

    100 kWh of a factor carrying 0.18 kg CO2 + 0.0002 kg CH4(fossil) + 0.00001 kg N2O/kWh:
      AR6: 100 * (0.18 + 0.0002*29.8 + 0.00001*273) = 100 * (0.18+0.00596+0.00273) = 18.869
      AR5: 100 * (0.18 + 0.0002*30.0 + 0.00001*265) = 100 * (0.18+0.00600+0.00265) = 18.865
    """
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="gas", subcategory="", unit="kWh", gwp_set="AR6",
                       value=0.18869, kg_co2=0.18, kg_ch4=0.0002, kg_n2o=0.00001,
                       ch4_origin="fossil")
    db.add(f); db.commit(); db.refresh(f)
    _activity(db, org.id, f, 100.0)

    assert compute_co2e(db, org.id, gwp_set="AR6").total_co2e == pytest.approx(18.869, rel=1e-9)
    assert compute_co2e(db, org.id, gwp_set="AR5").total_co2e == pytest.approx(18.865, rel=1e-9)
    # Direction check, worked through: per kWh the CH4 term FALLS by 0.0002*(29.8-30) =
    # -0.00004 while the N2O term RISES by 0.00001*(273-265) = +0.00008, so AR6 lands 0.004
    # above AR5 over 100 kWh. The vintage switch must move the number by exactly that.
    ar6 = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    ar5 = compute_co2e(db, org.id, gwp_set="AR5").total_co2e
    assert ar6 > ar5
    assert ar6 - ar5 == pytest.approx(0.004, abs=1e-9)


def test_vintage_switch_changes_only_by_the_gwp_delta(db):
    """The AR5->AR6 difference must be exactly the per-gas GWP difference, nothing else."""
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="gas", subcategory="", unit="kWh", gwp_set="AR6",
                       value=1.0, kg_co2=0.0, kg_ch4=1.0, kg_n2o=0.0, ch4_origin="fossil")
    db.add(f); db.commit(); db.refresh(f)
    _activity(db, org.id, f, 1.0)
    ar6 = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    ar5 = compute_co2e(db, org.id, gwp_set="AR5").total_co2e
    assert ar6 == pytest.approx(29.8, rel=1e-12)     # 1 kg fossil CH4
    assert ar5 == pytest.approx(30.0, rel=1e-12)
    assert ar5 - ar6 == pytest.approx(0.2, rel=1e-9)


# ---------------------------------------------------------------------------------------
# 2. Spend-based: inflation adjustment then FX, both at the factor's base year.

def test_spend_inflation_and_fx_match_hand_arithmetic(db):
    """£10,000 spent in 2024 against a EUR factor with base year 2021.

    Step 1 — deflate GBP 2024 -> 2021 using the CPI ratio: 10000 * (100.0/119.0)
             = 8403.361344...
    Step 2 — FX GBP->EUR at the BASE year. Only EUR->GBP 2021 = 0.86 is stored, so the
             engine must invert it: multiply by 1/0.86.
             8403.361344 / 0.86 = 9771.350400...
    Step 3 — apply the factor: 9771.3504 * 0.045 = 439.710768 kg
    """
    org = _org(db)
    now = _utcnow_iso()
    db.add_all([
        PriceIndex(currency="GBP", year=2021, index_value=100.0, recorded_at=now),
        PriceIndex(currency="GBP", year=2024, index_value=119.0, recorded_at=now),
        FxRate(base_currency="EUR", quote_currency="GBP", year=2021, rate=0.86,
               recorded_at=now),
    ])
    f = EmissionFactor(source="T", version="1", geography="EU", year=2021,
                       category="spend", subcategory="services", unit="EUR", gwp_set="AR6",
                       value=0.045, method_type="spend_based", base_year=2021,
                       price_basis="purchaser")
    db.add(f); db.commit(); db.refresh(f)
    _activity(db, org.id, f, 10000.0, unit="GBP", date="2024-05-01")

    expected = 10000.0 * (100.0 / 119.0) / 0.86 * 0.045
    assert expected == pytest.approx(439.710768, rel=1e-6)      # the hand figure
    got = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    assert got == pytest.approx(expected, rel=1e-9)


def test_spend_in_the_base_year_needs_no_inflation_step(db):
    """Same currency, same year as the factor base: the amount must pass through untouched."""
    org = _org(db)
    now = _utcnow_iso()
    db.add(PriceIndex(currency="GBP", year=2021, index_value=100.0, recorded_at=now))
    f = EmissionFactor(source="T", version="1", geography="GB", year=2021,
                       category="spend", subcategory="it", unit="GBP", gwp_set="AR6",
                       value=0.32, method_type="spend_based", base_year=2021,
                       price_basis="purchaser")
    db.add(f); db.commit(); db.refresh(f)
    _activity(db, org.id, f, 1000.0, unit="GBP", date="2021-03-03")
    assert compute_co2e(db, org.id, gwp_set="AR6").total_co2e == pytest.approx(320.0, rel=1e-12)


# ---------------------------------------------------------------------------------------
# 3. Market-based Scope 2: volume matching against contractual instruments.

def test_partial_rec_coverage_matches_hand_arithmetic(db):
    """1,000 kWh consumed, a 400 kWh zero-carbon REC, grid factor 0.20 kg/kWh.

      location-based = 1000 * 0.20                      = 200.0
      market-based   = 400 * 0.0  +  600 * 0.20         = 120.0
    """
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh", gwp_set="AR6",
                       value=0.20)
    db.add(f); db.commit(); db.refresh(f)
    _activity(db, org.id, f, 1000.0)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=400.0, gwp_set="AR6"))
    db.commit()

    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.total_co2e == pytest.approx(200.0, rel=1e-12)
    assert run.total_co2e_market == pytest.approx(120.0, rel=1e-9)


def test_instrument_volume_cannot_exceed_consumption(db):
    """A 5,000 kWh REC against 1,000 kWh of consumption may only cover 1,000 kWh —
    over-crediting would drive the market figure negative or to a fictitious zero."""
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh", gwp_set="AR6",
                       value=0.20)
    db.add(f); db.commit(); db.refresh(f)
    _activity(db, org.id, f, 1000.0)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=5000.0, gwp_set="AR6"))
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.total_co2e_market == pytest.approx(0.0, abs=1e-9)
    assert run.total_co2e_market >= 0.0


def test_supplier_specific_rate_is_applied_at_its_own_rate(db):
    """800 kWh at a supplier rate of 0.05 + 200 kWh grid at 0.20:
       market = 800*0.05 + 200*0.20 = 40 + 40 = 80.0"""
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh", gwp_set="AR6",
                       value=0.20)
    db.add(f); db.commit(); db.refresh(f)
    _activity(db, org.id, f, 1000.0)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="supplier_specific",
                            kg_co2e_per_kwh=0.05, coverage_kwh=800.0, gwp_set="AR6"))
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.total_co2e_market == pytest.approx(80.0, rel=1e-9)


# ---------------------------------------------------------------------------------------
# 4. LCA allocation factor.

def test_lca_allocation_factor_scales_the_line(db):
    """500 kg of steel at 2.0 kgCO2e/kg with a 0.4 allocation factor:
       500 * 2.0 * 0.4 = 400.0, and per functional unit (100 units) = 4.0"""
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="material", subcategory="steel", unit="kg", gwp_set="AR6",
                       value=2.0)
    db.add(f); db.commit(); db.refresh(f)
    a = LcaAssessment(organisation_id=org.id, name="W", standard="iso_14067",
                      functional_unit="1 widget", functional_unit_quantity=100.0,
                      gwp_set="AR6")
    db.add(a); db.commit(); db.refresh(a)
    db.add(LcaItem(assessment_id=a.id, stage="raw_materials", quantity=500.0, unit="kg",
                   factor_id=f.id, allocation_factor=0.4))
    db.commit()

    r = compute_assessment(db, a)
    assert r["total_co2e_kg"] == pytest.approx(400.0, rel=1e-12)
    assert r["co2e_per_functional_unit_kg"] == pytest.approx(4.0, rel=1e-12)


def test_allocation_factors_are_additive_across_items(db):
    """Two half-allocated items must equal one fully-allocated item of the same quantity."""
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="material", subcategory="steel", unit="kg", gwp_set="AR6",
                       value=2.0)
    db.add(f); db.commit(); db.refresh(f)
    a = LcaAssessment(organisation_id=org.id, name="W", standard="iso_14067",
                      functional_unit="1 kg", functional_unit_quantity=1.0, gwp_set="AR6")
    db.add(a); db.commit(); db.refresh(a)
    db.add_all([
        LcaItem(assessment_id=a.id, stage="raw_materials", quantity=100.0, unit="kg",
                factor_id=f.id, allocation_factor=0.5),
        LcaItem(assessment_id=a.id, stage="raw_materials", quantity=100.0, unit="kg",
                factor_id=f.id, allocation_factor=0.5),
    ])
    db.commit()
    assert compute_assessment(db, a)["total_co2e_kg"] == pytest.approx(200.0, rel=1e-12)
