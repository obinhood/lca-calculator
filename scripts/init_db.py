"""Initialise the database (via alembic) and seed the demo emission factors.

NOTE on the demo catalog: the per-gas columns (kg_co2/kg_ch4/kg_n2o) in
``defra_2024_demo.csv`` are BACK-SOLVED from the published aggregate value so
that the AR6 recomposition reproduces it exactly — they are structural demo
data, NOT independently published DEFRA per-gas figures. Replace with licensed
per-gas data for real use.
"""
from pathlib import Path
import csv

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


def upgrade_schema():
    """Create/upgrade the schema through alembic so the DB is always stamped.

    (Previously ``Base.metadata.create_all`` created tables without an alembic
    stamp, leaving the DB permanently out of step with the migration chain.)
    """
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def seed_factors(session):
    from app.services.calc import factor_gases
    from app.services.gwp import co2e_from_gases
    from app.models import EmissionFactor

    path = ROOT / "app" / "ef_catalog" / "defra_2024_demo.csv"
    with path.open() as f:
        rows = list(csv.DictReader(f))

    def _opt_float(v):
        return float(v) if v not in (None, "") else None

    factors = []
    for r in rows:
        ef = EmissionFactor(
            source="DEFRA_DEMO",
            version="2024.1",
            geography=r["geography"],
            year=int(r["year"]),
            category=r["category"],
            subcategory=r["subcategory"],
            unit=r["unit"],
            gwp_set=r["gwp_set"],
            value=float(r["value"]),
            kg_co2=_opt_float(r.get("kg_co2")),
            kg_ch4=_opt_float(r.get("kg_ch4")),
            kg_n2o=_opt_float(r.get("kg_n2o")),
            ch4_origin=(r.get("ch4_origin") or None),
            method_type=(r.get("method_type") or "average_data"),
            lca_boundary=(r.get("lca_boundary") or None),
            base_year=(int(r["base_year"]) if r.get("base_year") else None),
            price_basis=(r.get("price_basis") or None),
            supersedes_id=None,
        )
        # Guard against catalog typos: a per-gas breakdown must recompose to the
        # published aggregate under the factor's own GWP vintage.
        if ef.has_gas_breakdown:
            recomposed = co2e_from_gases(factor_gases(ef), ef.gwp_set or "AR6")
            if abs(recomposed - ef.value) > max(1e-9, 1e-6 * ef.value):
                raise ValueError(
                    f"per-gas breakdown inconsistent with aggregate for "
                    f"{ef.category}/{ef.subcategory}: {recomposed} != {ef.value}")
        # A spend-based factor without a base_year cannot be aligned to at
        # calc time (fail-closed) — reject the catalog row up front.
        if ef.method_type == "spend_based" and ef.base_year is None:
            raise ValueError(
                f"spend-based factor {ef.category}/{ef.subcategory} has no base_year")
        factors.append(ef)
    session.add_all(factors)
    session.commit()


def seed_reference_data(session):
    """DEMO FX/CPI reference series so the demo spend factors (GBP, base 2021)
    are computable for activity years 2021-2026. Placeholder values like the
    rest of the demo catalog — replace with ONS/Eurostat/ECB series for real use.
    """
    from app.models import FxRate, PriceIndex
    from app.services.calc import _utcnow_iso
    now = _utcnow_iso()
    cpi = {
        "GBP": {2021: 100.0, 2022: 109.0, 2023: 116.0, 2024: 119.0, 2025: 122.0, 2026: 125.0},
        "EUR": {2021: 100.0, 2022: 108.0, 2023: 114.0, 2024: 117.0, 2025: 119.0, 2026: 121.0},
    }
    for cur, series in cpi.items():
        for year, idx in series.items():
            session.add(PriceIndex(currency=cur, year=year, index_value=idx, recorded_at=now))
    session.add(FxRate(base_currency="EUR", quote_currency="GBP", year=2021,
                       rate=0.86, recorded_at=now))
    session.commit()


def seed_cbam_defaults(session):
    """DEMO CBAM default embedded-emissions values (tCO2e per tonne of good) —
    plausible magnitudes only; replace with the official Commission tables."""
    from app.models import CbamDefaultValue
    from app.services.calc import _utcnow_iso
    now = _utcnow_iso()
    rows = [
        ("7208", "iron_steel",  1.90, 0.30),   # flat-rolled steel
        ("7601", "aluminium",   1.50, 5.50),   # unwrought aluminium (electricity-heavy)
        ("2523", "cement",      0.55, 0.05),
        ("3102", "fertilisers", 1.50, 0.30),   # nitrogenous fertilisers
        ("280410", "hydrogen",  9.00, 1.00),
    ]
    for prefix, cat, d, i in rows:
        session.add(CbamDefaultValue(cn_code_prefix=prefix, good_category=cat,
                                     direct_t_co2e_per_t=d, indirect_t_co2e_per_t=i,
                                     valid_year=2026, recorded_at=now))
    session.commit()


def main():
    upgrade_schema()
    from app.database import SessionLocal
    from app.models import EmissionFactor, PriceIndex
    session = SessionLocal()
    try:
        if session.query(EmissionFactor).count() == 0:
            seed_factors(session)
            print("Seeded emission factors (demo).")
        else:
            print("Emission factors already present.")
        if session.query(PriceIndex).count() == 0:
            seed_reference_data(session)
            print("Seeded demo FX/CPI reference data.")
        from app.models import CbamDefaultValue
        if session.query(CbamDefaultValue).count() == 0:
            seed_cbam_defaults(session)
            print("Seeded demo CBAM default values.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
