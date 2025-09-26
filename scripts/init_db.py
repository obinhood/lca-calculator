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
