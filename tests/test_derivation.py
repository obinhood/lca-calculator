"""Worked calculations, and the rule that makes them worth anything: a derivation must
reproduce the figure the report actually states, or say plainly that it cannot."""
import pytest

from app.reports import derivation as D


def test_a_derivation_that_does_not_add_up_says_so(db):
    """A plausible-looking calculation that does not reach the disclosed number is worse
    than none at all — it manufactures confidence in a figure it does not explain."""
    d = D.total_of("Total", [{"k": "a", "v": 1.0}, {"k": "b", "v": 2.0}],
                   label_key="k", value_key="v", unit="t", stated_value=5.0)
    assert d["reconciles"] is False
    assert d["computed"] == 3.0 and d["reported"] == 5.0
    assert d["difference"] == pytest.approx(-2.0)
    assert "does not fully explain it" in d["reconciliation_error"]
    # ...and the block-level headline surfaces it rather than burying it in one row.
    blk = D.summarise([d])
    assert blk["all_reconcile"] is False
    assert blk["unreconciled"] == ["Total"]
    assert "could not be reconciled" in blk["warning"]


def test_an_unknown_term_poisons_the_sum_rather_than_being_skipped(db):
    """Silently dropping a None yields a smaller number wearing the same label."""
    d = D.total_of("Total", [{"k": "a", "v": 1.0}, {"k": "b", "v": None}],
                   label_key="k", value_key="v", unit="t", stated_value=1.0)
    assert d["computed"] is None
    assert d["reconciles"] is False


def test_display_rounding_is_checked_not_demanded(db):
    """Reports calculate at full precision and publish rounded, which is the correct
    order. Reconciliation must verify the report rounded the RIGHT number, not demand an
    exact match a rounded figure can never give."""
    ok = D.ratio("Intensity", "Numerator", 20730.90 / 1000.0, "Denominator", 12.5,
                 stated_value=round(20730.90 / 1000.0 / 12.5, 6), display_dp=6)
    assert ok["reconciles"] is True
    # A genuinely wrong figure is still caught at the same precision.
    bad = D.ratio("Intensity", "Numerator", 20.0, "Denominator", 12.5,
                  stated_value=2.0, display_dp=6)
    assert bad["reconciles"] is False


def test_alternatives_are_never_summed(db):
    """Scope 2 location and market measure the same electricity two ways. Adding them
    double-counts it; hiding one loses half the required disclosure."""
    a = D.alternatives("Scope 2", [("Location-based", 100.0), ("Market-based", 60.0)],
                       reported=100.0, unit="tCO2e")
    assert a["operation"] == D.ALTERNATIVES
    assert a["reported"] == 100.0
    assert a["computed"] is None          # nothing is combined
    assert a["reconciles"] is True
    assert "Location-based" in a["expression"] and "Market-based" in a["expression"]


def test_a_blocked_calculation_never_gets_a_tick(db):
    """Where the platform has established a sum would be wrong, showing it reconciled
    would certify a known-bad number as reperformed arithmetic."""
    b = D.blocked("Total incl. financed", "Category 15 is double-declared", reported=600.0)
    assert b["operation"] == D.BLOCKED
    assert b["reconciles"] is False       # must not pass the all_reconcile headline
    assert b["blocked_reason"] == "Category 15 is double-declared"
    blk = D.summarise([b])
    assert blk["all_reconcile"] is False
    assert blk["calculations_refused"] == ["Total incl. financed"]
    assert "NO working shown" in blk["warning"]


def test_an_input_is_recorded_as_an_input_not_dressed_in_arithmetic(db):
    s = D.stated("Emission factor", 0.207, unit="kgCO2e/kWh", source="DEFRA 2024 #91")
    assert s["operation"] == D.STATED
    assert s["reconciles"] is True
    assert D.to_lines(D.summarise([s]))[1][1].startswith("(DEFRA 2024 #91")


def test_summing_avoids_accumulated_rounding_drift(db):
    """Naive left-to-right addition of many small terms drifts; fsum does not. A total
    that fails to reconcile against its own terms for arithmetic reasons alone would
    make every real reconciliation failure indistinguishable from noise."""
    rows = [{"k": str(i), "v": 0.1} for i in range(1000)]
    d = D.total_of("Total", rows, label_key="k", value_key="v", stated_value=100.0)
    assert d["reconciles"] is True
    assert sum(r["v"] for r in rows) != 100.0      # the naive sum genuinely drifts


def test_a_long_sum_stays_readable(db):
    rows = [{"k": str(i), "v": 1.0} for i in range(40)]
    d = D.total_of("Total", rows, label_key="k", value_key="v", stated_value=40.0)
    assert d["expression"] == "sum of 40 terms"
    assert len(d["terms"]) == 40                   # the detail is still there


def test_display_rounding_cannot_hide_a_wrong_figure_at_small_magnitudes(db):
    """`display_dp` exists so a 6dp payload is not asked for exact equality, but it is an
    ABSOLUTE decimal test on figures of unbounded magnitude. At 6dp any two values within
    5e-7 matched, so an intensity of 2e-7 published as 0.0 — or sign-flipped — passed."""
    zeroed = D.ratio("Intensity", "Num", 0.2, "Den", 1_000_000.0,
                     stated_value=0.0, display_dp=6)
    assert zeroed["reconciles"] is False
    flipped = D.ratio("Intensity", "Num", 0.2, "Den", 1e6,
                      stated_value=-2e-7, display_dp=6)
    assert flipped["reconciles"] is False
    # ...while a legitimate 6dp rounding of an ordinary figure still passes.
    ok = D.ratio("Intensity", "Num", 20730.9 / 1000, "Den", 12.5,
                 stated_value=round(20730.9 / 1000 / 12.5, 6), display_dp=6)
    assert ok["reconciles"] is True


def test_stated_and_alternatives_are_falsifiable(db):
    """Two of the operations previously could never fail, while the docstring claimed
    every derivation is checked — a gate that cannot fail is a defect."""
    from app.reports.derivation import Derivation
    bogus = D.alternatives("Scope 2", [("Location", 100.0), ("Market", 60.0)],
                           reported=999_999.0)
    assert bogus["reconciles"] is False
    real = D.alternatives("Scope 2", [("Location", 100.0), ("Market", 60.0)],
                          reported=100.0)
    assert real["reconciles"] is True

    d = Derivation("Emission factor", operation=D.STATED)
    d.term("Emission factor", 0.207)
    assert d.build(9.99)["reconciles"] is False
    d2 = Derivation("Emission factor", operation=D.STATED)
    d2.term("Emission factor", 0.207)
    assert d2.build(0.207)["reconciles"] is True


def test_an_absent_stated_value_never_certifies_the_derivations_own_output(db):
    """Defaulting `stated` to `computed` made the block state a figure the report does not
    publish — a run with a NULL total showed '200.0 kgCO2e' as the disclosed value."""
    d = D.total_of("Total", [{"k": "a", "v": 200.0}], label_key="k", value_key="v",
                   stated_value=None)
    assert d["reported"] is None
    assert d["reconciles"] is False
    # Unknown vs unknown is not agreement either.
    u = D.total_of("Total", [{"k": "a", "v": None}], label_key="k", value_key="v")
    assert u["reconciles"] is False


def test_refused_figures_are_not_also_counted_as_unreconciled(db):
    """The UI renders `unreconciled.length`, so listing a refusal in both double-counted
    the very thing the warning already describes separately."""
    blk = D.summarise([
        D.blocked("Refused", "because the platform established the sum is wrong"),
        D.total_of("Broken", [{"k": "a", "v": 1.0}], label_key="k", value_key="v",
                   stated_value=5.0),
    ])
    assert blk["unreconciled"] == ["Broken"]
    assert blk["calculations_refused"] == ["Refused"]
    assert blk["all_reconcile"] is False


def test_the_block_distinguishes_independent_checks_from_recomputations(db):
    """A recomputation shows HOW a figure was built but inherits any corrupted input on
    both sides, so it cannot fail on bad data. Presenting both as 'reperformed' would let
    a green tick vouch for a number nothing verified."""
    indep = D.total_of("Verified", [{"k": "a", "v": 1.0}], label_key="k", value_key="v",
                       stated_value=1.0, independent=True)
    restated = D.total_of("Restated", [{"k": "a", "v": 2.0}], label_key="k",
                          value_key="v", stated_value=2.0)
    blk = D.summarise([indep, restated])
    assert blk["independently_verified"] == ["Verified"]
    assert indep["independent"] is True and restated["independent"] is False
    assert "cannot detect a corrupted input" in blk["note"]


def test_computed_count_excludes_inputs_and_alternative_pairs(db):
    """Neither is reperformed arithmetic, and counting them inflated the badge."""
    blk = D.summarise([
        D.total_of("Sum", [{"k": "a", "v": 1.0}], label_key="k", value_key="v",
                   stated_value=1.0),
        D.stated("Factor", 0.2),
        D.alternatives("Dual", [("A", 1.0), ("B", 2.0)], reported=1.0),
    ])
    assert blk["count"] == 3
    assert blk["computed_count"] == 1


def test_very_small_and_very_large_values_render_without_collapsing_to_zero(db):
    """Rendering 3.5e-07 as '0' would make a worked calculation show terms that do not
    produce its own printed result."""
    assert D._fmt(3.512e-07) == "3.512000e-07"
    assert "e+" in D._fmt(1.5e16)
    assert D._fmt(1234.5) == "1,234.5"
