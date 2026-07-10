import hashlib
import math
import secrets
from typing import Optional

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Depends, Query, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import (
    Organisation, ActivityRecord, EmissionFactor, ReportingPeriod, CalculationRun,
    MarketInstrument, FxRate, PriceIndex,
)
from .services.ingestion import parse_csv
from .services.qa import check_records
from .services.resolver import auto_map_activity
from .services.calc import compute_co2e, ReportingPeriodError, _parse_iso_date
from .reports.summary import summary
from .reports.secr import secr_report
from .reports.sb253 import sb253_report
from .reports.esrs_e1 import esrs_e1_report
from .reports.cbam import cbam_declaration

app = FastAPI(title="Carbon Footprint MVP", version="0.3.0")

# Browser SPA (frontend/) runs on a different origin in dev; restrict to
# localhost by default, override with ALLOWED_ORIGINS for deployments.
import os as _os
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=_os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Schema is managed by alembic (scripts/init_db.py runs `upgrade head` + seeds).
# A create_all here would create unstamped tables and diverge from the migration
# chain, so it was removed deliberately.

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_ROWS = 50_000


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def current_org(x_api_key: str = Header(...), db: Session = Depends(get_db)) -> Organisation:
    """Resolve the calling organisation from its API key — the ONLY way any
    org-scoped endpoint identifies a tenant (org names are not credentials)."""
    org = db.query(Organisation).filter(
        Organisation.api_key_hash == _hash_key(x_api_key)).first()
    if org is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    return org


@app.post("/organisations")
def register_organisation(name: str = Query(...), sector: Optional[str] = None,
                          db: Session = Depends(get_db)):
    """Register an organisation. The API key is returned ONCE — store it safely."""
    if db.query(Organisation).filter(Organisation.name == name).first():
        raise HTTPException(status_code=409, detail=f"organisation {name!r} already exists")
    key = secrets.token_urlsafe(32)
    org = Organisation(name=name, sector=sector, api_key_hash=_hash_key(key))
    db.add(org); db.commit(); db.refresh(org)
    return {"id": org.id, "name": org.name, "api_key": key,
            "note": "Store this key now; it is not retrievable later."}


@app.post("/activities/upload_csv")
async def upload_activities(file: UploadFile = File(...),
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
    try:
        df = parse_csv(content, filename=file.filename)
    except Exception:
        raise HTTPException(status_code=400, detail="unable to parse CSV file")
    if len(df) > MAX_UPLOAD_ROWS:
        raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_ROWS} rows")
    df, issues = check_records(df)

    # Idempotency: an identical file already ingested for this org would silently
    # double-count every activity in it on a retry/double-click.
    upload_hash = hashlib.sha256(content).hexdigest()
    if db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == org.id,
            ActivityRecord.upload_hash == upload_hash).first():
        raise HTTPException(status_code=409,
                            detail="this exact file was already uploaded for this organisation")

    recs = []
    for _, r in df.iterrows():
        recs.append(ActivityRecord(
            organisation_id=org.id,
            date=str(r["date"]),
            category=(r["category"] or "").strip().lower(),
            subcategory=(str(r["subcategory"]) if r["subcategory"] is not None else "").strip(),
            description=r["description"],
            quantity=float(r["quantity"]) if pd.notna(r["quantity"]) else None,
            unit=(r["unit"] or "").strip(),
            geo=(r["geo"] or "GB").strip(),
            source_file=r["source_file"],
            upload_hash=upload_hash,
            provenance="process",
        ))
    db.add_all(recs); db.commit()

    # Mapping policy (Gap 6): exact matches bind automatically; coarser matches
    # become suggestions in the review queue; nothing coarse binds silently.
    # needs_review rows are RE-proposed so a later, better catalog entry (e.g. a
    # new exact factor) upgrades or refreshes stale suggestions.
    statuses = {"auto": 0, "needs_review": 0, "unmapped": 0}
    try:
        for a in db.query(ActivityRecord).filter(
                ActivityRecord.organisation_id == org.id,
                ActivityRecord.factor_id.is_(None),
                ActivityRecord.mapping_status.in_(["unmapped", "needs_review", None])).all():
            statuses[auto_map_activity(db, a)] += 1
        db.commit()
    except Exception:
        db.rollback()   # activities stay ingested (unmapped); mapping can be retried
        return JSONResponse(status_code=207, content={
            "records_ingested": len(recs), "organisation_id": org.id,
            "mapping": None, "issues": issues + [
                "automatic mapping failed; activities are ingested but unmapped — retry upload or map via review queue"],
        })

    return JSONResponse({"records_ingested": len(recs), "organisation_id": org.id,
                         "mapping": statuses, "issues": issues})


@app.get("/mappings/review")
def list_review_queue(org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    acts = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == org.id,
        ActivityRecord.mapping_status == "needs_review").limit(500).all()
    out = []
    for a in acts:
        sf = a.suggested_factor
        out.append({
            "activity_id": a.id, "date": a.date, "category": a.category,
            "subcategory": a.subcategory, "description": a.description,
            "quantity": a.quantity, "unit": a.unit, "geo": a.geo,
            "mapping_basis": a.mapping_basis, "mapping_confidence": a.mapping_confidence,
            "suggested_factor": None if sf is None else {
                "id": sf.id, "source": sf.source, "version": sf.version,
                "category": sf.category, "subcategory": sf.subcategory,
                "geography": sf.geography, "unit": sf.unit, "value": sf.value,
            },
        })
    return out


def _get_own_activity(db: Session, org: Organisation, activity_id: int) -> ActivityRecord:
    a = db.get(ActivityRecord, activity_id)
    if a is None or a.organisation_id != org.id:
        raise HTTPException(status_code=404, detail="activity not found for this organisation")
    return a


@app.post("/mappings/{activity_id}/approve")
def approve_mapping(activity_id: int, org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    a = _get_own_activity(db, org, activity_id)
    if a.mapping_status != "needs_review" or a.suggested_factor_id is None:
        raise HTTPException(status_code=400, detail="activity is not awaiting review")
    a.factor_id = a.suggested_factor_id
    a.mapping_status = "approved"
    db.commit()
    return {"activity_id": a.id, "factor_id": a.factor_id, "mapping_status": a.mapping_status}


@app.post("/mappings/{activity_id}/override")
def override_mapping(activity_id: int, factor_id: int = Query(...),
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    a = _get_own_activity(db, org, activity_id)
    factor = db.get(EmissionFactor, factor_id)
    if factor is None:
        raise HTTPException(status_code=404, detail="emission factor not found")
    a.factor_id = factor.id
    a.mapping_status = "overridden"
    a.mapping_confidence = 1.0   # human decision
    db.commit()
    return {"activity_id": a.id, "factor_id": a.factor_id, "mapping_status": a.mapping_status}


@app.post("/reporting_periods")
def create_reporting_period(label: str = Query(...),
                            start_date: Optional[str] = None, end_date: Optional[str] = None,
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    period = ReportingPeriod(organisation_id=org.id, label=label,
                             start_date=start_date, end_date=end_date, frozen=False)
    db.add(period); db.commit(); db.refresh(period)
    return {"id": period.id, "organisation_id": org.id, "label": period.label}


@app.post("/reporting_periods/{period_id}/freeze")
def freeze_reporting_period(period_id: int, org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    period = db.get(ReportingPeriod, period_id)
    if period is None or period.organisation_id != org.id:
        raise HTTPException(status_code=404, detail="reporting period not found for this organisation")
    period.frozen = True
    db.commit()
    return {"id": period.id, "frozen": True}


@app.post("/market_instruments")
def create_market_instrument(instrument_type: str = Query(...),
                             kg_co2e_per_kwh: float = Query(...),
                             coverage_kwh: Optional[float] = None,
                             gwp_set: str = Query("AR6"),
                             start_date: Optional[str] = None,
                             end_date: Optional[str] = None,
                             description: Optional[str] = None,
                             org: Organisation = Depends(current_org),
                             db: Session = Depends(get_db)):
    allowed = {"supplier_specific", "ppa", "rec", "residual_mix"}
    contractual = {"supplier_specific", "ppa", "rec"}
    if instrument_type not in allowed:
        raise HTTPException(status_code=400, detail=f"instrument_type must be one of {sorted(allowed)}")
    # Finiteness BEFORE any write: inf/nan would poison every future market total.
    if not math.isfinite(kg_co2e_per_kwh) or kg_co2e_per_kwh < 0:
        raise HTTPException(status_code=400, detail="kg_co2e_per_kwh must be a finite number >= 0")
    if coverage_kwh is not None and (not math.isfinite(coverage_kwh) or coverage_kwh <= 0):
        raise HTTPException(status_code=400, detail="coverage_kwh must be a finite number > 0")
    # Real certificates have a vintage: contractual instruments must be dated so a
    # single-year REC can't silently blanket an org's entire history.
    if instrument_type in contractual:
        if not (start_date and end_date):
            raise HTTPException(status_code=400,
                                detail="contractual instruments (rec/ppa/supplier_specific) require start_date and end_date")
        if _parse_iso_date(start_date) is None or _parse_iso_date(end_date) is None:
            raise HTTPException(status_code=400, detail="dates must be ISO format YYYY-MM-DD")
    inst = MarketInstrument(organisation_id=org.id, instrument_type=instrument_type,
                            kg_co2e_per_kwh=kg_co2e_per_kwh, coverage_kwh=coverage_kwh,
                            gwp_set=gwp_set, start_date=start_date,
                            end_date=end_date, description=description)
    db.add(inst); db.commit(); db.refresh(inst)
    return {"id": inst.id, "organisation_id": org.id, "instrument_type": inst.instrument_type,
            "kg_co2e_per_kwh": inst.kg_co2e_per_kwh, "coverage_kwh": inst.coverage_kwh,
            "gwp_set": inst.gwp_set}


@app.post("/calculate/run")
def run_calculation(gwp_set: str = Query("AR6"),
                    reporting_period_id: Optional[int] = None,
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    try:
        run = compute_co2e(db, org.id, gwp_set=gwp_set, reporting_period_id=reporting_period_id)
    except ReportingPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(summary(db, organisation_id=org.id, run_id=run.id))


@app.get("/results/summary")
def get_summary(run_id: Optional[int] = None,
                org: Organisation = Depends(current_org),
                db: Session = Depends(get_db)):
    return JSONResponse(summary(db, organisation_id=org.id, run_id=run_id))


@app.get("/runs")
def list_runs(org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    runs = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
        .order_by(CalculationRun.id.desc()).limit(50).all()
    return [{"id": r.id, "created_at": r.created_at, "gwp_set": r.gwp_set, "status": r.status,
             "total_co2e": r.total_co2e, "total_co2e_market": r.total_co2e_market,
             "mapped": r.mapped, "total_activities": r.total_activities}
            for r in runs]


@app.get("/runs/{run_id}/lineage")
def get_run_lineage(run_id: int, org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """Full lineage for one immutable run: every line item with its FROZEN
    calculation detail (factor id/version, unit conversion, per-gas GWPs,
    market allocation, spend normalization, DQ) joined to its source activity.
    The assurer drill-down: any figure -> source record -> pinned factor."""
    import json as _json
    from .models import CalculationRun, EmissionLineItem
    run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                          CalculationRun.organisation_id == org.id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    rows = db.query(EmissionLineItem, ActivityRecord)\
        .join(ActivityRecord, ActivityRecord.id == EmissionLineItem.activity_id)\
        .filter(EmissionLineItem.run_id == run.id)\
        .order_by(EmissionLineItem.id).all()
    return {
        "run": {"id": run.id, "created_at": run.created_at, "gwp_set": run.gwp_set,
                "status": run.status, "total_co2e": run.total_co2e,
                "total_co2e_market": run.total_co2e_market,
                "total_biogenic_co2e": run.total_biogenic_co2e,
                "reporting_period_id": run.reporting_period_id},
        "exclusions": _json.loads(run.notes or "[]"),
        "line_items": [{
            "id": li.id, "scope": li.scope, "method": li.method, "co2e": li.co2e,
            "detail": _json.loads(li.details or "{}"),
            "activity": {"id": a.id, "date": a.date, "category": a.category,
                         "subcategory": a.subcategory, "description": a.description,
                         "quantity": a.quantity, "unit": a.unit, "geo": a.geo,
                         "source_file": a.source_file},
        } for li, a in rows],
    }


@app.get("/reports/summary.txt")
def get_plain_report(run_id: Optional[int] = None,
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    s = summary(db, organisation_id=org.id, run_id=run_id)
    lines = [f"Total (location-based): {s['total_co2e']:.2f} kgCO2e"]
    if s.get("run"):
        lines.append(f"Total (market-based):   {s.get('total_co2e_market', 0.0):.2f} kgCO2e")
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
    if s.get("partial"):
        lines.append(f"PARTIAL RUN — excluded: {s.get('partial_reasons')}")
    if s.get("notes"):
        lines.append("\nNotes: " + s["notes"])
    return PlainTextResponse("\n".join(lines))


def require_admin(x_admin_key: str = Header(...)) -> None:
    """Reference data (FX/CPI) is GLOBAL — every tenant's spend calculations
    depend on it, so writes need the platform admin credential, not any org's
    API key. Disabled entirely when no admin key is configured."""
    import hmac
    import os
    admin_key = os.environ.get("ADMIN_API_KEY")
    if not admin_key:
        raise HTTPException(status_code=503,
                            detail="reference-data administration disabled "
                                   "(ADMIN_API_KEY not configured)")
    if not hmac.compare_digest(x_admin_key, admin_key):
        raise HTTPException(status_code=401, detail="invalid admin key")


@app.post("/reference/fx_rates")
def add_fx_rate(base_currency: str = Query(...), quote_currency: str = Query(...),
                year: int = Query(...), rate: float = Query(...),
                _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Append-only: corrections insert a NEW row (latest wins); history preserved."""
    from .services.calc import _utcnow_iso
    if not math.isfinite(rate) or rate <= 0:
        raise HTTPException(status_code=400, detail="rate must be a finite number > 0")
    row = FxRate(base_currency=base_currency.upper(), quote_currency=quote_currency.upper(),
                 year=year, rate=rate, recorded_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "base_currency": row.base_currency,
            "quote_currency": row.quote_currency, "year": row.year, "rate": row.rate}


@app.post("/reference/price_indices")
def add_price_index(currency: str = Query(...), year: int = Query(...),
                    index_value: float = Query(...),
                    _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Append-only: corrections insert a NEW row (latest wins); history preserved."""
    from .services.calc import _utcnow_iso
    if not math.isfinite(index_value) or index_value <= 0:
        raise HTTPException(status_code=400, detail="index_value must be a finite number > 0")
    row = PriceIndex(currency=currency.upper(), year=year, index_value=index_value,
                     recorded_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "currency": row.currency, "year": row.year,
            "index_value": row.index_value}


@app.post("/cbam/goods")
def add_cbam_good(cn_code: str = Query(...), quantity_tonnes: float = Query(...),
                  origin_country: str = Query(...), import_date: str = Query(...),
                  description: Optional[str] = None, installation: Optional[str] = None,
                  actual_direct_t_per_t: Optional[float] = None,
                  actual_indirect_t_per_t: Optional[float] = None,
                  actual_verified: bool = False,
                  carbon_price_paid_eur_per_t: Optional[float] = None,
                  org: Organisation = Depends(current_org),
                  db: Session = Depends(get_db)):
    from .models import CbamGood
    if not math.isfinite(quantity_tonnes) or quantity_tonnes <= 0:
        raise HTTPException(status_code=400, detail="quantity_tonnes must be a finite number > 0")
    for name, v in (("actual_direct_t_per_t", actual_direct_t_per_t),
                    ("actual_indirect_t_per_t", actual_indirect_t_per_t),
                    ("carbon_price_paid_eur_per_t", carbon_price_paid_eur_per_t)):
        if v is not None and (not math.isfinite(v) or v < 0):
            raise HTTPException(status_code=400, detail=f"{name} must be a finite number >= 0")
    if _parse_iso_date(import_date) is None:
        raise HTTPException(status_code=400, detail="import_date must be ISO format YYYY-MM-DD")
    good = CbamGood(organisation_id=org.id, cn_code=cn_code.strip(),
                    description=description, quantity_tonnes=quantity_tonnes,
                    origin_country=origin_country.upper(), import_date=import_date,
                    installation=installation,
                    actual_direct_t_per_t=actual_direct_t_per_t,
                    actual_indirect_t_per_t=actual_indirect_t_per_t,
                    actual_verified=actual_verified,
                    carbon_price_paid_eur_per_t=carbon_price_paid_eur_per_t)
    db.add(good); db.commit(); db.refresh(good)
    return {"id": good.id, "cn_code": good.cn_code, "quantity_tonnes": good.quantity_tonnes}


@app.get("/reports/cbam")
def get_cbam_declaration(year: int = Query(...),
                         ets_price_eur_per_t: Optional[float] = None,
                         org: Organisation = Depends(current_org),
                         db: Session = Depends(get_db)):
    """CBAM annual declaration payload with fail-closed gates."""
    return JSONResponse(cbam_declaration(db, org.id, year,
                                         ets_price_eur_per_t=ets_price_eur_per_t))


@app.post("/reference/cbam_defaults")
def add_cbam_default(cn_code_prefix: str = Query(...), good_category: str = Query(...),
                     direct_t_co2e_per_t: float = Query(...),
                     indirect_t_co2e_per_t: float = Query(...),
                     valid_year: int = Query(...),
                     _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Append-only, admin-gated (global reference data, same doctrine as FX/CPI)."""
    from .models import CbamDefaultValue
    from .services.calc import _utcnow_iso
    for name, v in (("direct_t_co2e_per_t", direct_t_co2e_per_t),
                    ("indirect_t_co2e_per_t", indirect_t_co2e_per_t)):
        if not math.isfinite(v) or v < 0:
            raise HTTPException(status_code=400, detail=f"{name} must be a finite number >= 0")
    # An empty/short prefix would match (hijack) every CN code.
    if not cn_code_prefix.strip().isdigit() or len(cn_code_prefix.strip()) < 2:
        raise HTTPException(status_code=400,
                            detail="cn_code_prefix must be numeric, at least 2 digits")
    row = CbamDefaultValue(cn_code_prefix=cn_code_prefix.strip(),
                           good_category=good_category, valid_year=valid_year,
                           direct_t_co2e_per_t=direct_t_co2e_per_t,
                           indirect_t_co2e_per_t=indirect_t_co2e_per_t,
                           recorded_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "cn_code_prefix": row.cn_code_prefix}


@app.get("/reports/esrs_e1")
def get_esrs_e1_report(run_id: Optional[int] = None,
                       net_revenue_millions: Optional[float] = None,
                       revenue_currency: str = "EUR",
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """CSRD ESRS E1 quantitative disclosure payload with pre-submission gates."""
    if net_revenue_millions is not None and (
            not math.isfinite(net_revenue_millions) or net_revenue_millions <= 0):
        raise HTTPException(status_code=400,
                            detail="net_revenue_millions must be a finite number > 0")
    return JSONResponse(esrs_e1_report(db, org.id, run_id=run_id,
                                       net_revenue_millions=net_revenue_millions,
                                       revenue_currency=revenue_currency))


@app.get("/reports/sb253")
def get_sb253_report(run_id: Optional[int] = None,
                     assurance_level: str = "none",
                     assurance_provider: Optional[str] = None,
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    """California SB 253 (CCDAA) filing payload with pre-submission gates."""
    return JSONResponse(sb253_report(db, org.id, run_id=run_id,
                                     assurance_level=assurance_level,
                                     assurance_provider=assurance_provider))


@app.get("/reports/secr")
def get_secr_report(run_id: Optional[int] = None,
                    intensity_denominator: Optional[float] = None,
                    intensity_denominator_unit: Optional[str] = None,
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """UK SECR disclosure payload with pre-submission validation gates."""
    if intensity_denominator is not None and (
            not math.isfinite(intensity_denominator) or intensity_denominator <= 0):
        raise HTTPException(status_code=400, detail="intensity_denominator must be a finite number > 0")
    return JSONResponse(secr_report(db, org.id, run_id=run_id,
                                    intensity_denominator=intensity_denominator,
                                    intensity_denominator_unit=intensity_denominator_unit))


@app.get("/factors")
def list_factors(db: Session = Depends(get_db), category: Optional[str] = None,
                 geo: Optional[str] = None):
    q = db.query(EmissionFactor)
    if category:
        q = q.filter(EmissionFactor.category == category)
    if geo:
        q = q.filter(EmissionFactor.geography == geo)
    facs = q.limit(200).all()
    return [{"id": f.id, "src": f.source, "ver": f.version, "geo": f.geography, "year": f.year,
             "cat": f.category, "subcat": f.subcategory, "unit": f.unit, "gwp": f.gwp_set,
             "value": f.value} for f in facs]
