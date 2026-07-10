import pytest
from app.services.dq import indicators, score, temporal_score, geographical_score


def test_temporal_bands():
    assert temporal_score(2024, 2024) == 1
    assert temporal_score(2024, 2022) == 2
    assert temporal_score(2024, 2019) == 3
    assert temporal_score(2024, 2010) == 5
    assert temporal_score(None, 2024) == 3


def test_geographical_match():
    assert geographical_score("GB", "GB") == 1
    assert geographical_score("GB", "Global") == 3
    assert geographical_score("GB", "DE") == 4


def test_supplier_specific_scores_better_than_spend():
    sup = score(indicators("supplier_specific", "exact", "GB", "GB", 2024, 2024))
    spend = score(indicators("spend_based", "category_only", "GB", "DE", 2024, 2015))
    assert sup["overall"] < spend["overall"]
    assert sup["rating"] == "high"
    assert spend["rating"] == "low"
    # Better data quality => tighter uncertainty band.
    assert sup["ci95_high_mult"] < spend["ci95_high_mult"]


def test_perfect_data_has_minimal_uncertainty():
    s = score(indicators("supplier_specific", "exact", "GB", "GB", 2024, 2024))
    # Only the basic uncertainty factor contributes; band is narrow but > 1.
    assert 1.0 < s["ci95_high_mult"] < 1.2
    assert s["gsd"] >= 1.0
