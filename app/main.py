from fastapi import FastAPI, UploadFile, File, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session
from typing import Optional
import pandas as pd

from .database import SessionLocal, engine
from .models import Base, Organisation, ActivityRecord, EmissionFactor, ReportingPeriod, CalculationRun
from .services.ingestion import parse_csv
from .services.qa import check_records
from .services.resolver import resolve_factor, suggest_subcategory
from .services.calc import compute_co2e, ReportingPeriodError
from .reports.summary import summary

app = FastAPI(title="Carbon Footprint MVP", version="0.2.0")

# Create tables
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_org(db: Session, org_name: str) -> Organisation:
    """Resolve an organisation or 404. Never return None to a caller — an
    unresolved org must not fall through to an unscoped ('any tenant') query."""
    org = db.query(Organisation).filter(Organisation.name == org_name).first()
    if not org:
        raise HTTPException(status_code=404, detail=f"unknown organisation {org_name!r}")
    return org

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

    # Auto map simple factors (deterministic) — scoped to THIS org's unmapped rows only.
    acts = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == org.id,
        ActivityRecord.factor_id == None,
    ).all()
    for a in acts:
        # try subcategory suggestion from description if missing
        subcat = a.subcategory or suggest_subcategory(db, a.category, a.description or "")
        ef = resolve_factor(db, a.category, subcat or "", a.geo, gwp_set="AR6")
        if ef:
            a.factor_id = ef.id
            a.mapping_confidence = 1.0 if subcat or a.subcategory else 0.8
    db.commit()

    return JSONResponse({"records_ingested": len(recs), "organisation_id": org.id, "issues": issues})

@app.post("/reporting_periods")
def create_reporting_period(org_name: str = Query(...), label: str = Query(...),
                            start_date: Optional[str] = None, end_date: Optional[str] = None,
                            db: Session = Depends(get_db)):
    org = require_org(db, org_name)
    period = ReportingPeriod(organisation_id=org.id, label=label,
                             start_date=start_date, end_date=end_date, frozen=False)
    db.add(period); db.commit(); db.refresh(period)
    return {"id": period.id, "organisation_id": org.id, "label": period.label}

@app.post("/reporting_periods/{period_id}/freeze")
def freeze_reporting_period(period_id: int, org_name: str = Query(...),
                            db: Session = Depends(get_db)):
    org = require_org(db, org_name)
    period = db.get(ReportingPeriod, period_id)
    if period is None or period.organisation_id != org.id:
        raise HTTPException(status_code=404, detail="reporting period not found for this organisation")
    period.frozen = True
    db.commit()
    return {"id": period.id, "frozen": True}

@app.post("/calculate/run")
def run_calculation(org_name: str = Query("Demo Org"), gwp_set: str = Query("AR6"),
                    reporting_period_id: Optional[int] = None, db: Session = Depends(get_db)):
    org = require_org(db, org_name)
    try:
        run = compute_co2e(db, org.id, gwp_set=gwp_set, reporting_period_id=reporting_period_id)
    except ReportingPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(summary(db, organisation_id=org.id, run_id=run.id))

@app.get("/results/summary")
def get_summary(org_name: str = Query("Demo Org"), run_id: Optional[int] = None,
                db: Session = Depends(get_db)):
    org = require_org(db, org_name)
    return JSONResponse(summary(db, organisation_id=org.id, run_id=run_id))

@app.get("/runs")
def list_runs(org_name: str = Query("Demo Org"), db: Session = Depends(get_db)):
    org = require_org(db, org_name)
    runs = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
        .order_by(CalculationRun.id.desc()).limit(50).all()
    return [{"id": r.id, "created_at": r.created_at, "gwp_set": r.gwp_set, "status": r.status,
             "total_co2e": r.total_co2e, "mapped": r.mapped, "total_activities": r.total_activities}
            for r in runs]

@app.get("/reports/summary.txt")
def get_plain_report(org_name: str = Query("Demo Org"), run_id: Optional[int] = None,
                     db: Session = Depends(get_db)):
    org = require_org(db, org_name)
    s = summary(db, organisation_id=org.id, run_id=run_id)
    lines = [f"Total: {s['total_co2e']:.2f} kgCO2e"]
    if s.get("run"):
        lines.append(f"(run #{s['run']['id']}, {s['run']['gwp_set']}, {s['run']['created_at']})")
    lines.append("\nBy scope:")
    for row in s["by_scope"]:
        lines.append(f"  Scope {row['scope']}: {row['co2e']:.2f} kgCO2e")
    lines.append("\nBy category:")
    for row in s["by_category"]:
        lines.append(f"  {row.get('category','?')}: {row.get('co2e',0.0):.2f} kgCO2e")
    cov = s.get("coverage")
    if cov:
        lines.append(f"\nCoverage: {cov['coverage_pct']}% ({cov['coverage_basis']}); "
                     f"{cov['activities_calculated']}/{cov['activities_total']} activities")
        if cov.get("warning"):
            lines.append("WARNING: " + cov["warning"])
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
