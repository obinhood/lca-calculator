from app.database import Base, engine, SessionLocal
from app.models import EmissionFactor
from pathlib import Path
import csv, json

def seed_factors(session):
    # Load demo factors from CSV
    path = Path("app/ef_catalog/defra_2024_demo.csv")
    with path.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    def _opt_float(v):
        return float(v) if v not in (None, "") else None

    for r in rows:
        session.add(EmissionFactor(
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
            supersedes_id=None
        ))
    session.commit()

def main():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    if session.query(EmissionFactor).count() == 0:
        seed_factors(session)
        print("Seeded emission factors (demo).")
    else:
        print("Emission factors already present.")

if __name__ == "__main__":
    main()
