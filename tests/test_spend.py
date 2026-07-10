import pytest

from app.models import FxRate, PriceIndex, EmissionFactor, ActivityRecord, Organisation, EmissionLineItem
from app.services.spend import normalize_spend, SpendNormalizationError
from app.services.calc import compute_co2e


def test_same_currency_same_year_passthrough(db):
    s = normalize_spend(db, 1000.0, "GBP", 2021, "GBP", 2021)
    assert s["amount_in_factor_currency"] == 1000.0


def test_fx_conversion_at_base_year_rate(db):
    db.add(FxRate(base_currency="EUR", quote_currency="GBP", year=2021, rate=0.85))
    db.commit()
    s = normalize_spend(db, 1000.0, "EUR", 2021, "GBP", 2021)
    assert s["amount_in_factor_currency"] == pytest.approx(850.0)
    assert s["fx_year"] == 2021


def test_inverse_fx_rate_used_when_only_reverse_present(db):
    db.add(FxRate(base_currency="GBP", quote_currency="EUR", year=2021, rate=1.25))
    db.commit()
    s = normalize_spend(db, 1000.0, "EUR", 2021, "GBP", 2021)
    assert s["amount_in_factor_currency"] == pytest.approx(800.0)   # 1/1.25


def test_inflation_adjustment_to_base_year(db):
    db.add_all([PriceIndex(currency="GBP", year=2024, index_value=110.0),
                PriceIndex(currency="GBP", year=2021, index_value=100.0)])
    db.commit()
    # 1100 GBP of 2024 spend deflated to 2021 basis = 1100 * (100/110) = 1000
    s = normalize_spend(db, 1100.0, "GBP", 2024, "GBP", 2021)
    assert s["amount_in_factor_currency"] == pytest.approx(1000.0)


def test_missing_fx_rate_fails_closed(db):
    with pytest.raises(SpendNormalizationError):
        normalize_spend(db, 1000.0, "EUR", 2021, "GBP", 2021)


def test_missing_price_index_fails_closed(db):
    with pytest.raises(SpendNormalizationError):
        normalize_spend(db, 1000.0, "GBP", 2024, "GBP", 2021)   # no indices loaded


def _org(db):
    o = Organisation(name="Org"); db.add(o); db.commit(); db.refresh(o); return o


def _spend_factor(db, currency="GBP", base_year=2021, value=0.05):
    f = EmissionFactor(source="EEIO", version="1", geography="GB", year=2024,
                       category="spend", subcategory="services", unit=currency, gwp_set="AR6",
                       value=value, method_type="spend_based", lca_boundary="cradle_to_gate",
                       base_year=base_year, price_basis="basic")
    db.add(f); db.commit(); db.refresh(f); return f


def test_calc_end_to_end_with_fx_and_inflation(db):
    org = _org(db)
    f = _spend_factor(db)                                   # GBP, base 2021, 0.05/GBP
    db.add_all([FxRate(base_currency="EUR", quote_currency="GBP", year=2021, rate=0.90),
                PriceIndex(currency="EUR", year=2024, index_value=112.0),
                PriceIndex(currency="EUR", year=2021, index_value=100.0)])
    db.commit()
    a = ActivityRecord(organisation_id=org.id, date="2024-06-01", category="spend",
                       subcategory="services", description="", quantity=11200, unit="EUR",
                       geo="GB", factor_id=f.id)
    db.add(a); db.commit()
    run = compute_co2e(db, org.id)
    # 11200 EUR(2024) -> deflate to 2021: *100/112 = 10000 EUR -> *0.90 = 9000 GBP -> *0.05 = 450 kg
    assert run.mapped == 1
    assert run.total_co2e == pytest.approx(450.0)
    import json
    li = db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id).one()
    steps = json.loads(li.details)["spend_normalization"]
    assert steps["inflation_factor"] == pytest.approx(100/112)
    assert steps["fx_rate"] == pytest.approx(0.90)


def test_calc_missing_reference_data_is_data_error(db):
    org = _org(db)
    f = _spend_factor(db)
    a = ActivityRecord(organisation_id=org.id, date="2024-01-01", category="spend",
                       subcategory="services", description="", quantity=1000, unit="EUR",
                       geo="GB", factor_id=f.id)
    db.add(a); db.commit()
    run = compute_co2e(db, org.id)                          # no FX/CPI loaded
    assert run.data_errors == 1
    assert run.mapped == 0
