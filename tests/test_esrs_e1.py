import pytest

from app.models import EmissionFactor, ActivityRecord, Organisation, MarketInstrument
from app.services.calc import compute_co2e
from app.reports.esrs_e1 import esrs_e1_report


def _org(db, name="DemoOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, geo="GB", subcategory=""):
    f = EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography=geo, year=2024,
                       category=category, subcategory=subcategory, unit=unit,
                       gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category, quantity, unit):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


@pytest.fixture
def seeded(db):
    """electricity 1200 kWh, gas 800 kWh, diesel 150 L, waste 250 kg (scope 3, Cat 5).

    Returns a period-scoped, fully-screened, disclosure-ready run (all 15 GHGP
    Scope 3 categories declared) so the golden tests exercise a real filing."""
    from tests.scope3_util import ready_run
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id,
              "electricity", 1200, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 800, "kWh")
    _activity(db, org.id, _factor(db, "diesel", "L", 2.676).id, "diesel", 150, "L")
    waste = _activity(db, org.id, _factor(db, "waste", "kg", 0.480, subcategory="").id,
                      "waste", 250, "kg")
    waste.ghgp_category = 5      # operational waste -> Cat 5 (resolve the ambiguity)
    db.commit()
    run, _period = ready_run(db, org.id)
    return org, run


def test_esrs_e1_golden_values(db, seeded):
    org, run = seeded
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=10.0)
    e = r["e1_6_gross_ghg_emissions_tco2e"]
    assert e["scope1"] == pytest.approx(0.5486)
    assert e["scope2_location_based"] == pytest.approx(0.204)
    assert e["scope2_market_based"] == pytest.approx(0.204)
    assert e["scope3"] == pytest.approx(0.120)                  # waste 250*0.48
    assert e["scope3_ghgp_categories"]["5"]["tco2e"] == pytest.approx(0.120)  # Cat 5
    assert r["e1_6_scope3_screening"]["included"] == [5]
    assert e["total_location_based"] == pytest.approx(0.8726)
    # Intensity: 0.8726 t / 10 M€ revenue
    assert e["ghg_intensity"]["tco2e_total_location_per_million_revenue"] == \
        pytest.approx(0.08726)
    # E1-5 energy in MWh: 1.2 + 0.8 + 1.5 (diesel @ demo 10 kWh/L) = 3.5 MWh
    assert r["e1_5_energy_consumption"]["total_mwh"] == pytest.approx(3.5)
    assert r["disclosure_ready"] is True
    assert r["e1_7_removals_and_credits"]["removals_retired_tco2e"] == 0.0
    assert r["e1_7_removals_and_credits"]["credit_count"] == 0
    assert "E1-4 targets" in " ".join(r["not_covered"])


def test_esrs_e1_blocks_without_revenue(db, seeded):
    org, run = seeded
    r = esrs_e1_report(db, org.id, run_id=run.id)
    assert r["disclosure_ready"] is False
    assert any("net_revenue_millions" in b for b in r["blockers"])


def test_esrs_e1_renewable_split_from_rec(db, seeded):
    org, _ = seeded
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=700.0,
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=10.0)
    energy = r["e1_5_energy_consumption"]
    # 700 of 1200 kWh contractually covered by the REC -> 0.7 MWh renewable
    assert energy["electricity_renewable_contractual_mwh"] == pytest.approx(0.7)
    e = r["e1_6_gross_ghg_emissions_tco2e"]
    assert e["scope2_market_based"] < e["scope2_location_based"]


def test_esrs_e1_biogenic_separate(db, seeded):
    org, _ = seeded
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category="waste", subcategory="composting", unit="kg",
                       gwp_set="AR6", value=0.01, kg_co2_biogenic=0.2)
    db.add(f); db.commit(); db.refresh(f)
    a = ActivityRecord(organisation_id=org.id, date="2025-01-01", category="waste",
                       subcategory="composting", description="", quantity=100,
                       unit="kg", geo="GB", factor_id=f.id)
    db.add(a); db.commit()
    run = compute_co2e(db, org.id)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=10.0)
    e = r["e1_6_gross_ghg_emissions_tco2e"]
    assert e["biogenic_co2_separate"] == pytest.approx(0.02)    # 20 kg, own line
    # and never netted into the gross totals
    assert e["total_location_based"] == pytest.approx(0.8726 + 0.001)


def test_esrs_e1_blocks_partial_run(db, seeded):
    org, _ = seeded
    _activity(db, org.id, None, "widgets", 5, "kg")             # unmapped
    run = compute_co2e(db, org.id)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=10.0)
    assert r["disclosure_ready"] is False
    assert any("PARTIAL" in b for b in r["blockers"])


def test_esrs_e1_no_run_yet(db):
    org = _org(db)
    r = esrs_e1_report(db, org.id, net_revenue_millions=10.0)
    assert r["disclosure_ready"] is False
    assert any("no calculation run" in b for b in r["blockers"])
