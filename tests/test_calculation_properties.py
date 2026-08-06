"""Property-based verification: the invariants must hold for THOUSANDS of generated datasets.

The hand-written checks in test_calculation_verification.py assert the invariants for one
carefully-chosen dataset. Hypothesis searches for a dataset where they DON'T hold — random
magnitudes, awkward decimals, tiny and huge values, duplicate categories, empty sets — and
shrinks any counter-example to the smallest failing case.

NOTE on fixtures: Hypothesis re-runs the test body once per generated example, so the
function-scoped `db` fixture cannot be used (its data would accumulate across examples and
every property would fail for the wrong reason). Each example builds its own in-memory
database via `_fresh()`.

NOTE on floats: addition is not associative, so properties about sums are asserted with a
tolerance scaled to the magnitude of the result rather than exact equality. The tolerance is
deliberately tight (1e-9 relative) — loose enough for reordering, far too tight to hide a
real arithmetic error. Mutation-tested: a 1-part-per-million error injected into every line
is caught.

BLIND SPOT, deliberately: the GWP properties read GWP_100 from the module on BOTH sides of the
assertion, so they verify the SHAPE of the arithmetic (linear, homogeneous, additive,
fossil >= biogenic) but cannot detect a wrong CONSTANT — changing AR6 fossil CH4 from 29.8 to
27.9 leaves every property here green. The constant VALUES are pinned separately, as literals
transcribed from the published IPCC tables, in test_calculation_oracles.py (which does catch
that mutation, on 4 tests). Keep both: this file guards the algebra, that one guards the data.
"""
import math

import pytest
from hypothesis import assume, given, settings, strategies as st, HealthCheck
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models  # noqa: F401  (register tables)
from app.models import (EmissionFactor, ActivityRecord, Organisation, EmissionLineItem,
                        MarketInstrument)
from app.services.calc import compute_co2e
from app.services.gwp import co2e_from_gases, GWP_100


# ---------------------------------------------------------------------------------------
# Per-example database + dataset construction.

def _fresh():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


# Categories with a known, stable scope mapping, so generated data exercises all three scopes.
_CATEGORIES = ["electricity", "gas", "diesel", "waste"]
_UNIT_FOR = {"electricity": "kWh", "gas": "kWh", "diesel": "L", "waste": "kg"}

# Magnitudes a real inventory plausibly contains, kept away from float extremes so that a
# failure means a LOGIC error rather than an unavoidable representation limit.
quantities = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
factor_values = st.floats(min_value=0.0, max_value=100.0, allow_nan=False,
                          allow_infinity=False)

rows = st.lists(
    st.tuples(st.sampled_from(_CATEGORIES), quantities, factor_values),
    min_size=1, max_size=12)


def _build(db, dataset, name="PropCo"):
    """Insert the generated dataset; return the independently-summed oracle total.

    `name` must be unique per organisation, so tests that build two datasets in one database
    (e.g. the row-order property) pass distinct names.
    """
    org = Organisation(name=name)
    db.add(org); db.commit(); db.refresh(org)
    oracle = 0.0
    for i, (category, qty, value) in enumerate(dataset):
        f = EmissionFactor(source="P", version="1", geography="GB", year=2024,
                           category=category, subcategory=f"{name}-s{i}",
                           unit=_UNIT_FOR[category],
                           gwp_set="AR6", value=value)
        db.add(f); db.commit(); db.refresh(f)
        db.add(ActivityRecord(organisation_id=org.id, date="2025-06-15", category=category,
                              subcategory="", description="", quantity=qty,
                              unit=_UNIT_FOR[category], geo="GB", factor_id=f.id))
        db.commit()
        oracle += qty * value
    return org, oracle


def _close(a, b, rel=1e-9):
    """Tolerance scaled to magnitude — reordering-safe, error-sensitive."""
    return math.isclose(a, b, rel_tol=rel, abs_tol=max(1e-9, abs(b) * rel))


# Example counts come from the Hypothesis PROFILE registered in conftest (dev / ci / deep),
# so a deep search is `HYPOTHESIS_PROFILE=deep pytest` rather than an edit here.
PROP = settings(deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


# ---------------------------------------------------------------------------------------
# 1. The oracle and conservation invariants, over generated data.

@PROP
@given(dataset=rows)
def test_total_always_equals_independent_recomputation(dataset):
    db = _fresh()
    try:
        org, oracle = _build(db, dataset)
        run = compute_co2e(db, org.id, gwp_set="AR6")
        assert _close(run.total_co2e, oracle), f"{run.total_co2e} != {oracle}"
    finally:
        db.close()


@PROP
@given(dataset=rows)
def test_total_always_equals_sum_of_its_line_items(dataset):
    """The headline figure must never drift from its own audit trail."""
    db = _fresh()
    try:
        org, _ = _build(db, dataset)
        run = compute_co2e(db, org.id, gwp_set="AR6")
        summed = sum(li.co2e for li in db.query(EmissionLineItem).filter(
            EmissionLineItem.run_id == run.id, EmissionLineItem.method == "location").all())
        assert _close(run.total_co2e, summed)
    finally:
        db.close()


@PROP
@given(dataset=rows)
def test_scope_subtotals_always_resum_to_the_total(dataset):
    db = _fresh()
    try:
        org, _ = _build(db, dataset)
        run = compute_co2e(db, org.id, gwp_set="AR6")
        by_scope = {}
        for li in db.query(EmissionLineItem).filter(
                EmissionLineItem.run_id == run.id,
                EmissionLineItem.method == "location").all():
            by_scope[li.scope] = by_scope.get(li.scope, 0.0) + li.co2e
        assert _close(sum(by_scope.values()), run.total_co2e)
    finally:
        db.close()


@PROP
@given(dataset=rows)
def test_total_is_never_negative_for_non_negative_inputs(dataset):
    db = _fresh()
    try:
        org, _ = _build(db, dataset)
        run = compute_co2e(db, org.id, gwp_set="AR6")
        assert run.total_co2e >= 0.0
        assert run.total_co2e_market >= 0.0
        assert math.isfinite(run.total_co2e)
    finally:
        db.close()


# ---------------------------------------------------------------------------------------
# 2. Metamorphic properties over generated data.

@PROP
@given(dataset=rows,
       k=st.floats(min_value=0.001, max_value=1000.0, allow_nan=False, allow_infinity=False))
def test_scaling_all_quantities_scales_the_total(dataset, k):
    """Linearity for ANY dataset and ANY positive scale factor."""
    db = _fresh()
    try:
        org, _ = _build(db, dataset)
        base = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
        for a in db.query(ActivityRecord).filter(
                ActivityRecord.organisation_id == org.id).all():
            a.quantity = a.quantity * k
        db.commit()
        scaled = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
        assert _close(scaled, base * k, rel=1e-8)
    finally:
        db.close()


@PROP
@given(dataset=rows)
def test_order_of_rows_never_changes_the_total(dataset):
    """Two organisations with the same rows in opposite order must agree. The Scope 2
    allocator consumes instruments in row order, so this is a real reproducibility risk."""
    db = _fresh()
    try:
        org_f, oracle_f = _build(db, dataset, name="Forward")
        org_r, oracle_r = _build(db, list(reversed(dataset)), name="Reversed")
        fwd = compute_co2e(db, org_f.id, gwp_set="AR6").total_co2e
        rev = compute_co2e(db, org_r.id, gwp_set="AR6").total_co2e
        assert _close(fwd, rev, rel=1e-8), f"forward {fwd} vs reversed {rev}"
        assert _close(oracle_f, oracle_r, rel=1e-8)
    finally:
        db.close()


@PROP
@given(dataset=rows)
def test_recomputation_is_deterministic(dataset):
    db = _fresh()
    try:
        org, _ = _build(db, dataset)
        a = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
        b = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
        assert a == b        # bit-identical: same inputs, same code path
    finally:
        db.close()


@PROP
@given(dataset=rows,
       extra=st.floats(min_value=0.0, max_value=1e5, allow_nan=False, allow_infinity=False))
def test_adding_a_row_never_decreases_the_total(dataset, extra):
    """Monotonicity: an additional non-negative activity cannot reduce emissions."""
    db = _fresh()
    try:
        org, _ = _build(db, dataset)
        before = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
        f = EmissionFactor(source="P", version="1", geography="GB", year=2024,
                           category="gas", subcategory="extra", unit="kWh",
                           gwp_set="AR6", value=0.184)
        db.add(f); db.commit(); db.refresh(f)
        db.add(ActivityRecord(organisation_id=org.id, date="2025-06-15", category="gas",
                              subcategory="", description="", quantity=extra, unit="kWh",
                              geo="GB", factor_id=f.id))
        db.commit()
        after = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
        assert after >= before - 1e-9
    finally:
        db.close()


@PROP
@given(dataset=rows, idx=st.integers(min_value=0, max_value=11),
       frac=st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False))
def test_splitting_any_row_leaves_the_total_unchanged(dataset, idx, frac):
    """Additivity: chunking the same consumption differently must not move the number."""
    assume(idx < len(dataset))
    db = _fresh()
    try:
        org, _ = _build(db, dataset)
        before = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
        acts = db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == org.id).order_by(ActivityRecord.id).all()
        victim = acts[idx]
        whole = victim.quantity
        victim.quantity = whole * frac
        db.commit()
        db.add(ActivityRecord(organisation_id=org.id, date=victim.date,
                              category=victim.category, subcategory="", description="",
                              quantity=whole * (1.0 - frac), unit=victim.unit, geo="GB",
                              factor_id=victim.factor_id))
        db.commit()
        after = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
        assert _close(after, before, rel=1e-8), f"{after} != {before}"
    finally:
        db.close()


# ---------------------------------------------------------------------------------------
# 3. Market-based Scope 2 bounds, over generated instruments.

@PROP
@given(consumption=st.floats(min_value=0.0, max_value=1e6, allow_nan=False,
                             allow_infinity=False),
       grid=st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False),
       coverage=st.floats(min_value=0.0, max_value=2e6, allow_nan=False,
                          allow_infinity=False),
       rate=st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False))
def test_market_scope2_stays_within_its_physical_bounds(consumption, grid, coverage, rate):
    """For ANY instrument, the market figure must sit between 'all covered at the instrument
    rate' and 'none covered' — never negative, never more than the worse of the two rates
    applied to the whole load. Over-volume instruments must be clamped to consumption."""
    db = _fresh()
    try:
        org = Organisation(name="MktCo")
        db.add(org); db.commit(); db.refresh(org)
        f = EmissionFactor(source="P", version="1", geography="GB", year=2024,
                           category="electricity", subcategory="", unit="kWh",
                           gwp_set="AR6", value=grid)
        db.add(f); db.commit(); db.refresh(f)
        db.add(ActivityRecord(organisation_id=org.id, date="2025-06-15",
                              category="electricity", subcategory="", description="",
                              quantity=consumption, unit="kWh", geo="GB", factor_id=f.id))
        db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                                kg_co2e_per_kwh=rate, coverage_kwh=coverage, gwp_set="AR6"))
        db.commit()

        run = compute_co2e(db, org.id, gwp_set="AR6")
        lo = consumption * min(rate, grid)
        hi = consumption * max(rate, grid)
        assert run.total_co2e_market >= -1e-9
        assert run.total_co2e_market <= hi + 1e-6, (
            f"market {run.total_co2e_market} exceeds the whole load at the worse rate {hi}")
        assert run.total_co2e_market >= lo - 1e-6, (
            f"market {run.total_co2e_market} below the whole load at the better rate {lo}")
    finally:
        db.close()


# ---------------------------------------------------------------------------------------
# 4. GWP recomposition — pure arithmetic, so many more examples.

gas_masses = st.floats(min_value=0.0, max_value=1e4, allow_nan=False, allow_infinity=False)


@settings(deadline=None)
@given(co2=gas_masses, ch4=gas_masses, n2o=gas_masses,
       gwp_set=st.sampled_from(["AR5", "AR6"]))
def test_co2e_is_the_exact_linear_combination_of_gas_masses(co2, ch4, n2o, gwp_set):
    table = GWP_100[gwp_set]
    expected = co2 * table["CO2"] + ch4 * table["CH4_fossil"] + n2o * table["N2O"]
    got = co2e_from_gases({"CO2": co2, "CH4_fossil": ch4, "N2O": n2o}, gwp_set)
    assert _close(got, expected)


@settings(deadline=None)
@given(co2=gas_masses, ch4=gas_masses, n2o=gas_masses,
       k=st.floats(min_value=0.001, max_value=1000.0, allow_nan=False, allow_infinity=False),
       gwp_set=st.sampled_from(["AR5", "AR6"]))
def test_co2e_is_homogeneous_in_the_gas_masses(co2, ch4, n2o, k, gwp_set):
    """Scaling every gas mass by k scales the CO2e by exactly k."""
    base = co2e_from_gases({"CO2": co2, "CH4_fossil": ch4, "N2O": n2o}, gwp_set)
    scaled = co2e_from_gases({"CO2": co2 * k, "CH4_fossil": ch4 * k, "N2O": n2o * k}, gwp_set)
    assert _close(scaled, base * k, rel=1e-8)


@settings(deadline=None)
@given(a=gas_masses, b=gas_masses, gwp_set=st.sampled_from(["AR5", "AR6"]))
def test_co2e_is_additive_across_gas_masses(a, b, gwp_set):
    """f(x+y) == f(x) + f(y) — a split inventory must total the same as a combined one."""
    combined = co2e_from_gases({"CH4_fossil": a + b}, gwp_set)
    apart = (co2e_from_gases({"CH4_fossil": a}, gwp_set)
             + co2e_from_gases({"CH4_fossil": b}, gwp_set))
    assert _close(combined, apart)


@settings(deadline=None)
@given(mass=st.floats(min_value=0.01, max_value=1e4, allow_nan=False, allow_infinity=False))
def test_fossil_methane_always_outweighs_biogenic_methane(mass):
    """Fossil CH4 carries the extra fossil-carbon term in both vintages, for any mass."""
    for s in ("AR5", "AR6"):
        fossil = co2e_from_gases({"CH4_fossil": mass}, s)
        bio = co2e_from_gases({"CH4_biogenic": mass}, s)
        assert fossil >= bio


# ---------------------------------------------------------------------------------------
# 5. Unit conversion equivalence, over generated magnitudes.

@PROP
@given(kwh=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
       value=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False))
def test_mwh_and_kwh_are_interchangeable(kwh, value):
    """The same physical energy expressed in MWh or kWh must give the same emissions."""
    db = _fresh()
    try:
        results = []
        for unit, qty in (("kWh", kwh), ("MWh", kwh / 1000.0)):
            org = Organisation(name=f"U{unit}")
            db.add(org); db.commit(); db.refresh(org)
            f = EmissionFactor(source="P", version="1", geography="GB", year=2024,
                               category="electricity", subcategory=unit, unit="kWh",
                               gwp_set="AR6", value=value)
            db.add(f); db.commit(); db.refresh(f)
            db.add(ActivityRecord(organisation_id=org.id, date="2025-06-15",
                                  category="electricity", subcategory="", description="",
                                  quantity=qty, unit=unit, geo="GB", factor_id=f.id))
            db.commit()
            results.append(compute_co2e(db, org.id, gwp_set="AR6").total_co2e)
        assert _close(results[0], results[1], rel=1e-8)
    finally:
        db.close()
