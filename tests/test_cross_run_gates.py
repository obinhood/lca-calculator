"""No renderer may re-derive a comparability gate that already exists.

This is the structural half of the fix. Wiring the two renderers that had drifted was a
patch; what actually caused the defect was that assembling the gate set was left to each
renderer, so every new framework entry was a fresh chance to forget one — and forgetting
one is invisible, because a missing gate shows up as an EMPTY blocker list, which reads as
"checked and fine".

Three renderers had already drifted before anyone looked: ISO 14064-2 re-implemented the
boundary test as a bare `consolidation_approach !=` comparison (which a divestment leaves
untouched, so a disposal published as a substantiated project reduction), EcoVadis omitted
it entirely while its comment claimed "the same comparability gates" as GRI, and ESRS E1-6,
CDP C6.10, SECR and EcoVadis all published an intensity ratio with no check that the
denominator covered the numerator's period.

So the rule is enforced rather than documented: ask the shared question, or do not ask.
"""
import pathlib
import re

import pytest

REPORTS = pathlib.Path("app/reports")

# export.py renders whatever another renderer already produced — it re-serialises a
# payload and never subtracts two runs itself, so it has nothing to gate.
PASSTHROUGH = {"export.py"}

# sbti.py compares a base run to a current one, but it is tracking a TARGET rather than
# subtracting two totals, and it says so in a different voice: `base_year_recalculation`
# tells you to re-base, where `boundary_comparable` tells a renderer to refuse. It calls
# the same underlying detectors through that wrapper. Listed explicitly so the exemption
# is a decision on the record rather than an omission nobody noticed.
DECLARED_EXEMPT = {"sbti.py"}


def _sources():
    return {p.name: p.read_text() for p in sorted(REPORTS.glob("*.py"))}


def _two_run_renderers(srcs):
    return {n: s for n, s in srcs.items()
            if re.search(r"\b(base_run_id|baseline_run_id)\b", s)
            and n not in PASSTHROUGH}


def test_every_two_run_renderer_asks_the_shared_question():
    srcs = _sources()
    missing = [n for n, s in _two_run_renderers(srcs).items()
               if "cross_run_gates" not in s and n not in DECLARED_EXEMPT]
    assert not missing, (
        f"{missing} subtract or compare two runs without calling "
        f"services.comparability.cross_run_gates. Assembling the gates by hand is how ISO "
        f"14064-2 came to publish a divestment as abatement with an empty blocker list. "
        f"Call the shared helper, or add the file to DECLARED_EXEMPT with the reason.")


def test_no_renderer_re_derives_the_boundary_test():
    """The narrowest and most dangerous re-derivation: it looks like a boundary check and
    passes every divestment."""
    offenders = [n for n, s in _sources().items() if "consolidation_approach !=" in s
                 or "consolidation_approach or None) !=" in s]
    assert not offenders, (
        f"{offenders} compare consolidation_approach directly. That is not a boundary "
        f"test: an entity can be acquired or divested without the approach changing, and "
        f"such a check waves it through. Use services.boundary.boundary_comparable.")


def test_no_renderer_re_derives_the_gwp_vintage_test():
    """Was inlined in three renderers with three different sentences."""
    offenders = []
    for n, s in _two_run_renderers(_sources()).items():
        if n in DECLARED_EXEMPT:
            continue
        for m in re.finditer(r"\.gwp_set\s*!=\s*\w+\.gwp_set", s):
            offenders.append(f"{n}:{s[:m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        f"{offenders} compare two runs' gwp_set inline. cross_run_gates already asks it, "
        f"so an inline copy either duplicates the blocker or silently disagrees with it.")


def test_every_intensity_ratio_checks_its_denominator_period():
    """An intensity ratio divides a period-scoped total by a per-period quantity.

    Both inputs can be individually correct and the ratio still wrong by the ratio of the
    spans — annual revenue over a quarter's emissions is a figure four times too low, and
    nothing downstream can catch it. GRI gated this from the start and four other
    renderers did not, so the SAME run and the SAME denominator made one refuse and four
    publish.
    """
    missing = []
    for n, s in _sources().items():
        if n in PASSTHROUGH:
            continue
        takes_denominator = re.search(
            r"\b(intensity_denominator|net_revenue_millions)\s*:", s)
        if takes_denominator and "denominator_period_comparable" not in s:
            missing.append(n)
    assert not missing, (
        f"{missing} accept an intensity denominator without calling "
        f"services.comparability.denominator_period_comparable — so they cannot tell an "
        f"annual denominator from a quarterly one.")


def test_the_shared_gate_actually_covers_all_four_dimensions():
    """A guard on the callers is worthless if the thing they call has itself been thinned.

    Every renderer now delegates, which means one deletion here silently removes that gate
    from all of them at once — the cost of centralising, paid for by this test.
    """
    src = pathlib.Path("app/services/comparability.py").read_text()
    body = src[src.index("def cross_run_gates"):]
    for gate in ("period_comparable", "boundary_comparable", "gwp_comparable",
                 "residual_mix_comparable"):
        assert gate in body, f"cross_run_gates no longer applies {gate}"
