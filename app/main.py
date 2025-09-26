from fastapi import FastAPI, UploadFile, File, Depends, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session
from typing import Optional
import pandas as pd

from .database import SessionLocal, engine
from .models import Base, Organisation, ActivityRecord, EmissionFactor, Result
from .services.ingestion import parse_csv
from .services.qa import check_records
from .services.resolver import resolve_factor, suggest_subcategory
from .services.calc import compute_co2e
from .reports.summary import summary

app = FastAPI(title="Carbon Footprint MVP", version="0.1.0")

# Create tables
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.post("/activities/upload_csv")
async def upload_activities(file: UploadFile = File(...), org_name: str = Query("Demo Org"), db: Session = Depends(get_db)):
    content = await file.read()
    df = parse_csv(content, filename=file.filename)
    df, issues = check_records(df)

    # Upsert organisation
    org = db.query(Organisation).filter(Organisation.name==org_name).first()
    if not org:
        org = Organisation(name=org_name)
        db.add(org); db.commit(); db.refresh(org)

    # Persist activities
    recs = []
    for _, r in df.iterrows():
        recs.append(ActivityRecord(
            organisation_id = org.id,
            date = str(r["date"]),
            category = (r["category"] or "").strip().lower(),
            subcategory = (str(r["subcategory"]) if r["subcategory"] is not None else "").strip(),
            description = r["description"],
            quantity = float(r["quantity"]) if pd.notna(r["quantity"]) else None,
            unit = (r["unit"] or "").strip(),
            geo = (r["geo"] or "GB").strip(),
            source_file = r["source_file"],
            provenance = "process"
        ))
    db.add_all(recs); db.commit()

    # Auto map simple factors (deterministic)
    acts = db.query(ActivityRecord).filter(ActivityRecord.factor_id==None).all()
    for a in acts:
        # try subcategory suggestion from description if missing
        subcat = a.subcategory or suggest_subcategory(db, a.category, a.description or "")
        ef = resolve_factor(db, a.category, subcat or "", a.geo, gwp_set="AR6")
        if ef:
            a.factor_id = ef.id
            a.mapping_confidence = 1.0 if subcat or a.subcategory else 0.8
    db.commit()

    return JSONResponse({"records_ingested": len(recs), "issues": issues})

@app.post("/calculate/run")
def run_calculation(db: Session = Depends(get_db)):
    compute_co2e(db, gwp_set="AR6")
    return {"status":"ok"}

@app.get("/results/summary")
def get_summary(db: Session = Depends(get_db)):
    s = summary(db)
    return JSONResponse(s)

@app.get("/reports/summary.txt")
def get_plain_report(db: Session = Depends(get_db)):
    s = summary(db)
    lines = []
    lines.append(f"Total: {s['total_co2e']:.2f} kgCO2e")
    lines.append("\nBy scope:")
    for row in s["by_scope"]:
        lines.append(f"  Scope {row['scope']}: {row['co2e']:.2f} kgCO2e")
    lines.append("\nBy category:")
    for row in s["by_category"]:
        lines.append(f"  {row.get('category','?')}: {row.get('co2e',0.0):.2f} kgCO2e")
    if s.get("notes"):
        lines.append("\nNotes: " + s["notes"])
    return PlainTextResponse("\n".join(lines))

@app.get("/factors")
def list_factors(db: Session = Depends(get_db), category: Optional[str]=None, geo: Optional[str]=None):
    q = db.query(EmissionFactor)
    if category:
        q = q.filter(EmissionFactor.category==category)
    if geo:
        q = q.filter(EmissionFactor.geography==geo)
    facs = q.limit(200).all()
    return [{"id": f.id, "src": f.source, "ver": f.version, "geo": f.geography, "year": f.year,
             "cat": f.category, "subcat": f.subcategory, "unit": f.unit, "gwp": f.gwp_set, "value": f.value} for f in facs]
