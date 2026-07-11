from pathlib import Path

import pytest

from app.models import EmissionFactor
from app.ef_catalog.loaders.base import FactorRow, load_factors
from app.ef_catalog.loaders.defra import parse_defra_flat_csv
from app.ef_catalog.loaders.useeio import parse_useeio_csv
from app.ef_catalog.loaders.generic import parse_generic_csv
from app.services.resolver import propose_mapping

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
