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
    """The DEFRA loader used to hardcode lca_boundary=None, so every DEFRA factor was
    'boundary not assessable' (W1) and Table 5.4 (B12) had nothing to check. Every row of
    the real sample now carries the boundary its published (Scope, Level 1) table implies."""
    rows = parse_defra_flat_csv((SAMPLES / "defra_flat_sample.csv").read_bytes())
    gas = next(r for r in rows if "Natural gas" in r.subcategory)
    diesel = next(r for r in rows if "Diesel" in r.subcategory)
    elec = next(r for r in rows if r.category == "UK electricity")
    car = next(r for r in rows if "Medium car" in r.subcategory)
    assert gas.lca_boundary == "combustion"                # Scope 1 fuel
    assert diesel.lca_boundary == "combustion"
    assert elec.lca_boundary == "generation"               # Scope 2 electricity
    assert car.lca_boundary == "ttw"                       # Scope 3 business travel (tailpipe)
    # ...and each SATISFIES the gate for a category it can legitimately serve.
    assert boundary_meets_minimum(6, car.lca_boundary) is True
    assert boundary_meets_minimum(8, gas.lca_boundary) is True     # leased building's heating
    assert boundary_meets_minimum(7, elec.lca_boundary) is True    # EV commuting


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


def test_scope1_and_scope2_direct_energy_boundaries():
    """Scope-1 fuel combustion and Scope-2 generation, derived again now that s3bnd-v2
    accepts those tokens across the scope1/2 family.

    HISTORY: under the original vocabulary (s3bnd-v1) Cat 8/13/14 rejected `combustion` and
    Cat 4/6/7/9 rejected `generation`, so — because a factor is scope-AGNOSTIC and these are
    legitimately used on Scope-3 lines — deriving them flipped a safe W1 into a FALSE B12
    BLOCK. That is why PR #17 left them None. s3bnd-v2 fixed the asymmetry at its source."""
    d = _derive_boundary
    assert d("Scope 1", "Fuels", "Liquid fuels") == "combustion"
    assert d("Scope 1", "Bioenergy", "") == "combustion"
    assert d("Scope 2", "UK electricity", "") == "generation"
    assert d("Scope 2", "Heat and steam", "") == "generation"
    # Fugitive/process emissions are NOT a combustion boundary — still None.
    assert d("Scope 1", "Refrigerant & other", "") is None
    assert d("Scope 2", "Something else", "") is None
    # The false block as it existed under v1 — the reason this was withheld...
    assert boundary_meets_minimum(8, "combustion", "s3bnd-v1") is False
    assert boundary_meets_minimum(7, "generation", "s3bnd-v1") is False
    # ...and its fix under the current policy, which is what unblocks the derivation.
    assert boundary_meets_minimum(8, "combustion") is True
    assert boundary_meets_minimum(7, "generation") is True


def test_direct_energy_tokens_still_block_where_they_are_genuinely_wrong():
    """Restoring the derivation must not become a false PASS. A combustion/generation
    factor is NOT a cradle-to-gate goods figure (Cat 1/2) and NOT an upstream fuel figure
    (Cat 3) — those must still block, which is the Table 5.4 check working."""
    for tok in ("combustion", "generation"):
        for cat in (1, 2, 3):
            assert boundary_meets_minimum(cat, tok) is False, (cat, tok)
    # A WTT factor is still not a substitute for an operational one, either.
    for cat in (4, 6, 7, 8, 9, 10, 13, 14):
        assert boundary_meets_minimum(cat, "well_to_tank") is False


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
