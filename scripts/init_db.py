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
        factors.append(ef)
    session.add_all(factors)
    session.commit()


def main():
    upgrade_schema()
    from app.database import SessionLocal
    from app.models import EmissionFactor
    session = SessionLocal()
    try:
        if session.query(EmissionFactor).count() == 0:
            seed_factors(session)
            print("Seeded emission factors (demo).")
        else:
            print("Emission factors already present.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
