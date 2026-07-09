import pytest

from app.services.units import convert, are_compatible, UnitConversionError, QuantityError


def test_same_unit_passthrough():
    assert convert(1000, "kWh", "kWh") == 1000.0


def test_mwh_to_kwh():
    assert convert(3.5, "MWh", "kWh") == pytest.approx(3500.0)


def test_miles_to_km():
    assert convert(100, "miles", "km") == pytest.approx(160.934, rel=1e-4)


def test_tonne_to_kg():
    assert convert(2, "tonne", "kg") == pytest.approx(2000.0)


def test_litre_alias_passthrough():
    assert convert(10, "l", "L") == pytest.approx(10.0)


def test_incompatible_dimensions_rejected():
    # Volume -> mass needs a density bridge; must reject, not guess.
    with pytest.raises(UnitConversionError):
        convert(10, "L", "kg")


def test_missing_unit_rejected():
    with pytest.raises(UnitConversionError):
        convert(10, "", "kWh")


def test_unknown_unit_rejected():
    with pytest.raises(UnitConversionError):
        convert(10, "bushels", "kWh")


def test_are_compatible():
    assert are_compatible("MWh", "kWh") is True
    assert are_compatible("L", "kg") is False


# --- Fail-closed hardening (verifier findings) ---

def test_tonne_conversion_still_works():
    assert convert(2, "tonne", "kg") == pytest.approx(2000.0)


def test_bare_ton_rejected_as_ambiguous():
    # pint would treat "ton" as the US short ton (907 kg) -> silent ~9% error.
    with pytest.raises(UnitConversionError):
        convert(1, "ton", "kg")
    with pytest.raises(UnitConversionError):
        convert(1, "tons", "kg")


def test_gallon_rejected_as_ambiguous():
    with pytest.raises(UnitConversionError):
        convert(1, "gallon", "L")


def test_non_finite_quantity_rejected():
    with pytest.raises(QuantityError):
        convert(float("inf"), "kWh", "kWh")
    with pytest.raises(QuantityError):
        convert(float("nan"), "kWh", "kWh")


def test_none_and_nonnumeric_quantity_rejected():
    with pytest.raises(QuantityError):
        convert(None, "kWh", "kWh")
    # Even the same-unit fast path must funnel through validation.
    with pytest.raises(QuantityError):
        convert("abc", "kWh", "kWh")


def test_trailing_punctuation_rejected():
    with pytest.raises(UnitConversionError):
        convert(5, "kWh!", "kWh")
    with pytest.raises(UnitConversionError):
        convert(1.2, "MWh;", "kWh")


def test_pkm_tkm_passthrough():
    assert convert(900, "pkm", "pkm") == 900.0
    assert convert(50, "tkm", "tkm") == 50.0


def test_quantity_error_is_unit_conversion_error():
    # Subclass relationship: broad handlers still catch quantity problems.
    assert issubclass(QuantityError, UnitConversionError)
