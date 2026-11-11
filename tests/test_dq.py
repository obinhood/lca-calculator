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


# --- the pedigree compilation, pinned to published arithmetic ----------------------------
# Every assertion above this line is RELATIONAL (a < b, > 1.0, is not None). That is why
# the engine could sum ecoinvent's variance contributions without the factor of a half
# that SDg95 = sigma_g^2 implies — publishing every 95% interval twice as wide in log
# space as the data gives — and the whole suite stayed green. These tests pin the
# arithmetic itself, so the compilation cannot drift again without something failing.

import math

from app.services.dq import PEDIGREE_METHOD
from app.services.comparability import pedigree_comparable


def test_worst_technological_cell_matches_the_published_variance():
    """ecoinvent Table 10.5 caps 'further technological correlation' at variance 0.12.

    services/crosswalk.py:PEDIGREE_TECH_CORRELATION_CAP_VARIANCE hardcodes that same
    0.12 and is the reason the halving is provably correct rather than a judgement
    call: 0.12 is reachable from the published uncertainty factor 2.00 only as
    (ln 2.00 / 2)^2 = 0.120113. Without the halving it is (ln 2.00)^2 = 0.480453, four
    times too large. The two modules once disagreed; this keeps them honest.
    """
    from app.services.crosswalk import PEDIGREE_TECH_CORRELATION_CAP_VARIANCE
    contribution = 0.25 * math.log(_UF["technological"][4]) ** 2
    assert contribution == pytest.approx(PEDIGREE_TECH_CORRELATION_CAP_VARIANCE, abs=2e-4)


def test_sigma_is_half_the_root_of_the_summed_variances():
    """sigma = 0.5 * sqrt(SUM (ln Ui)^2 + (ln Ub)^2), computed here independently."""
    ind = {"reliability": 3, "completeness": 3, "temporal": 3,
           "geographical": 3, "technological": 3}
    s = score(dict(ind), category="electricity")
    expected = 0.5 * math.sqrt(
        sum(math.log(_UF[k][ind[k] - 1]) ** 2 for k in _UF) + math.log(1.05) ** 2)
    assert s["sigma_log"] == pytest.approx(expected, abs=5e-5)
    # And concretely, so a future reader sees the number and not only the formula.
    assert s["sigma_log"] == pytest.approx(0.1189, abs=5e-5)


def test_gsd_is_the_geometric_sd_and_not_its_square():
    """The field is named `gsd`, so it must be exp(sigma) — not ecoinvent's SDg95.

    The superseded compilation returned exp(2*sigma) here, which IS SDg95, mislabelled.
    """
    ind = {"reliability": 5, "completeness": 5, "temporal": 5,
           "geographical": 5, "technological": 5}
    s = score(dict(ind))
    # Anchored independently as well as self-consistently: a test that reads
    # sigma back out of the code cannot notice the code using the wrong one.
    expected_sigma = 0.5 * math.sqrt(
        sum(math.log(_UF[k][4]) ** 2 for k in _UF) + math.log(1.05) ** 2)
    assert s["sigma_log"] == pytest.approx(expected_sigma, abs=5e-5)
    assert s["gsd"] == pytest.approx(math.exp(s["sigma_log"]), abs=5e-4)
    # SDg95 = sigma_g^2 is the 95% range factor; the two must not be confused.
    sdg95 = math.exp(2 * s["sigma_log"])
    assert sdg95 == pytest.approx(s["gsd"] ** 2, abs=1e-3)
    assert s["gsd"] < sdg95


def test_the_95_interval_uses_1_96_sigma_not_3_92():
    ind = {"reliability": 4, "completeness": 4, "temporal": 4,
           "geographical": 4, "technological": 4}
    s = score(dict(ind))
    sigma = s["sigma_log"]
    assert s["ci95_high_mult"] == pytest.approx(math.exp(1.96 * sigma), abs=5e-4)
    assert s["ci95_low_mult"] == pytest.approx(math.exp(-1.96 * sigma), abs=5e-4)
    # The superseded compilation doubled sigma, so its band was exactly twice as
    # wide in log space. Stated on the widths themselves, because the published
    # multipliers are rounded to 4dp and a ratio of logs of rounded values is not.
    new_log_width = math.log(s["ci95_high_mult"]) - math.log(s["ci95_low_mult"])
    assert new_log_width == pytest.approx(2 * 1.96 * sigma, abs=1e-3)
    old_log_width = 2 * 1.96 * (2 * sigma)
    assert old_log_width == pytest.approx(2 * new_log_width, abs=1e-3)


def test_every_scored_line_carries_its_compilation():
    s = score({"reliability": 1, "completeness": 1, "temporal": 1,
               "geographical": 1, "technological": 1})
    assert s["method"] == PEDIGREE_METHOD


# --- and the two compilations must not be subtracted from one another --------------------

class _Run:
    def __init__(self, pedigree_method):
        self.pedigree_method = pedigree_method


def test_runs_on_the_same_compilation_compare():
    assert pedigree_comparable(_Run(PEDIGREE_METHOD), _Run(PEDIGREE_METHOD),
                               label_a="base year", label_b="current",
                               quantity="the reduction") is None


def test_a_pre_correction_run_cannot_be_subtracted_from_a_corrected_one():
    """An absent marker is not 'unknown' — nothing wrote the field before the
    correction, so it IS the superseded compilation, and the blocker says so."""
    reason = pedigree_comparable(_Run(None), _Run(PEDIGREE_METHOD),
                                 label_a="base year", label_b="current",
                                 quantity="the reduction")
    assert reason is not None
    assert "superseded pedigree compilation" in reason


def test_the_pedigree_gate_is_in_the_one_cross_run_gate_set():
    """Renderers ask cross_run_gates() one question and get every answer; a gate that
    exists but is not wired in protects nothing."""
    import inspect
    from app.services import comparability
    src = inspect.getsource(comparability.cross_run_gates)
    assert "pedigree_comparable" in src
