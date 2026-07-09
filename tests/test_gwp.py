import pytest

from app.services.gwp import gwp, co2e_from_gases, UnknownGas, UnknownGwpSet


def test_ar5_values():
    assert gwp("CO2", "AR5") == 1.0
    assert gwp("CH4", "AR5") == 28.0
    assert gwp("N2O", "AR5") == 265.0


def test_ar6_values():
    assert gwp("N2O", "AR6") == 273.0
    assert gwp("CH4_fossil", "AR6") == 29.8
    # IPCC AR6 Table 7.15 "methane, non-fossil" is 27.0 (not 27.2).
    assert gwp("CH4_biogenic", "AR6") == 27.0


def test_ar5_differs_from_ar6():
    # The switch must actually change the number (Gap 2).
    assert gwp("CH4", "AR5") != gwp("CH4", "AR6")
    assert gwp("N2O", "AR5") != gwp("N2O", "AR6")


def test_co2e_from_gases():
    # 1 kg CO2 + 1 kg fossil CH4 (AR6 = 29.8) = 30.8 kg CO2e
    assert co2e_from_gases({"CO2": 1.0, "CH4_fossil": 1.0}, "AR6") == pytest.approx(30.8)


def test_co2e_switches_with_gwp_set():
    masses = {"CH4": 1.0}
    assert co2e_from_gases(masses, "AR5") == pytest.approx(28.0)
    assert co2e_from_gases(masses, "AR6") == pytest.approx(27.9)


def test_unknown_gas_rejected():
    with pytest.raises(UnknownGas):
        gwp("XeF6", "AR6")


def test_unknown_gwp_set_rejected():
    with pytest.raises(UnknownGwpSet):
        gwp("CO2", "AR9")
