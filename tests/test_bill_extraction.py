"""Utility-bill extraction: the validation layer.

The tests encode the traps that silently change a reported tonne — a scanned PDF
returning empty text rather than an error, kVArh sitting beside kWh, the CT/PT
multiplier, per-register read quality, and the catch-up bill that restates rather
than adds.
"""
import pytest

from app.services.bill_extraction import (
    ACCEPTED_UNITS, EMBEDDED_INVOICE_ATTACHMENTS, ESTIMATED_QUALITIES,
    PROVENANCE_TIERS, READ_QUALITY, REJECTED_UNITS,
    TEXT_LAYER_MIN_CHARS_PER_PAGE, consumption_from_registers, document_hash,
    gas_volume_to_kwh, mpan_valid, read_quality, reconcile, supersession,
    to_activity_rows, triage, unit_verdict,
)


# --- triage -------------------------------------------------------------------

def test_a_scanned_pdf_is_detected_rather_than_returning_a_silent_zero():
    """A scanned PDF returns EMPTY text, not an error — the zero flows straight
    through to a reported tonne."""
    t = triage(["", "", ""])
    assert t["route"] == "ocr"
    assert "silent zero" in t["note"]


def test_a_broken_glyph_map_routes_to_ocr():
    """Worse than a scan: it returns garbage that parses."""
    page = "�" * 50 + "x" * 200
    t = triage([page])
    assert t["route"] == "ocr"
    assert "garbage that parses" in t["note"]


def test_a_real_text_layer_is_used_directly():
    t = triage(["x" * 500])
    assert t["route"] == "text_layer" and t["provenance_tier"] == "text_layer"
    assert t["note"] is None


@pytest.mark.parametrize("name", EMBEDDED_INVOICE_ATTACHMENTS)
def test_an_embedded_structured_invoice_wins_over_ocr(name):
    t = triage([""], attachments={name: b"<xml/>"})
    assert t["route"] == "embedded_xml"
    assert "never OCR" in t["note"]


def test_the_char_threshold_is_per_page():
    assert triage(["x" * (TEXT_LAYER_MIN_CHARS_PER_PAGE + 1)])["route"] == "text_layer"
    assert triage(["x" * (TEXT_LAYER_MIN_CHARS_PER_PAGE - 1)])["route"] == "ocr"


# --- units --------------------------------------------------------------------

@pytest.mark.parametrize("unit", sorted(REJECTED_UNITS))
def test_reactive_and_demand_units_are_rejected(unit):
    """The commonest false positive in Scope 2 extraction — same table, numeric
    value, k-prefixed unit, no emission factor."""
    v = unit_verdict(unit)
    assert v["accepted"] is False
    assert "false positive" in v["note"]


@pytest.mark.parametrize("unit", sorted(ACCEPTED_UNITS))
def test_real_activity_units_are_accepted(unit):
    assert unit_verdict(unit)["accepted"] is True


def test_a_missing_unit_is_never_guessed():
    assert unit_verdict(None)["accepted"] is False
    assert "never guessed" in unit_verdict("")["reason"]


def test_an_unrecognised_unit_goes_to_review():
    v = unit_verdict("furlongs")
    assert v["accepted"] is False and "review" in v["reason"]


# --- MPAN ---------------------------------------------------------------------

def test_the_mpan_core_is_thirteen_digits():
    r = mpan_valid("12345678")
    assert r["valid"] is False
    assert "bottom line" in r["reason"]


def test_a_valid_mpan_passes_its_check_digit():
    # Construct a core whose check digit is correct by computation.
    from app.services.bill_extraction import MPAN_PRIMES
    body = "100012345678"
    check = (sum(int(d) * p for d, p in zip(body, MPAN_PRIMES)) % 11) % 10
    assert mpan_valid(body + str(check))["valid"] is True


def test_a_transposed_digit_fails_the_check():
    from app.services.bill_extraction import MPAN_PRIMES
    body = "100012345678"
    check = (sum(int(d) * p for d, p in zip(body, MPAN_PRIMES)) % 11) % 10
    wrong = (check + 1) % 10
    assert mpan_valid(body + str(wrong))["valid"] is False


# --- registers ----------------------------------------------------------------

def test_the_multiplier_is_applied():
    """A 200:5 CT multiplies by 40. Missing it under-reports by 40x."""
    r = consumption_from_registers(1000.0, 1500.0, multiplier=40.0)
    assert r["consumption"] == pytest.approx(20000.0)
    assert r["register_difference"] == pytest.approx(500.0)


def test_a_unit_multiplier_is_flagged_for_confirmation():
    r = consumption_from_registers(1000.0, 1500.0)
    assert "NOT the consumption" in r["multiplier_note"]


def test_rollover_is_handled_rather_than_going_negative():
    r = consumption_from_registers(99500.0, 200.0, dial_count=5)
    assert r["rollover_applied"] is True
    assert r["consumption"] == pytest.approx(700.0)
    assert "rolled over" in r["rollover_note"]


def test_a_non_finite_reading_refuses():
    assert consumption_from_registers(float("nan"), 1.0)["determinable"] is False
    assert consumption_from_registers(1.0, 2.0, multiplier=0)["determinable"] is False


# --- gas ----------------------------------------------------------------------

def test_gas_conversion_uses_the_bills_own_calorific_value():
    r = gas_volume_to_kwh(1000.0, 39.5)
    assert r["determinable"] is True
    assert r["kwh"] == pytest.approx(1000.0 * 1.02264 * 39.5 / 3.6)
    assert r["cv_basis"] == "gross"
    assert "GROSS-CV" in r["note"]


def test_a_missing_calorific_value_refuses_rather_than_defaulting():
    r = gas_volume_to_kwh(1000.0, 0)
    assert r["determinable"] is False
    assert "never a default" in r["reason"]


def test_the_cv_basis_is_constrained():
    assert gas_volume_to_kwh(1.0, 39.5, cv_basis="approximate")["determinable"] is False


# --- read quality --------------------------------------------------------------

@pytest.mark.parametrize("code", sorted(READ_QUALITY))
def test_every_espi_quality_code_is_understood(code):
    q = read_quality(code)
    assert q["known"] is True and q["label"]


@pytest.mark.parametrize("code", sorted(ESTIMATED_QUALITIES))
def test_estimated_codes_derive_is_estimated(code):
    q = read_quality(code)
    assert q["is_estimated"] is True and q["is_actual"] is False


def test_a_valid_read_is_actual_and_not_estimated():
    q = read_quality(0)
    assert q["is_estimated"] is False and q["is_actual"] is True


def test_an_absent_quality_is_unknown_not_actual():
    """Unknown is never a yes."""
    q = read_quality(None)
    assert q["known"] is False
    assert q["is_actual"] is None
    assert "not the same as actual" in q["note"]


def test_is_estimated_is_derived_never_stored():
    assert "derived from the quality code" in read_quality(8)["note"]


# --- reconciliation -----------------------------------------------------------

def _bill(**over):
    b = {
        "registers": [{"register_id": "R1", "consumption": 6000.0, "unit": "kWh",
                       "read_quality": 0},
                      {"register_id": "R2", "consumption": 4000.0, "unit": "kWh",
                       "read_quality": 8}],
        "total_consumption": 10000.0, "unit_rate": 0.25,
        "standing_charge": 0.5, "days": 31,
        "subtotal": 10000.0 * 0.25 + 0.5 * 31,
        "vat": 100.0,
        "total": 10000.0 * 0.25 + 0.5 * 31 + 100.0,
        "period_start": "2027-01-01", "period_end": "2027-01-31",
    }
    b.update(over)
    return b


def test_a_correct_bill_reconciles():
    r = reconcile(_bill())
    assert r["reconciles"] is True and r["failed"] == 0


def test_registers_that_do_not_sum_are_caught():
    r = reconcile(_bill(total_consumption=12000.0))
    assert r["reconciles"] is False
    assert any(c["check"] == "registers_sum_to_total" and c["result"] == "fail"
               for c in r["checks"])


def test_a_wrong_day_count_is_caught():
    r = reconcile(_bill(days=28))
    assert any(c["check"] == "period_length_matches_day_count"
               and c["result"] == "fail" for c in r["checks"])


def test_vat_arithmetic_is_checked():
    r = reconcile(_bill(total=99.0))
    assert any(c["check"] == "subtotal_plus_vat_equals_total"
               and c["result"] == "fail" for c in r["checks"])


def test_a_missing_input_is_not_assessable_rather_than_a_pass():
    """not_assessable is not a pass — it means the relation's inputs were absent."""
    b = _bill()
    b.pop("unit_rate")
    r = reconcile(b)
    assert r["not_assessable"] >= 1
    assert r["trustworthy"] is False
    assert "not a pass" in r["note"]


def test_gas_volume_is_reconciled_against_stated_kwh():
    kwh = 1000.0 * 1.02264 * 39.5 / 3.6
    good = reconcile(_bill(registers=[{"register_id": "R1", "consumption": kwh,
                                       "unit": "kWh", "read_quality": 0}],
                           total_consumption=kwh, gas_volume_m3=1000.0,
                           calorific_value=39.5,
                           subtotal=kwh * 0.25 + 0.5 * 31,
                           total=kwh * 0.25 + 0.5 * 31 + 100.0))
    assert any(c["check"] == "gas_volume_converts_to_stated_kwh"
               and c["result"] == "pass" for c in good["checks"])


# --- supersession -------------------------------------------------------------

def test_a_catch_up_bill_supersedes_rather_than_adds():
    """Adding double counts; overwriting destroys the corrected-alongside-original
    record SECR requires."""
    prev = {"period_start": "2027-01-01", "period_end": "2027-03-31",
            "total_consumption": 9000.0, "read_quality": 8}
    catch = {"period_start": "2027-01-01", "period_end": "2027-06-30",
             "total_consumption": 21000.0, "read_quality": 0}
    s = supersession(prev, catch)
    assert s["supersedes"] is True
    assert s["superseded_period"]["consumption"] == 9000.0
    assert "double counts" in s["note"]


def test_a_non_overlapping_bill_is_not_a_restatement():
    prev = {"period_start": "2027-01-01", "period_end": "2027-03-31"}
    nxt = {"period_start": "2027-04-01", "period_end": "2027-06-30"}
    assert supersession(prev, nxt)["supersedes"] is False


# --- activity rows ------------------------------------------------------------

def test_the_grain_is_per_register_not_per_bill():
    out = to_activity_rows(_bill(), doc_hash="abc", provenance_tier="text_layer")
    assert len(out["rows"]) == 2
    assert out["grain"].startswith("one row per supply point per register")


def test_every_row_carries_the_document_hash_and_provenance_tier():
    out = to_activity_rows(_bill(), doc_hash="abc", provenance_tier="ocr")
    for r in out["rows"]:
        assert r["source_document_hash"] == "abc"
        assert r["provenance_tier"] == "ocr"


def test_a_reactive_register_is_rejected_not_emitted():
    b = _bill()
    b["registers"].append({"register_id": "R3", "consumption": 500.0,
                           "unit": "kVArh", "read_quality": 0})
    out = to_activity_rows(b, doc_hash="abc", provenance_tier="text_layer")
    assert len(out["rows"]) == 2
    assert out["rejected_registers"][0]["unit"] == "kVArh"


def test_read_quality_travels_per_row():
    out = to_activity_rows(_bill(), doc_hash="abc", provenance_tier="text_layer")
    qualities = [r["read_quality"]["is_estimated"] for r in out["rows"]]
    assert qualities == [False, True]      # one actual, one estimated, same bill


def test_the_reconciliation_verdict_travels_with_the_rows():
    out = to_activity_rows(_bill(total_consumption=99.0), doc_hash="abc",
                           provenance_tier="text_layer")
    assert out["reconciliation"]["reconciles"] is False
    assert all(r["reconciles"] is False for r in out["rows"])


def test_rows_are_suggestions_not_inventory():
    out = to_activity_rows(_bill(), doc_hash="abc", provenance_tier="text_layer")
    assert "SUGGESTIONS" in out["note"] and "unreviewed" in out["note"]


@pytest.mark.parametrize("tier", PROVENANCE_TIERS)
def test_every_provenance_tier_is_accepted(tier):
    assert to_activity_rows(_bill(), doc_hash="a", provenance_tier=tier)["rows"]


def test_an_unknown_provenance_tier_is_refused():
    out = to_activity_rows(_bill(), doc_hash="a", provenance_tier="vibes")
    assert out["rows"] == [] and "provenance_tier" in out["reason"]


def test_the_document_hash_is_a_full_sha256():
    h = document_hash(b"%PDF-1.7 ...")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
