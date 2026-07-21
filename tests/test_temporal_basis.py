"""GHGP Cats 2/11/12 temporal basis — the acquisition-year / sale-year-lifetime assertion.

The gap this closes: the engine computes activity x factor FOR THE PERIOD, but Cat 2
(capital goods acquired), Cat 11 (use of sold products) and Cat 12 (end-of-life of sold
products) require a different temporal basis. A Cat 11 figure covering one year of use
instead of a 12-year product lifetime is understated ~12x, and the platform previously
only warned.
"""
import json
import pytest

from app.models import (
    Organisation, ActivityRecord, EmissionFactor, Scope3CategoryDeclaration,
)
from app.services.calc import compute_co2e
from app.services.ghgp import (
    scope3_completeness, declarations_fingerprint, fingerprint_scheme_of,
    _fingerprint_v1, TEMPORAL_BASES, TEMPORAL_BASIS_VERSION, temporal_bases_for,
)
from tests.scope3_util import make_period, screen_complete


def _org(db, name="Maker"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit="kWh", value=1.0):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit, gwp_set="AR6",
                       value=value, lca_boundary=None)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id, category, quantity, unit="kWh", ghgp_category=None):
    a = ActivityRecord(organisation_id=org_id, date="2025-03-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id, scope="3", ghgp_category=ghgp_category)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _cat11_run(db, kg, **basis):
    """A period-scoped run with `kg` of Cat 11 emissions and a Cat 11 declaration."""
    org = _org(db)
    p = make_period(db, org.id)
    _act(db, org.id, _factor(db, "use_phase", "kWh", 1.0).id, "use_phase", kg,
         ghgp_category=11)
    run0 = compute_co2e(db, org.id, reporting_period_id=p.id)
    screen_complete(db, org.id, p.id, run0)
    d = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id, category=11).first()
    d.status = "included"
    d.method_description = "Units sold x expected lifetime x measured per-unit annual use."
    for k, v in basis.items():
        setattr(d, k, v)
    db.commit()
    return org, p, compute_co2e(db, org.id, reporting_period_id=p.id)


# --- The fingerprint split: the highest-risk part of the change --------------------

def test_v1_fingerprint_is_pinned_byte_for_byte():
    """GOLDEN VECTOR. Every run filed before s3decl-v2 stored a digest from exactly this
    part-string. If v1 ever drifts, B10 turns every filed run into a FALSE forgery
    accusation — worse than any blocker in this change."""
    class D:
        def __init__(self, **k):
            for a, b in k.items():
                setattr(self, a, b)
    ds = [D(category=11, status="included", justification="x",
            screening_estimate_tco2e=None, materiality_threshold_pct=None,
            screened_at="2025-06-30", temporal_basis=None, basis_units_sold=None,
            basis_lifetime_years=None, basis_per_unit_annual_co2e_kg=None)]
    assert _fingerprint_v1(ds) == (
        "s3decl-v1:02af5114371ace942398b72637e4a9f01ce1b6141762e8df76dd35b63c17c587")
    # v2 differs (the basis fields are in the hash, so a post-filing edit is detectable)
    assert declarations_fingerprint(ds).startswith("s3decl-v2:")
    assert declarations_fingerprint(ds, "s3decl-v1") == _fingerprint_v1(ds)


def test_fingerprint_scheme_is_read_from_the_stored_digest():
    assert fingerprint_scheme_of("s3decl-v1:abc") == "s3decl-v1"
    assert fingerprint_scheme_of("s3decl-v2:abc") == "s3decl-v2"
    assert fingerprint_scheme_of(None) == "s3decl-v1"        # pre-versioning digests
    assert fingerprint_scheme_of("garbage") == "s3decl-v1"


def test_a_run_filed_under_v1_is_never_falsely_accused_of_being_edited(db):
    """B10 must re-derive under the version the RUN recorded. A raw string compare against
    a freshly-computed v2 digest would flag EVERY pre-existing run as forged."""
    org, p, run = _cat11_run(db, 0.0)
    live = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id).all()
    run.scope3_declaration_fingerprint = declarations_fingerprint(live, "s3decl-v1")
    db.commit()
    assert not any("EDITED" in b for b in scope3_completeness(db, run)["blockers"])


def test_editing_the_declared_lifetime_after_filing_is_detected(db):
    """The reason the basis fields must be IN the hash: otherwise a preparer could file a
    conforming assertion and then halve the declared lifetime undetected."""
    org, p, run = _cat11_run(
        db, 1200.0, temporal_basis="sold_units_full_lifetime", basis_units_sold=100.0,
        basis_lifetime_years=12.0, basis_per_unit_annual_co2e_kg=1.0)
    assert not any("EDITED" in b for b in scope3_completeness(db, run)["blockers"])
    d = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id, category=11).first()
    d.basis_lifetime_years = 6.0            # halve it AFTER the run froze the screen
    db.commit()
    assert any("EDITED" in b for b in scope3_completeness(db, run)["blockers"])


# --- The anti-cliff mechanism ------------------------------------------------------

def test_a_run_predating_the_requirement_is_warned_never_blocked(db):
    """THE CLIFF GUARD. scope3_completeness is re-evaluated at RENDER time on already-filed
    runs; blocking them would gate every historical filing behind a question nobody asked."""
    org, p, run = _cat11_run(db, 1200.0)
    run.scope3_temporal_basis_version = None       # a run frozen before the requirement
    db.commit()
    gate = scope3_completeness(db, run)
    assert not any("TEMPORAL BASIS" in b for b in gate["blockers"])
    assert any("predates the temporal-basis requirement" in w for w in gate["warnings"])


# --- The gate ----------------------------------------------------------------------

def test_unstated_basis_blocks(db):
    org, p, run = _cat11_run(db, 1200.0)
    assert run.scope3_temporal_basis_version == TEMPORAL_BASIS_VERSION
    gate = scope3_completeness(db, run)
    assert any("does not state its TEMPORAL BASIS" in b for b in gate["blockers"])


def test_a_non_conforming_basis_blocks_and_names_both_exits(db):
    """The headline case: one year of use filed as Cat 11."""
    org, p, run = _cat11_run(db, 1200.0, temporal_basis="single_period_of_use")
    b = " ".join(scope3_completeness(db, run)["blockers"])
    assert "NON-CONFORMING temporal basis 'single_period_of_use'" in b
    assert "sold_units_full_lifetime" in b       # the conforming exit
    assert "not_measured" in b                    # the honest-gap exit


def test_a_token_from_another_category_blocks(db):
    org, p, run = _cat11_run(db, 1200.0, temporal_basis="acquisition_year_full")  # Cat 2's
    assert any("not part of this category's vocabulary" in b
               for b in scope3_completeness(db, run)["blockers"])


def test_dissipative_seller_is_not_false_blocked(db):
    """A fuel / feedstock / chemical seller's Cat 11 has NO lifetime — quantity sold IS
    quantity used. Without this token the design would false-block an entire industry."""
    org, p, run = _cat11_run(db, 1200.0, temporal_basis="sold_quantity_consumed_in_use")
    gate = scope3_completeness(db, run)
    assert gate["blockers"] == []


def test_the_arithmetic_claim_must_carry_its_numbers(db):
    org, p, run = _cat11_run(db, 1200.0, temporal_basis="sold_units_full_lifetime")
    b = " ".join(scope3_completeness(db, run)["blockers"])
    assert "is an arithmetic claim" in b
    for f in ("basis_units_sold", "basis_lifetime_years", "basis_per_unit_annual_co2e_kg"):
        assert f in b


def test_a_conforming_lifetime_figure_passes(db):
    # 100 units x 12 years x 1.0 kg/unit/yr = 1200 kg filed -> implied lifetime 12.0
    org, p, run = _cat11_run(
        db, 1200.0, temporal_basis="sold_units_full_lifetime", basis_units_sold=100.0,
        basis_lifetime_years=12.0, basis_per_unit_annual_co2e_kg=1.0)
    assert scope3_completeness(db, run)["blockers"] == []


def test_one_year_of_a_twelve_year_lifetime_is_caught_with_the_signature_message(db):
    """The exact understatement this phase exists to catch, and the message names it."""
    org, p, run = _cat11_run(
        db, 100.0, temporal_basis="sold_units_full_lifetime", basis_units_sold=100.0,
        basis_lifetime_years=12.0, basis_per_unit_annual_co2e_kg=1.0)
    b = " ".join(scope3_completeness(db, run)["blockers"])
    assert "precisely ONE YEAR" in b and "12" in b and "understated by about 12x" in b


def test_the_check_is_asymmetric_and_coarse(db):
    """Understatement is privileged. A real multi-SKU portfolio's weighted averages drift
    well beyond 10% from a family-level sum and must NOT be blocked for it; nothing honest
    produces a 2x shortfall."""
    # 30% short of the declared lifetime -> tolerated (no block).
    _o, _p, run = _cat11_run(
        db, 840.0, temporal_basis="sold_units_full_lifetime", basis_units_sold=100.0,
        basis_lifetime_years=12.0, basis_per_unit_annual_co2e_kg=1.0)
    assert scope3_completeness(db, run)["blockers"] == []


def test_overstatement_warns_but_never_blocks(db):
    # implied 30 years against 12 declared -> possible double count, but not understatement
    _o, _p, run = _cat11_run(
        db, 3000.0, temporal_basis="sold_units_full_lifetime", basis_units_sold=100.0,
        basis_lifetime_years=12.0, basis_per_unit_annual_co2e_kg=1.0)
    gate = scope3_completeness(db, run)
    assert gate["blockers"] == []
    assert any("check for double counting" in w for w in gate["warnings"])


# --- Vocabulary shape ---------------------------------------------------------------

def test_cat2_has_no_lifetime_vocabulary():
    """Cat 2's conformant basis STRUCTURALLY FITS the period model (goods acquired x
    cradle-to-gate IS activity x factor), so offering a lifetime token there would be
    nonsense a preparer could pick. Its only failure mode is deliberate amortisation."""
    assert "sold_units_full_lifetime" not in temporal_bases_for(2)
    assert temporal_bases_for(2)["acquisition_year_full"][0] is True
    assert temporal_bases_for(2)["depreciated_or_amortised"][0] is False
    # and exactly one token platform-wide entails the lifetime numbers
    entailing = [(c, t) for c, toks in TEMPORAL_BASES.items()
                 for t, v in toks.items() if v[1]]
    assert entailing == [(11, "sold_units_full_lifetime")]


def test_only_the_sale_year_categories_have_a_vocabulary():
    assert set(TEMPORAL_BASES) == {2, 11, 12}
    for c in (1, 3, 4, 5, 6, 7, 8, 9, 10, 13, 14, 15):
        assert temporal_bases_for(c) == {}


# --- Adversarial-review regressions -------------------------------------------------

def _jv_cat11_run(db, gross_kg, share_pct, **basis):
    """A Cat 11 run whose sales sit in a fractionally-held entity, so the frozen line is
    CONSOLIDATED (gross x share) while the preparer's assertion is a PHYSICAL claim."""
    from app.models import ReportingEntity
    org = Organisation(name=f"Group{share_pct}", consolidation_approach="equity_share",
                       consolidation_approach_reason="Equity share reflects our economic "
                                                     "interest in the joint venture.")
    db.add(org); db.commit(); db.refresh(org)
    jv = ReportingEntity(organisation_id=org.id, name="JV",
                         accounting_category="joint_venture_incorporated",
                         equity_share_pct=share_pct, joint_financial_control=True,
                         in_consolidated_accounting_group=False,
                         equity_share_basis="Per the JV agreement.")
    db.add(jv); db.commit(); db.refresh(jv)
    p = make_period(db, org.id)
    a = _act(db, org.id, _factor(db, "use_phase", "kWh", 1.0).id, "use_phase", gross_kg,
             ghgp_category=11)
    a.entity_id = jv.id
    db.commit()
    run0 = compute_co2e(db, org.id, reporting_period_id=p.id)
    screen_complete(db, org.id, p.id, run0)
    d = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id, category=11).first()
    d.status = "included"
    d.method_description = "Units sold x expected lifetime x measured per-unit annual use."
    for k, v in basis.items():
        setattr(d, k, v)
    db.commit()
    return org, p, compute_co2e(db, org.id, reporting_period_id=p.id)


def test_a_truthful_assertion_through_a_40pct_jv_is_not_falsely_accused(db):
    """Regression (review, HIGH): every EmissionLineItem.co2e is stored CONSOLIDATED
    (gross x share), but the assertion is an UNWEIGHTED physical claim about units sold.
    Dividing the weighted figure by the unweighted product gave implied == share x declared,
    so ANY org selling through a sub-50% entity was blocked as 'understated by more than 2x'
    — with no honest exit, since every alternative token would be a false statement."""
    # gross 1,200,000 kg == 100 units x 12 yr x 1000 kg/unit/yr. Consolidated at 40%.
    org, p, run = _jv_cat11_run(
        db, 1_200_000.0, 40.0, temporal_basis="sold_units_full_lifetime",
        basis_units_sold=100.0, basis_lifetime_years=12.0,
        basis_per_unit_annual_co2e_kg=1000.0)
    assert run.total_co2e == pytest.approx(480_000.0)          # correctly consolidated
    gate = scope3_completeness(db, run)
    assert not any("UNDERSTATEMENT" in b or "ONE YEAR" in b for b in gate["blockers"])


def test_the_signature_message_cannot_fire_from_a_share_weight(db):
    """At a share near 1/lifetime the old bug produced the MAXIMALLY specific false
    accusation — 'equals precisely ONE YEAR of the 12-year lifetime' — of the exact error
    the preparer had not made."""
    org, p, run = _jv_cat11_run(
        db, 1_200_000.0, 8.3333, temporal_basis="sold_units_full_lifetime",
        basis_units_sold=100.0, basis_lifetime_years=12.0,
        basis_per_unit_annual_co2e_kg=1000.0)
    assert not any("ONE YEAR" in b for b in scope3_completeness(db, run)["blockers"])


def test_a_genuine_understatement_is_still_caught_through_a_jv(db):
    """The gross basis must not become a way to hide: a real one-year figure still blocks
    even when the selling entity is fractionally held."""
    # gross 100,000 kg where the lifetime claim implies 1,200,000 -> ~1 year.
    org, p, run = _jv_cat11_run(
        db, 100_000.0, 40.0, temporal_basis="sold_units_full_lifetime",
        basis_units_sold=100.0, basis_lifetime_years=12.0,
        basis_per_unit_annual_co2e_kg=1000.0)
    assert any("ONE YEAR" in b for b in scope3_completeness(db, run)["blockers"])


def test_a_zero_figure_under_a_positive_lifetime_claim_blocks(db):
    """Regression (review, HIGH): the old `if filed_kg > 0` guard skipped the arithmetic
    exactly when the figure was zero — making the MAXIMUM possible understatement (100%)
    the one case the check never saw. A line that computes to 0.0 kg still has a line, so
    B8 does not fire and nothing else caught it."""
    org, p, run = _cat11_run(
        db, 0.0, temporal_basis="sold_units_full_lifetime", basis_units_sold=100.0,
        basis_lifetime_years=12.0, basis_per_unit_annual_co2e_kg=1.0)
    b = " ".join(scope3_completeness(db, run)["blockers"])
    assert "UNDERSTATEMENT" in b or "ONE YEAR" in b


def test_denominator_underflow_blocks_instead_of_raising_at_render_time(db):
    """Regression (review, LOW): both operands are validated individually, but their PRODUCT
    can underflow to 0.0 — and the divide happens at RENDER time on an already-filed run, so
    a ZeroDivisionError is an HTTP 500 on every renderer, worse than any blocker."""
    org, p, run = _cat11_run(
        db, 1200.0, temporal_basis="sold_units_full_lifetime", basis_units_sold=1e-200,
        basis_lifetime_years=12.0, basis_per_unit_annual_co2e_kg=1e-200)
    gate = scope3_completeness(db, run)                       # must not raise
    assert any("not a usable number" in b for b in gate["blockers"])


def test_entailment_check_is_null_safe_in_the_database(db):
    """Regression (review, LOW): `temporal_basis = 'x'` evaluates to NULL when the column is
    NULL, and SQLite treats a NULL CHECK result as PASS — so the naive constraint did not
    enforce what it documented. The API already refused this; the CHECK is defence in depth
    and must actually defend."""
    import sqlalchemy.exc
    org = _org(db); p = make_period(db, org.id)
    db.add(Scope3CategoryDeclaration(
        organisation_id=org.id, reporting_period_id=p.id, category=11,
        status="not_applicable", justification="The entity sells no products with a use phase.",
        screened_at="2025-06-30", temporal_basis=None, basis_units_sold=5.0))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.commit()
    db.rollback()
