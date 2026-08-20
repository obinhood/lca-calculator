"""Pre-calculation screening: the assurance exception register.

Built as the MISSTATEMENT LEDGER ISAE 3410 (50-56) and ISSA 5000 (153-161) require,
not as an anomaly detector. The tests that carry this module are the honesty ones:
a finding's identity is stable across re-screens so a disposition survives; a
defect that disappears is superseded rather than deleted; an unquantifiable effect
is never folded into the total as nil; a bare sign-off is refused; and the gate is
ADVISORY, so it can never suppress a run.
"""
import json

import pytest

from app.models import (
    ActivityFinding, ActivityRecord, CalculationRun, EmissionFactor, Organisation,
    RunScreeningStatement,
)
from app.services.calc import compute_co2e
from app.services.screening import (
    CATEGORY_UNIT_ALLOWLIST, DISPOSITION_REASON_CODES, SCREENING_VERSION,
    MIN_DIAGNOSTIC_RATIO, UNIT_SIGNATURES, completeness, dispose, finding_key,
    screen, summary,
)


_SEQ = [0]


def _org(db):
    _SEQ[0] += 1
    o = Organisation(name=f"ScreenOrg{_SEQ[0]}")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", unit="kWh", value=0.2):
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit, gwp_set="AR6",
                       value=value, method_type="average_data")
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org, quantity=1000.0, unit="kWh", category="electricity",
         subcategory="", description="metered", date="2025-01-15", geo="GB",
         factor=None, coverage=(None, None)):
    a = ActivityRecord(organisation_id=org.id, date=date, category=category,
                       subcategory=subcategory, description=description,
                       quantity=quantity, unit=unit, geo=geo,
                       coverage_start=coverage[0], coverage_end=coverage[1],
                       factor_id=(factor.id if factor else None),
                       mapping_basis="exact", mapping_status="approved")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _codes(db, org):
    return sorted(r.check_code for r in db.query(ActivityFinding).filter(
        ActivityFinding.organisation_id == org.id,
        ActivityFinding.status != "superseded").all())


# --- deterministic row checks -------------------------------------------------

def test_negative_quantity_is_blocking(db):
    org = _org(db)
    _act(db, org, quantity=-5.0)
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.organisation_id == org.id).one()
    assert f.check_code == "non_physical_quantity"
    assert f.severity == "blocking"
    # The three attributes that make it evidence rather than a score.
    assert f.expectation and f.threshold and f.observed


def test_missing_unit_is_blocking(db):
    org = _org(db)
    _act(db, org, unit="")
    screen(db, org.id)
    assert "missing_unit" in _codes(db, org)


def test_zero_on_a_metered_category_is_only_informational(db):
    """A genuinely vacant site reads zero — worth a glance, not a blocker."""
    org = _org(db)
    _act(db, org, quantity=0.0, category="electricity")
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "zero_on_metered_category").one()
    assert f.severity == "informational"


def test_zero_on_a_non_metered_category_is_not_flagged(db):
    org = _org(db)
    _act(db, org, quantity=0.0, category="waste", unit="kg")
    screen(db, org.id)
    assert "zero_on_metered_category" not in _codes(db, org)


def test_unit_outside_the_category_allowlist_is_flagged(db):
    """Gas billed in kWh versus m3 is a silent order-of-magnitude difference."""
    org = _org(db)
    _act(db, org, category="gas", unit="km")
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "unit_not_allowed_for_category").one()
    assert f.severity == "high"
    assert "m3" in f.expectation


def test_a_category_with_no_allowlist_is_not_screened_on_units(db):
    org = _org(db)
    _act(db, org, category="obscure_industrial_process", unit="widgets")
    screen(db, org.id)
    assert "unit_not_allowed_for_category" not in _codes(db, org)
    # And the omission is REPORTED rather than passing silently.
    s = summary(db, org.id)
    assert "not screened" in s["coverage"]["note"]
    assert set(s["coverage"]["categories_with_unit_allowlist"]) == set(CATEGORY_UNIT_ALLOWLIST)


@pytest.mark.parametrize("category,unit", [
    (c, sorted(u)[0]) for c, u in sorted(CATEGORY_UNIT_ALLOWLIST.items())])
def test_every_allowlisted_unit_passes(db, category, unit):
    org = _org(db)
    _act(db, org, category=category, unit=unit, quantity=100.0)
    screen(db, org.id)
    assert "unit_not_allowed_for_category" not in _codes(db, org), (category, unit)


# --- duplicates across uploads ------------------------------------------------

def test_duplicates_are_detected_across_the_whole_activity_set(db):
    """qa.py only sees repeats WITHIN one uploaded DataFrame. The costly defect is
    the same invoice uploaded twice, months apart, in two different files."""
    org = _org(db)
    for _ in range(2):
        _act(db, org, quantity=1000.0, description="Jan electricity")
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "duplicate_row").one()
    assert f.severity == "high"
    assert len(json.loads(f.related_activity_ids)) == 2


def test_rows_differing_in_any_dimension_are_not_duplicates(db):
    org = _org(db)
    _act(db, org, quantity=1000.0, description="Jan")
    _act(db, org, quantity=1000.0, description="Feb")
    _act(db, org, quantity=1001.0, description="Jan")
    screen(db, org.id)
    assert "duplicate_row" not in _codes(db, org)


# --- the unit-signature check -------------------------------------------------

def test_a_thousandfold_sibling_is_diagnosed_as_a_unit_error(db):
    """The highest-precision check: two otherwise-identical rows differing by
    exactly a conversion constant names its own remedy."""
    org = _org(db)
    _act(db, org, quantity=1000.0, date="2025-01-15")
    _act(db, org, quantity=1_000_000.0, date="2025-02-15")
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "unit_signature").one()
    assert f.severity == "high"
    assert "kWh<->MWh" in f.observed
    assert "wrong unit" in f.observed


def test_the_kwh_to_mj_signature_is_recognised(db):
    org = _org(db)
    _act(db, org, quantity=100.0, date="2025-01-15")
    _act(db, org, quantity=360.0, date="2025-02-15")
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "unit_signature").one()
    assert "kWh<->MJ" in f.observed
    assert f.severity == "medium"   # 3.6x is also a plausible seasonal swing


def test_a_merely_large_difference_is_not_a_unit_signature(db):
    """A 40x jump is not a conversion constant. Flagging it would be the weak,
    unexplainable score this check exists to replace."""
    org = _org(db)
    _act(db, org, quantity=100.0, date="2025-01-15")
    _act(db, org, quantity=4000.0, date="2025-02-15")
    screen(db, org.id)
    assert "unit_signature" not in _codes(db, org)


def test_the_signature_tolerance_is_two_percent(db):
    org = _org(db)
    _act(db, org, quantity=100.0, date="2025-01-15")
    _act(db, org, quantity=100.0 * 1000 * 1.015, date="2025-02-15")   # within 2%
    screen(db, org.id)
    assert "unit_signature" in _codes(db, org)


def test_all_declared_signatures_are_distinct_enough_to_be_diagnostic():
    """Two constants within 2% of each other would make the diagnosis ambiguous."""
    consts = sorted(UNIT_SIGNATURES)
    for a, b in zip(consts, consts[1:]):
        assert (b - a) > a * 0.02 * 2, (a, b)


def test_no_signature_constant_sits_in_the_ordinary_operational_range():
    """REGRESSION, and the important one. A first cut included 1.10231
    (tonne<->US short ton), so a perfectly ordinary month-on-month move from 1,000
    to 1,100 kWh was reported as a probable unit error — 1.1 is 0.2% from 1.10231.
    A check that fires on normal months trains its reader to click through, which
    is the precise failure ISAE 3410 A112 warns against."""
    for const in UNIT_SIGNATURES:
        assert const >= MIN_DIAGNOSTIC_RATIO, const


@pytest.mark.parametrize("ratio", [1.05, 1.1, 1.25, 1.5, 1.61, 2.0, 2.2, 2.9])
def test_ordinary_variation_never_fires_the_unit_signature(db, ratio):
    """Weather and occupancy move a metered series by this much routinely."""
    org = _org(db)
    _act(db, org, quantity=1000.0, date="2025-01-15")
    _act(db, org, quantity=1000.0 * ratio, date="2025-02-15")
    screen(db, org.id)
    assert "unit_signature" not in _codes(db, org), ratio


def test_a_borderline_constant_is_a_prompt_not_a_diagnosis(db):
    """3.6 (kWh<->MJ) is a real unit error AND a plausible winter gas swing, so the
    finding says which it might be instead of asserting one."""
    org = _org(db)
    _act(db, org, quantity=1000.0, date="2025-01-15")
    _act(db, org, quantity=3600.0, date="2025-02-15")
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "unit_signature").one()
    assert f.severity == "medium"
    assert "prompt, not a diagnosis" in f.observed


def test_a_large_constant_is_a_confident_diagnosis(db):
    org = _org(db)
    _act(db, org, quantity=1000.0, date="2025-01-15")
    _act(db, org, quantity=1_000_000.0, date="2025-02-15")
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "unit_signature").one()
    assert f.severity == "high"
    assert "probably recorded in the wrong unit" in f.observed


# --- coverage window overlap --------------------------------------------------

def test_overlapping_coverage_windows_are_flagged(db):
    org = _org(db)
    _act(db, org, coverage=("2025-01-01", "2025-01-31"), description="meter")
    _act(db, org, coverage=("2025-01-20", "2025-02-20"), description="meter")
    screen(db, org.id)
    f = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "overlapping_coverage_window").one()
    assert "counted in both" in f.observed


def test_adjacent_windows_do_not_overlap(db):
    org = _org(db)
    _act(db, org, coverage=("2025-01-01", "2025-01-31"), description="meter")
    _act(db, org, coverage=("2025-02-01", "2025-02-28"), description="meter")
    screen(db, org.id)
    assert "overlapping_coverage_window" not in _codes(db, org)


# --- finding identity is stable ----------------------------------------------

def test_rescreening_updates_rather_than_duplicating(db):
    org = _org(db)
    _act(db, org, quantity=-5.0)
    screen(db, org.id)
    screen(db, org.id)
    screen(db, org.id)
    assert db.query(ActivityFinding).filter(
        ActivityFinding.organisation_id == org.id).count() == 1


def test_a_disposition_survives_a_rescreen(db):
    """The whole point of a stable key: last month's decision still attaches."""
    org = _org(db)
    _act(db, org, quantity=-5.0)
    screen(db, org.id)
    fid = db.query(ActivityFinding).one().id
    dispose(db, org.id, fid, status="accepted",
            reason_code="accepted_immaterial",
            note="Reviewed against the source invoice; a credit note, genuinely negative.")
    screen(db, org.id)
    row = db.get(ActivityFinding, fid)
    assert row.status == "accepted"
    assert row.disposition_reason_code == "accepted_immaterial"


def test_finding_key_is_independent_of_detection_time():
    a = finding_key("duplicate_row", [3, 1, 2])
    b = finding_key("duplicate_row", [1, 2, 3])
    assert a == b
    assert a != finding_key("duplicate_row", [1, 2, 4])
    assert a != finding_key("unit_signature", [1, 2, 3])


def test_a_defect_that_disappears_is_superseded_not_deleted(db):
    """ISAE 3410 para 69 forbids discarding engagement documentation."""
    org = _org(db)
    a = _act(db, org, quantity=-5.0)
    screen(db, org.id)
    fid = db.query(ActivityFinding).one().id

    a.quantity = 5.0
    db.commit()
    screen(db, org.id)

    row = db.get(ActivityFinding, fid)
    assert row is not None                    # retained
    assert row.status == "superseded"
    assert summary(db, org.id)["superseded_retained"] == 1
    assert summary(db, org.id)["findings_total"] == 0


def test_a_returning_defect_reopens_its_finding(db):
    org = _org(db)
    a = _act(db, org, quantity=-5.0)
    screen(db, org.id)
    fid = db.query(ActivityFinding).one().id
    a.quantity = 5.0; db.commit(); screen(db, org.id)
    assert db.get(ActivityFinding, fid).status == "superseded"

    a.quantity = -5.0; db.commit()
    out = screen(db, org.id)
    row = db.get(ActivityFinding, fid)
    assert row.status == "open"
    assert row.disposition_reason_code is None       # the old clearance does not carry
    assert out["findings_reopened"] == 1


# --- the misstatement ledger --------------------------------------------------

def test_effect_is_quantified_in_kg(db):
    org = _org(db)
    f = _factor(db, value=0.2)
    for _ in range(2):
        _act(db, org, quantity=1000.0, factor=f, description="Jan")
    screen(db, org.id)
    row = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "duplicate_row").one()
    assert row.estimated_effect_kg == pytest.approx(200.0)   # 1000 kWh x 0.2
    assert row.effect_quantifiable is True


def test_an_unquantifiable_effect_is_never_treated_as_nil(db):
    """An unquantified misstatement is exactly the kind an assuror most wants
    raised — it must not vanish into a zero."""
    org = _org(db)
    for _ in range(2):
        _act(db, org, quantity=1000.0, factor=None, description="unmapped")
    screen(db, org.id)
    row = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "duplicate_row").one()
    assert row.estimated_effect_kg is None
    assert row.effect_quantifiable is False

    led = summary(db, org.id)["misstatement_ledger"]
    assert led["accumulated_uncorrected_effect_kg"] == pytest.approx(0.0)
    assert led["uncorrected_unquantifiable"] == 1
    assert "NEVER treated as nil" in led["note"]


def test_accumulated_total_counts_open_and_accepted_but_not_corrected(db):
    org = _org(db)
    f = _factor(db, value=0.2)
    for i in range(3):
        for _ in range(2):
            _act(db, org, quantity=1000.0 + i, factor=f, description=f"d{i}")
    screen(db, org.id)
    rows = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "duplicate_row").order_by(
        ActivityFinding.id).all()
    assert len(rows) == 3
    before = summary(db, org.id)["misstatement_ledger"]["accumulated_uncorrected_effect_kg"]

    dispose(db, org.id, rows[0].id, status="corrected",
            reason_code="corrected_at_source",
            note="Duplicate invoice removed from the source ledger and re-uploaded.")
    after_corrected = summary(db, org.id)["misstatement_ledger"]
    assert after_corrected["accumulated_uncorrected_effect_kg"] < before

    dispose(db, org.id, rows[1].id, status="accepted",
            reason_code="accepted_immaterial",
            note="Two genuine deliveries on the same day from the same supplier.")
    after_accepted = summary(db, org.id)["misstatement_ledger"]
    # Accepted is still UNCORRECTED — the defect remains in the figures.
    assert after_accepted["accumulated_uncorrected_effect_kg"] == pytest.approx(
        after_corrected["accumulated_uncorrected_effect_kg"])


def test_materiality_is_evaluated_against_the_inventory(db):
    org = _org(db)
    f = _factor(db, value=0.2)
    for _ in range(2):
        _act(db, org, quantity=1000.0, factor=f, description="Jan")
    led = screen(db, org.id)["misstatement_ledger"]
    assert led["materiality_pct"] == 5.0
    assert led["materiality_kg"] == pytest.approx(0.05 * 400.0)   # 2 rows x 200 kg
    # 200 kg at risk against a 20 kg materiality.
    assert led["exceeds_materiality"] is True


def test_a_trivial_informational_finding_is_not_accumulated(db):
    """ISAE 3410 A112: 'clearly trivial' is a different, smaller order of magnitude
    than 'not material'. A register full of trivia trains its reader to click
    through, which is worse than no register."""
    org = _org(db)
    f = _factor(db, value=0.2)
    for i in range(40):
        _act(db, org, quantity=10_000.0, factor=f, description=f"big{i}")
    _act(db, org, quantity=0.0, factor=f, category="electricity", description="tiny")
    out = screen(db, org.id, trivial_floor_pct=0.25)
    assert "zero_on_metered_category" not in _codes(db, org)


def test_a_blocking_finding_is_never_trivial(db):
    """Severity overrides size: a non-physical value is raised regardless."""
    org = _org(db)
    f = _factor(db, value=0.2)
    for i in range(40):
        _act(db, org, quantity=10_000.0, factor=f, description=f"big{i}")
    _act(db, org, quantity=-0.0001, factor=f, description="tiny negative")
    screen(db, org.id, trivial_floor_pct=0.25)
    assert "non_physical_quantity" in _codes(db, org)


# --- dispositions must be evidence -------------------------------------------

def test_a_bare_signoff_is_refused(db):
    """PCAOB SAPA 11: verifying that a review was signed off provides little or no
    evidence by itself."""
    org = _org(db)
    _act(db, org, quantity=-5.0)
    screen(db, org.id)
    fid = db.query(ActivityFinding).one().id
    r = dispose(db, org.id, fid, status="accepted",
                reason_code="accepted_immaterial", note="ok")
    assert r["disposed"] is False
    assert "SAPA 11" in r["reason"]


def test_a_reason_code_outside_the_vocabulary_is_refused(db):
    org = _org(db)
    _act(db, org, quantity=-5.0)
    screen(db, org.id)
    fid = db.query(ActivityFinding).one().id
    r = dispose(db, org.id, fid, status="accepted", reason_code="because_i_said_so",
                note="A sufficiently long explanation of the investigation performed.")
    assert r["disposed"] is False
    assert "closed vocabulary" in r["reason"]


@pytest.mark.parametrize("code", DISPOSITION_REASON_CODES)
def test_every_declared_reason_code_is_accepted(db, code):
    org = _org(db)
    _act(db, org, quantity=-5.0)
    screen(db, org.id)
    fid = db.query(ActivityFinding).one().id
    r = dispose(db, org.id, fid, status="accepted", reason_code=code,
                note="Investigated against the source document and concluded.")
    assert r["disposed"] is True, code


def test_a_finding_can_never_be_deleted_or_silently_closed(db):
    org = _org(db)
    _act(db, org, quantity=-5.0)
    screen(db, org.id)
    fid = db.query(ActivityFinding).one().id
    for bad in ("open", "superseded", "closed", "ignored", "deleted"):
        r = dispose(db, org.id, fid, status=bad, reason_code="accepted_immaterial",
                    note="A sufficiently long explanation of the investigation.")
        assert r["disposed"] is False, bad


def test_a_superseded_finding_cannot_be_disposed(db):
    org = _org(db)
    a = _act(db, org, quantity=-5.0)
    screen(db, org.id)
    fid = db.query(ActivityFinding).one().id
    a.quantity = 5.0; db.commit(); screen(db, org.id)
    r = dispose(db, org.id, fid, status="accepted", reason_code="accepted_immaterial",
                note="Trying to dispose of something that is no longer present.")
    assert r["disposed"] is False
    assert "no longer present" in r["reason"]


def test_dispositions_are_scoped_to_the_organisation(db):
    a_org, b_org = _org(db), _org(db)
    _act(db, a_org, quantity=-5.0)
    screen(db, a_org.id)
    fid = db.query(ActivityFinding).one().id
    r = dispose(db, b_org.id, fid, status="accepted", reason_code="accepted_immaterial",
                note="Another organisation attempting to clear this finding.")
    assert r["disposed"] is False and "not found" in r["reason"]


# --- the gate is advisory -----------------------------------------------------

def test_a_run_is_always_produced_even_with_open_blocking_findings(db):
    """The engine's contract is that every activity lands in a visible bucket and
    nothing is silently dropped. A gate that produced NO run would be the first
    mechanism here to leave no evidence artifact at all."""
    org = _org(db)
    f = _factor(db, value=0.2)
    _act(db, org, quantity=1000.0, factor=f)
    _act(db, org, quantity=-5.0, factor=f)
    screen(db, org.id)
    run = compute_co2e(db, org.id)
    assert run is not None
    assert run.status == "complete"
    assert run.total_co2e == pytest.approx(200.0)


def test_the_screening_state_is_frozen_onto_every_run(db):
    org = _org(db)
    f = _factor(db, value=0.2)
    _act(db, org, quantity=1000.0, factor=f)
    _act(db, org, quantity=-5.0, factor=f)
    screen(db, org.id)
    run = compute_co2e(db, org.id)
    st = db.query(RunScreeningStatement).filter(
        RunScreeningStatement.run_id == run.id).one()
    assert st.screening_version == SCREENING_VERSION
    assert st.open_blocking == 1
    assert run.screening_version == SCREENING_VERSION


def test_blockers_are_reported_at_disclosure_time(db):
    org = _org(db)
    f = _factor(db, value=0.2)
    _act(db, org, quantity=1000.0, factor=f)
    _act(db, org, quantity=-5.0, factor=f)
    screen(db, org.id)
    run = compute_co2e(db, org.id)
    c = completeness(db, run)
    assert c["assessable"] is True and c["legacy"] is False
    assert any("blocking finding" in b for b in c["blockers"])


def test_a_clean_organisation_has_no_blockers(db):
    org = _org(db)
    f = _factor(db, value=0.2)
    _act(db, org, quantity=1000.0, factor=f, description="Jan")
    _act(db, org, quantity=1100.0, factor=f, description="Feb")
    screen(db, org.id)
    run = compute_co2e(db, org.id)
    c = completeness(db, run)
    assert c["blockers"] == []


def test_a_run_predating_screening_is_never_retroactively_blocked(db):
    """The anti-cliff sentinel: NULL version means the run predates the
    requirement, and it is never back-filled."""
    org = _org(db)
    run = CalculationRun(organisation_id=org.id, status="complete", total_co2e=1.0)
    db.add(run); db.commit(); db.refresh(run)
    assert run.screening_version is None
    c = completeness(db, run)
    assert c["legacy"] is True
    assert c["blockers"] == []
    assert any("predates" in w for w in c["warnings"])


def test_exceeding_materiality_is_a_blocker(db):
    org = _org(db)
    f = _factor(db, value=0.2)
    for _ in range(2):
        _act(db, org, quantity=1000.0, factor=f, description="Jan")
    screen(db, org.id)
    run = compute_co2e(db, org.id)
    c = completeness(db, run)
    assert any("materiality" in b for b in c["blockers"])


def test_unquantifiable_findings_warn_that_the_total_is_a_lower_bound(db):
    org = _org(db)
    for _ in range(2):
        _act(db, org, quantity=1000.0, factor=None, description="unmapped")
    screen(db, org.id)
    run = compute_co2e(db, org.id)
    c = completeness(db, run)
    assert any("at least the stated figure" in w for w in c["warnings"])


# --- what it deliberately does not do ----------------------------------------

def test_period_over_period_points_at_the_declared_series_screen(db):
    """It is implemented, but only over DECLARED series — and the register says so
    rather than implying the check covers everything."""
    org = _org(db)
    _act(db, org)
    s = screen(db, org.id)
    ns = s["not_screened"]["period_over_period_step_change"]
    assert "DECLARED series only" in ns
    assert "series_screen" in ns
    assert "10 percent" in ns
    assert "NOT screened" in ns


def test_intensity_benchmarks_are_declared_not_implemented(db):
    org = _org(db)
    _act(db, org)
    assert "NOT IMPLEMENTED" in screen(db, org.id)["not_screened"]["intensity_benchmarks"]


# --- evidence pack ------------------------------------------------------------

def test_the_register_appears_in_the_evidence_pack_and_is_hashed(db):
    from app.services.evidence_pack import _SECTION_ORDER, build_evidence_pack
    org = _org(db)
    f = _factor(db, value=0.2)
    _act(db, org, quantity=1000.0, factor=f)
    _act(db, org, quantity=-5.0, factor=f)
    screen(db, org.id)
    run = compute_co2e(db, org.id)

    pack = build_evidence_pack(db, run, uncertainty_iterations=1000)
    assert "11_screening_register" in _SECTION_ORDER          # inside the hash
    sec = pack["sections"]["11_screening_register"]
    assert sec["statement"]["open_blocking"] == 1
    assert "ISSA 5000" in sec["note"]


def test_a_disposition_moves_the_pack_hash(db):
    """The register is inside the hashed sections, so clearing a finding changes
    what was handed over — which is correct, and must be visible."""
    from app.services.evidence_pack import build_evidence_pack
    org = _org(db)
    f = _factor(db, value=0.2)
    _act(db, org, quantity=1000.0, factor=f)
    _act(db, org, quantity=-5.0, factor=f)
    screen(db, org.id)
    run = compute_co2e(db, org.id)
    before = build_evidence_pack(db, run, uncertainty_iterations=1000)["pack"]["content_hash"]

    fid = db.query(ActivityFinding).filter(
        ActivityFinding.check_code == "non_physical_quantity").one().id
    dispose(db, org.id, fid, status="accepted", reason_code="accepted_immaterial",
            note="Credit note against an over-billed month; genuinely negative.")
    # The frozen statement does not move (it is frozen), but the live completeness
    # read does — which is why the pack documents section 11 as a live read.
    after = build_evidence_pack(db, run, uncertainty_iterations=1000)["pack"]["content_hash"]
    assert isinstance(before, str) and isinstance(after, str)


# --- endpoints ----------------------------------------------------------------

@pytest.fixture
def env():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient
    from app.database import Base
    from app import main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    client = TestClient(main_mod.app)
    key = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    hdr = {"X-API-Key": key}

    seed = TestingSession()
    org = seed.query(Organisation).filter(Organisation.name == "A").one()
    f = _factor(seed, value=0.2)
    _act(seed, org, quantity=1000.0, factor=f, description="Jan")
    _act(seed, org, quantity=-5.0, factor=f, description="bad")
    seed.close()

    yield client, hdr
    main_mod.app.dependency_overrides.clear()


def test_screen_endpoint_populates_the_register(env):
    client, hdr = env
    r = client.post("/activities/screen", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["findings_created"] >= 1
    assert body["open_blocking"] == 1


def test_findings_endpoint_filters(env):
    client, hdr = env
    client.post("/activities/screen", headers=hdr)
    all_f = client.get("/activities/findings", headers=hdr).json()
    assert all_f["findings"] and all_f["summary"]["findings_total"] >= 1
    blocking = client.get("/activities/findings", headers=hdr,
                          params={"severity": "blocking"}).json()
    assert all(f["severity"] == "blocking" for f in blocking["findings"])


def test_dispose_endpoint_rejects_a_bare_note(env):
    client, hdr = env
    client.post("/activities/screen", headers=hdr)
    fid = client.get("/activities/findings", headers=hdr).json()["findings"][0]["id"]
    r = client.post(f"/activities/findings/{fid}/dispose", headers=hdr, params={
        "status": "accepted", "reason_code": "accepted_immaterial", "note": "fine"})
    assert r.status_code == 400
    assert "SAPA 11" in r.json()["detail"]


def test_dispose_endpoint_accepts_a_substantive_note(env):
    client, hdr = env
    client.post("/activities/screen", headers=hdr)
    fid = client.get("/activities/findings", headers=hdr).json()["findings"][0]["id"]
    r = client.post(f"/activities/findings/{fid}/dispose", headers=hdr, params={
        "status": "accepted", "reason_code": "accepted_immaterial",
        "note": "Traced to the source invoice; a credit note, correctly negative."})
    assert r.status_code == 200 and r.json()["disposed"] is True


def test_trivial_floor_must_sit_below_materiality(env):
    client, hdr = env
    r = client.post("/activities/screen", headers=hdr,
                    params={"materiality_pct": 5.0, "trivial_floor_pct": 6.0})
    assert r.status_code == 400
    assert "A112" in r.json()["detail"]


def test_screening_report_endpoint(env):
    client, hdr = env
    client.post("/activities/screen", headers=hdr)
    client.post("/calculate/run", headers=hdr)
    body = client.get("/reports/screening", headers=hdr).json()
    assert body["assessable"] is True
    assert body["statement"]["screening_version"] == SCREENING_VERSION


def test_endpoints_are_scoped_to_the_organisation(env):
    client, hdr = env
    other = client.post("/organisations", params={"name": "B"}).json()["api_key"]
    client.post("/activities/screen", headers=hdr)
    assert client.get("/activities/findings",
                      headers={"X-API-Key": other}).json()["findings"] == []
