import pytest

from app.models import Organisation, FinancedPosition
from app.services.pcaf import attribution_factor, portfolio_financed, position_financed
from app.reports.sfdr_pai import sfdr_pai_report


def _org(db, name="Bank"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _pos(db, org_id, **kw):
    defaults = dict(organisation_id=org_id, investee_name="Investee", asset_class="listed_equity",
                    currency="EUR", outstanding_amount=1_000_000.0,
                    attribution_denominator=10_000_000.0, investee_scope1_tco2e=1000.0,
                    investee_scope2_tco2e=500.0, investee_scope3_tco2e=8000.0,
                    investee_revenue_millions=50.0, data_quality_score=3)
    defaults.update(kw)
    p = FinancedPosition(**defaults)
    db.add(p); db.commit(); db.refresh(p)
    return p


def test_attribution_factor_and_financed():
    class P:
        outstanding_amount = 1_000_000.0
        attribution_denominator = 10_000_000.0
        investee_scope1_tco2e = 1000.0
        investee_scope2_tco2e = 500.0
        investee_scope3_tco2e = 8000.0
        id = 1; investee_name = "X"; asset_class = "listed_equity"; data_quality_score = 2
    assert attribution_factor(P()) == pytest.approx(0.1)
    # incl scope 3: 0.1 * (1000+500+8000) = 950
    f = position_financed(P(), include_scope3=True)
    assert f["financed_total_tco2e"] == pytest.approx(950.0)
    # excl scope 3: 0.1 * 1500 = 150
    assert position_financed(P(), include_scope3=False)["financed_total_tco2e"] == pytest.approx(150.0)


def test_portfolio_weighted_dq_and_by_asset(db):
    org = _org(db)
    _pos(db, org.id, asset_class="listed_equity", outstanding_amount=1_000_000,
         attribution_denominator=10_000_000, data_quality_score=2)   # af 0.1, financed 950
    _pos(db, org.id, asset_class="business_loans", outstanding_amount=2_000_000,
         attribution_denominator=8_000_000, data_quality_score=4)    # af 0.25, financed 2375
    r = portfolio_financed(db, org.id, include_scope3=True)
    assert r["positions"] == 2
    assert r["by_asset_class_tco2e"]["listed_equity"] == pytest.approx(950.0)
    assert r["by_asset_class_tco2e"]["business_loans"] == pytest.approx(2375.0)
    assert r["financed_emissions_tco2e"]["total"] == pytest.approx(3325.0)
    # emissions-weighted DQ: (950*2 + 2375*4)/3325
    assert r["weighted_data_quality_score"] == pytest.approx((950*2 + 2375*4) / 3325, abs=1e-3)


def test_attribution_over_100pct_flagged(db):
    org = _org(db)
    _pos(db, org.id, outstanding_amount=12_000_000, attribution_denominator=10_000_000)
    r = portfolio_financed(db, org.id)
    assert any("attribution factor > 100%" in w for w in r["warnings"])
    assert r["lines"][0]["attribution_over_100pct"] is True


def test_sfdr_pai_indicators(db):
    org = _org(db)
    # one position: af 0.1, financed total 950 (incl S3), investee intensity 9500/50=190
    _pos(db, org.id, outstanding_amount=1_000_000, attribution_denominator=10_000_000,
         investee_revenue_millions=50.0)
    r = sfdr_pai_report(db, org.id, portfolio_value_millions=2.0, include_scope3=True)
    assert r["ok"] is True
    assert r["pai_1_ghg_emissions_tco2e"]["total"] == pytest.approx(950.0)
    # PAI 2: 950 / 2 EURm = 475 tCO2e per EURm
    assert r["pai_2_carbon_footprint"]["tco2e_per_eur_million_invested"] == pytest.approx(475.0)
    # PAI 3: value-weighted intensity, single position = 9500/50 = 190
    assert r["pai_3_ghg_intensity_of_investees"]["value_weighted_tco2e_per_eur_million_revenue"] == pytest.approx(190.0)


def test_sfdr_pai_blocks_without_portfolio_value(db):
    org = _org(db)
    _pos(db, org.id)
    r = sfdr_pai_report(db, org.id)
    assert r["ok"] is False
    assert r["pai_2_carbon_footprint"] is None
    assert any("portfolio_value_millions" in b for b in r["blockers"])


def test_pai3_excludes_positions_without_revenue(db):
    org = _org(db)
    _pos(db, org.id, investee_revenue_millions=50.0)         # counted
    _pos(db, org.id, investee_revenue_millions=None)         # excluded from PAI 3
    r = sfdr_pai_report(db, org.id, portfolio_value_millions=5.0)
    assert r["pai_3_ghg_intensity_of_investees"]["positions_with_revenue"] == 1
    assert r["pai_3_ghg_intensity_of_investees"]["positions_total"] == 2


def test_finance_is_org_scoped(db):
    org_a, org_b = _org(db, "A"), _org(db, "B")
    _pos(db, org_b.id, outstanding_amount=9_000_000, attribution_denominator=10_000_000)
    r = portfolio_financed(db, org_a.id)
    assert r["positions"] == 0
    assert r["financed_emissions_tco2e"]["total"] == 0.0
    assert r["weighted_data_quality_score"] is None
