import pytest
from app.services.dq import (
    indicators, score, temporal_score, geographical_score, completeness_score,
    technological_score, _UF,
)


def test_temporal_bands():
    assert temporal_score(2024, 2024) == 1
    assert temporal_score(2024, 2022) == 2
    assert temporal_score(2024, 2019) == 3
    assert temporal_score(2024, 2010) == 5
    # Missing information defaults to POOR (GHG Protocol guidance), never mid.
    assert temporal_score(None, 2024) == 5
    assert temporal_score(2024, None) == 5


def test_geographical_match():
    assert geographical_score("GB", "GB") == 1
    assert geographical_score("GB", "Global") == 3
    assert geographical_score("GB", "DE") == 4
    assert geographical_score(None, "GB") == 5
    assert geographical_score("GB", None) == 5


def test_geographical_uf_table_is_monotonic():
    """Regression: score 4 was 1.00 (a transcription bug) — worse scores must
    never contribute LESS uncertainty than better ones."""
    for key, table in _UF.items():
        assert table == sorted(table), key
    assert _UF["geographical"][3] == 1.05


def test_missing_everything_scores_poor():
    ind = indicators(method_type=None, mapping_basis=None,
                     activity_geo=None, factor_geo=None,
                     activity_year=None, factor_year=None)
    assert all(v == 5 for v in ind.values())


def test_human_decision_scores_technological_two():
    assert technological_score(None, "approved") == 2
    assert technological_score(None, "overridden") == 2
    assert technological_score(None, "unmapped") == 5
    assert technological_score("exact", None) == 1


def test_completeness_proxy_varies_with_data():
    assert completeness_score(True, True, True) == 1
    assert completeness_score(False, True, True) == 2
    assert completeness_score(False, False, False) == 4


def test_supplier_specific_scores_better_than_spend():
    sup = score(indicators("supplier_specific", "exact", "GB", "GB", 2024, 2024,
                           completeness=1))
    spend = score(indicators("spend_based", "category_only", "GB", "DE", 2024, 2015,
                             completeness=4))
    assert sup["overall"] < spend["overall"]
    assert sup["rating"] == "high"
    assert spend["rating"] == "low"
    # Better data quality => tighter uncertainty band.
    assert sup["ci95_high_mult"] < spend["ci95_high_mult"]


def test_worst_indicator_caps_rating():
    """A line on the worst method tier can't read 'high' because the rest is pristine."""
    ind = indicators("spend_based", "exact", "GB", "GB", 2024, 2024, completeness=1)
    s = score(ind)
    assert ind["reliability"] == 5
    assert s["overall"] <= 2          # mean alone would look great
    assert s["rating"] == "low"       # worst-indicator cap wins


def test_perfect_data_has_minimal_uncertainty():
    s = score(indicators("supplier_specific", "exact", "GB", "GB", 2024, 2024,
                         completeness=1))
    # Only the basic uncertainty factor contributes; band is narrow but > 1.
    assert 1.0 < s["ci95_high_mult"] < 1.2
    assert s["gsd"] >= 1.0


def test_transport_basic_uncertainty_is_higher():
    ind = indicators("average_data", "exact", "GB", "GB", 2024, 2024, completeness=1)
    grid = score(ind, category="electricity")
    flight = score(ind, category="flight")
    assert grid["basic_uncertainty"] == 1.05
    assert flight["basic_uncertainty"] == 2.00
    assert flight["ci95_high_mult"] > grid["ci95_high_mult"]
