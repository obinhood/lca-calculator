from pathlib import Path

import pytest

from app.models import EmissionFactor
from app.ef_catalog.loaders.base import FactorRow, load_factors
from app.ef_catalog.loaders.defra import parse_defra_flat_csv, _derive_boundary
from app.ef_catalog.loaders.useeio import parse_useeio_csv
from app.ef_catalog.loaders.generic import parse_generic_csv
from app.services.resolver import propose_mapping
from app.services.ghgp import boundary_meets_minimum

SAMPLES = Path("app/ef_catalog/samples")


def test_defra_flat_adapter_parses_totals_only():
    rows = parse_defra_flat_csv((SAMPLES / "defra_flat_sample.csv").read_bytes())
    # Only the "kg CO2e" total rows — per-gas "... of CO2 ..." rows excluded.
    assert len(rows) == 4
    gas = next(r for r in rows if "Natural gas" in r.subcategory)
    assert gas.category == "Fuels"
    assert gas.unit == "kWh"
    assert gas.value == pytest.approx(0.18293)
    assert gas.gwp_set == "AR5"            # DEFRA is AR5
    assert gas.kg_co2 is None              # aggregate only, no back-solved per-gas
    assert gas.year == 2024                # parsed from the column header
    diesel = next(r for r in rows if "Diesel" in r.subcategory)
    assert diesel.unit == "L"              # 'litres' normalised


def test_defra_boundary_backfill_on_the_real_sample():
    """The DEFRA loader used to hardcode lca_boundary=None, so every DEFRA factor
    was 'boundary not assessable' (W1) and Table 5.4 (B12) had nothing to check.
    Now it derives the boundary for the Scope 3 tables from the published (Scope,
    Level 1) structure. The Scope 3 business-travel row now carries a boundary that
    SATISFIES the gate for its category (compliant Cat 6)."""
    rows = parse_defra_flat_csv((SAMPLES / "defra_flat_sample.csv").read_bytes())
    car = next(r for r in rows if "Medium car" in r.subcategory)
    assert car.lca_boundary == "ttw"                       # Scope 3 business travel (tailpipe)
    assert boundary_meets_minimum(6, car.lca_boundary) is True


def test_defra_boundary_derivation_is_conservative():
    """Only the DEFRA Scope 3 tables get a boundary, with tokens each target category
    accepts — anything else is None (honest 'not assessable', never a fabricated pass)."""
    d = _derive_boundary
    # Upstream fuel and grid T&D -> Category 3 boundaries.
    assert d("Scope 3", "WTT- fuels", "Gaseous fuels") == "well_to_tank"
    assert d("Scope 3", "WTT- UK & overseas electricity (T&D)", "") == "td_loss"
    assert d("Scope 3", "Transmission and distribution", "") == "td_loss"
    assert boundary_meets_minimum(3, "well_to_tank") is True
    assert boundary_meets_minimum(3, "td_loss") is True
    # Scope 3 direct tables -> tokens their category accepts.
    assert d("Scope 3", "Waste disposal", "") == "waste_treatment"
    assert d("Scope 3", "Freighting goods", "") == "ttw"
    assert d("Scope 3", "Material use", "") == "cradle_to_gate"
    assert boundary_meets_minimum(5, "waste_treatment") is True
    assert boundary_meets_minimum(1, "cradle_to_gate") is True
    # NOT a Scope 3 table / not unambiguous -> None.
    assert d("Scope 3", "Hotel stay", "") is None
    assert d("Scope 3", "Water supply", "") is None
    assert d("Scope 3", "Homeworking", "") is None
    assert d("", "", "") is None
    # None keeps boundary_meets_minimum unassessable (not a silent True).
    assert boundary_meets_minimum(6, None) is None


def test_scope1_fuel_and_scope2_grid_factors_stay_boundaryless_to_avoid_false_blocks():
    """Regression (adversarial review): a factor is scope-agnostic, so a Scope-1 gas
    'combustion' factor is legitimately usable on a Scope-3 line (e.g. a leased
    building's gas heating → Cat 8). The frozen taxonomy accepts 'combustion' for
    Cat 4/6/7/9 but NOT Cat 8/13/14 (and 'generation' vice-versa). Deriving those
    tokens would flip a safe W1 into a FALSE B12 BLOCK of a compliant leased-asset /
    franchise / EV line, so Scope-1 fuel and Scope-2 grid factors are left None."""
    d = _derive_boundary
    assert d("Scope 1", "Fuels", "Liquid fuels") is None
    assert d("Scope 1", "Bioenergy", "") is None
    assert d("Scope 1", "Refrigerant & other", "") is None
    assert d("Scope 2", "UK electricity", "") is None
    assert d("Scope 2", "Heat and steam", "") is None
    # The failure this prevents: had we derived 'combustion'/'generation', these would
    # have (wrongly) been rejected by exactly the categories such a factor can serve.
    assert boundary_meets_minimum(8, "combustion") is False    # Cat 8 rejects combustion
    assert boundary_meets_minimum(7, "generation") is False    # Cat 7 rejects generation
    # None instead yields the honest 'not assessable' (W1), never a false block.
    assert boundary_meets_minimum(8, None) is None
    assert boundary_meets_minimum(7, None) is None


def test_useeio_adapter_price_basis_and_ghg_filter():
    data = (SAMPLES / "useeio_sample.csv").read_bytes()
    purch = parse_useeio_csv(data, price_basis="purchaser")
    basic = parse_useeio_csv(data, price_basis="basic")
    # Only "All GHGs" rows (the CO2 row for computers is excluded).
    assert len(purch) == 3
    law = next(r for r in purch if r.subcategory == "541110")
    assert law.category == "spend" and law.unit == "USD"
    assert law.method_type == "spend_based" and law.base_year == 2022
    assert law.price_basis == "purchaser" and law.value == pytest.approx(0.048)
    law_basic = next(r for r in basic if r.subcategory == "541110")
    assert law_basic.value == pytest.approx(0.045)   # without margins


def test_generic_adapter_roundtrip():
    csv = (b"category,subcategory,unit,value,geography,year,gwp_set,method_type\n"
           b"electricity,,kWh,0.17,GB,2024,AR6,average_data\n")
    rows = parse_generic_csv(csv)
    assert len(rows) == 1 and rows[0].value == 0.17 and rows[0].geography == "GB"


def test_load_and_supersede_via_resolver(db):
    # Load v2024, then v2026 with a lower factor -> resolver returns the new one.
    load_factors(db, [FactorRow(category="electricity", subcategory="", unit="kWh",
                                value=0.207, geography="GB", year=2024, gwp_set="AR5")],
                 source="DEFRA_DESNZ", version="2024")
    load_factors(db, [FactorRow(category="electricity", subcategory="", unit="kWh",
                                value=0.131, geography="GB", year=2026, gwp_set="AR5")],
                 source="DEFRA_DESNZ", version="2026")
    assert db.query(EmissionFactor).count() == 2
    hit = propose_mapping(db, "electricity", "", None, "GB", gwp_set="AR5")
    assert hit is not None
    factor, basis, _ = hit
    assert factor.value == pytest.approx(0.131)      # newest vintage wins
    assert factor.version == "2026"
    # the 2024 row is superseded (its id referenced by the 2026 row)
    old = db.query(EmissionFactor).filter(EmissionFactor.version == "2024").one()
    new = db.query(EmissionFactor).filter(EmissionFactor.version == "2026").one()
    assert new.supersedes_id == old.id


def test_load_skips_bad_values(db):
    rows = [
        FactorRow(category="a", subcategory="", unit="kWh", value=0.1),
        FactorRow(category="b", subcategory="", unit="kWh", value=float("inf")),
        FactorRow(category="c", subcategory="", unit="kWh", value=-1.0),
    ]
    result = load_factors(db, rows, source="X", version="1")
    assert result["added"] == 1 and result["skipped"] == 2


def test_defra_then_useeio_load_end_to_end(db):
    d = load_factors(db, parse_defra_flat_csv((SAMPLES / "defra_flat_sample.csv").read_bytes()),
                     source="DEFRA_DESNZ", version="2024")
    u = load_factors(db, parse_useeio_csv((SAMPLES / "useeio_sample.csv").read_bytes()),
                     source="USEEIO", version="1.3")
    assert d["added"] == 4 and u["added"] == 3
    # a spend factor is resolvable and flagged spend_based
    hit = propose_mapping(db, "spend", "541110", None, "US", gwp_set="AR6")
    assert hit and hit[0].method_type == "spend_based"
