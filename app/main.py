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
from .reports.issb_s2 import issb_s2_report
from .reports.gri import gri_report
from .reports.cdp import cdp_export
from .reports.sbti import sbti_report
from .services.neutrality import neutrality_assessment
from .reports.framework_guidance import (
    FRAMEWORKS, list_frameworks, with_guidance,
)

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
    org-scoped endpoint identifies a tenant (org names are not credentials).
    A revoked key is rejected even though its hash still matches."""
    org = db.query(Organisation).filter(
        Organisation.api_key_hash == _hash_key(x_api_key)).first()
    if org is None or org.api_key_revoked:
        raise HTTPException(status_code=401, detail="invalid or revoked API key")
    return org


@app.post("/organisations")
def register_organisation(name: str = Query(...), sector: Optional[str] = None,
                          x_registration_token: Optional[str] = Header(None),
                          db: Session = Depends(get_db)):
    """Register an organisation. The API key is returned ONCE — store it safely.

    Gated: if REGISTRATION_TOKEN is configured, registration requires a matching
    X-Registration-Token (prevents open squatting/abuse). Left open only when no
    token is configured (dev)."""
    import hmac
    reg_token = _os.environ.get("REGISTRATION_TOKEN")
    if reg_token:
        if not x_registration_token or not hmac.compare_digest(x_registration_token, reg_token):
            raise HTTPException(status_code=401, detail="registration requires a valid X-Registration-Token")
    if db.query(Organisation).filter(Organisation.name == name).first():
        raise HTTPException(status_code=409, detail=f"organisation {name!r} already exists")
    key = secrets.token_urlsafe(32)
    org = Organisation(name=name, sector=sector, api_key_hash=_hash_key(key))
    db.add(org); db.commit(); db.refresh(org)
    return {"id": org.id, "name": org.name, "api_key": key,
            "note": "Store this key now; it is not retrievable later."}


@app.post("/organisations/rotate_key")
def rotate_api_key(org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Rotate the calling org's API key. The old key stops working immediately;
    the new key is returned ONCE."""
    from .services.calc import _utcnow_iso
    new_key = secrets.token_urlsafe(32)
    org.api_key_hash = _hash_key(new_key)
    org.api_key_revoked = False
    org.key_rotated_at = _utcnow_iso()
    db.commit()
    return {"id": org.id, "api_key": new_key,
            "note": "New key — the previous key is now invalid. Store this now."}


@app.post("/organisations/revoke_key")
def revoke_api_key(confirm_org_name: str = Query(...),
                   org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Revoke the calling org's API key (self-service kill switch). Requires the
    org name as confirmation; the org's data is retained but its key is disabled
    until an admin re-issues one via rotate on a restored key."""
    if confirm_org_name != org.name:
        raise HTTPException(status_code=400, detail="confirm_org_name does not match")
    org.api_key_revoked = True
    db.commit()
    return {"id": org.id, "revoked": True,
            "note": "Key disabled. Contact an administrator to re-enable access."}


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
    return JSONResponse(with_guidance(cbam_declaration(db, org.id, year,
                                         ets_price_eur_per_t=ets_price_eur_per_t)))


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


@app.post("/lca/assessments")
def create_lca_assessment(name: str = Query(...), standard: str = Query(...),
                          functional_unit: str = Query(...),
                          functional_unit_quantity: float = 1.0, gwp_set: str = "AR6",
                          org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import LcaAssessment
    from .services.lca import STANDARDS
    from .services.calc import _utcnow_iso
    if standard not in STANDARDS:
        raise HTTPException(status_code=400, detail=f"standard must be one of {sorted(STANDARDS)}")
    if not math.isfinite(functional_unit_quantity) or functional_unit_quantity <= 0:
        raise HTTPException(status_code=400, detail="functional_unit_quantity must be finite > 0")
    a = LcaAssessment(organisation_id=org.id, name=name, standard=standard,
                      functional_unit=functional_unit,
                      functional_unit_quantity=functional_unit_quantity,
                      gwp_set=gwp_set, created_at=_utcnow_iso())
    db.add(a); db.commit(); db.refresh(a)
    return {"id": a.id, "name": a.name, "standard": a.standard}


def _own_assessment(db: Session, org: Organisation, assessment_id: int):
    from .models import LcaAssessment
    a = db.query(LcaAssessment).filter(LcaAssessment.id == assessment_id,
                                       LcaAssessment.organisation_id == org.id).first()
    if a is None:
        raise HTTPException(status_code=404, detail="assessment not found for this organisation")
    return a


@app.post("/lca/assessments/{assessment_id}/items")
def add_lca_item(assessment_id: int, stage: str = Query(...),
                 quantity: float = Query(...), unit: str = Query(...),
                 factor_id: int = Query(...), description: Optional[str] = None,
                 allocation_factor: float = 1.0,
                 org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import LcaItem
    from .services.lca import valid_stage
    a = _own_assessment(db, org, assessment_id)
    if not valid_stage(a.standard, stage):
        raise HTTPException(status_code=400,
                            detail=f"invalid stage {stage!r} for {a.standard} "
                                   f"(EN standards require a module code like A1-A3, C3, B6)")
    if not (0.0 <= allocation_factor <= 1.0):
        raise HTTPException(status_code=400, detail="allocation_factor must be in [0, 1]")
    factor = db.get(EmissionFactor, factor_id)
    if factor is None:
        raise HTTPException(status_code=404, detail="emission factor not found")
    it = LcaItem(assessment_id=a.id, stage=stage, description=description, quantity=quantity,
                 unit=unit, factor_id=factor_id, allocation_factor=allocation_factor)
    db.add(it); db.commit(); db.refresh(it)
    return {"id": it.id, "stage": it.stage, "factor_id": factor_id}


@app.get("/reports/lca/{assessment_id}")
def get_lca_report(assessment_id: int, org: Organisation = Depends(current_org),
                   db: Session = Depends(get_db)):
    from .services.lca import compute_assessment
    a = _own_assessment(db, org, assessment_id)
    payload = compute_assessment(db, a)
    payload["framework"] = payload["framework"]  # keep as-is; guidance maps on prefix
    return JSONResponse(with_guidance(payload))


@app.post("/finance/positions")
def add_financed_position(investee_name: str = Query(...), asset_class: str = Query(...),
                          currency: str = Query(...), outstanding_amount: float = Query(...),
                          attribution_denominator: float = Query(...),
                          investee_scope1_tco2e: float = 0.0, investee_scope2_tco2e: float = 0.0,
                          investee_scope3_tco2e: Optional[float] = None,
                          investee_revenue_millions: Optional[float] = None,
                          data_quality_score: int = 5, as_of_date: Optional[str] = None,
                          org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import FinancedPosition
    from .services.pcaf import ASSET_CLASSES
    from .services.calc import _utcnow_iso
    if asset_class not in ASSET_CLASSES:
        raise HTTPException(status_code=400, detail=f"asset_class must be one of {sorted(ASSET_CLASSES)}")
    if not (1 <= data_quality_score <= 5):
        raise HTTPException(status_code=400, detail="data_quality_score must be 1..5 (PCAF)")
    for name, v in (("outstanding_amount", outstanding_amount),
                    ("attribution_denominator", attribution_denominator),
                    ("investee_scope1_tco2e", investee_scope1_tco2e),
                    ("investee_scope2_tco2e", investee_scope2_tco2e)):
        if not math.isfinite(v) or v < 0:
            raise HTTPException(status_code=400, detail=f"{name} must be a finite number >= 0")
    if attribution_denominator <= 0:
        raise HTTPException(status_code=400, detail="attribution_denominator must be > 0")
    p = FinancedPosition(organisation_id=org.id, investee_name=investee_name,
                         asset_class=asset_class, currency=currency.upper(),
                         outstanding_amount=outstanding_amount,
                         attribution_denominator=attribution_denominator,
                         investee_scope1_tco2e=investee_scope1_tco2e,
                         investee_scope2_tco2e=investee_scope2_tco2e,
                         investee_scope3_tco2e=investee_scope3_tco2e,
                         investee_revenue_millions=investee_revenue_millions,
                         data_quality_score=data_quality_score, as_of_date=as_of_date,
                         created_at=_utcnow_iso())
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id, "investee_name": p.investee_name, "asset_class": p.asset_class}


@app.get("/reports/pcaf")
def get_pcaf_report(include_scope3: bool = True, as_of: Optional[str] = None,
                    org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .services.pcaf import portfolio_financed
    return JSONResponse(with_guidance(portfolio_financed(db, org.id, include_scope3=include_scope3,
                                                         as_of=as_of)))


@app.get("/reports/sfdr_pai")
def get_sfdr_pai_report(portfolio_value_millions: Optional[float] = None,
                        include_scope3: bool = True,
                        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .reports.sfdr_pai import sfdr_pai_report
    if portfolio_value_millions is not None and (
            not math.isfinite(portfolio_value_millions) or portfolio_value_millions <= 0):
        raise HTTPException(status_code=400, detail="portfolio_value_millions must be finite > 0")
    return JSONResponse(with_guidance(sfdr_pai_report(db, org.id,
                                                      portfolio_value_millions=portfolio_value_millions,
                                                      include_scope3=include_scope3)))


# --- Nature (TNFD LEAP + SBTN) -----------------------------------------------

@app.post("/nature/sites")
def create_nature_site(name: str = Query(...), country: Optional[str] = None,
                       biome: Optional[str] = None, latitude: Optional[float] = None,
                       longitude: Optional[float] = None, area_hectares: float = 0.0,
                       in_protected_area: bool = False, in_kba: bool = False,
                       water_stress: str = "unknown",
                       org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import NatureSite
    from .services.nature import WATER_STRESS_LEVELS
    from .services.calc import _utcnow_iso
    if water_stress not in WATER_STRESS_LEVELS:
        raise HTTPException(status_code=400,
                            detail=f"water_stress must be one of {list(WATER_STRESS_LEVELS)}")
    if not math.isfinite(area_hectares) or area_hectares < 0:
        raise HTTPException(status_code=400, detail="area_hectares must be finite >= 0")
    if latitude is not None and (not math.isfinite(latitude) or not -90 <= latitude <= 90):
        raise HTTPException(status_code=400, detail="latitude must be finite in [-90, 90]")
    if longitude is not None and (not math.isfinite(longitude) or not -180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="longitude must be finite in [-180, 180]")
    s = NatureSite(organisation_id=org.id, name=name, country=country, biome=biome,
                   latitude=latitude, longitude=longitude, area_hectares=area_hectares,
                   in_protected_area=in_protected_area, in_kba=in_kba,
                   water_stress=water_stress, created_at=_utcnow_iso())
    db.add(s); db.commit(); db.refresh(s)
    return {"id": s.id, "name": s.name, "water_stress": s.water_stress}


def _own_site(db: Session, org: Organisation, site_id: int):
    from .models import NatureSite
    s = db.query(NatureSite).filter(NatureSite.id == site_id,
                                    NatureSite.organisation_id == org.id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="nature site not found for this organisation")
    return s


@app.post("/nature/sites/{site_id}/impacts")
def add_nature_impact(site_id: int, kind: str = Query(...), driver: str = Query(...),
                      materiality: str = "low", description: Optional[str] = None,
                      metric_value: Optional[float] = None, metric_unit: Optional[str] = None,
                      org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import NatureImpactDependency
    from .services.nature import valid_driver, MATERIALITY, IMPACT_DRIVERS, DEPENDENCY_SERVICES
    s = _own_site(db, org, site_id)
    if kind not in ("impact", "dependency"):
        raise HTTPException(status_code=400, detail="kind must be 'impact' or 'dependency'")
    if not valid_driver(kind, driver):
        allowed = IMPACT_DRIVERS if kind == "impact" else DEPENDENCY_SERVICES
        raise HTTPException(status_code=400,
                            detail=f"driver for a {kind} must be one of {list(allowed)}")
    if materiality not in MATERIALITY:
        raise HTTPException(status_code=400, detail=f"materiality must be one of {list(MATERIALITY)}")
    if metric_value is not None and not math.isfinite(metric_value):
        raise HTTPException(status_code=400, detail="metric_value must be a finite number")
    it = NatureImpactDependency(site_id=s.id, kind=kind, driver=driver, materiality=materiality,
                                description=description, metric_value=metric_value,
                                metric_unit=metric_unit)
    db.add(it); db.commit(); db.refresh(it)
    return {"id": it.id, "site_id": s.id, "kind": it.kind, "driver": it.driver}


@app.post("/nature/targets")
def create_nature_target(realm: str = Query(...), name: str = Query(...),
                         baseline_value: float = Query(...), baseline_unit: str = Query(...),
                         target_value: float = Query(...), target_year: int = Query(...),
                         baseline_year: Optional[int] = None, validated: bool = False,
                         org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import NatureTarget
    from .services.nature import REALMS
    from .services.calc import _utcnow_iso
    if realm not in REALMS:
        raise HTTPException(status_code=400, detail=f"realm must be one of {list(REALMS)}")
    for nm, v in (("baseline_value", baseline_value), ("target_value", target_value)):
        if not math.isfinite(v):
            raise HTTPException(status_code=400, detail=f"{nm} must be a finite number")
    if not 2000 <= target_year <= 2100:
        raise HTTPException(status_code=400, detail="target_year must be in [2000, 2100]")
    t = NatureTarget(organisation_id=org.id, realm=realm, name=name,
                     baseline_value=baseline_value, baseline_unit=baseline_unit,
                     baseline_year=baseline_year, target_value=target_value,
                     target_year=target_year, validated=validated, created_at=_utcnow_iso())
    db.add(t); db.commit(); db.refresh(t)
    return {"id": t.id, "realm": t.realm, "name": t.name}


@app.get("/reports/tnfd")
def get_tnfd_report(org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .services.nature import leap_assessment
    return JSONResponse(with_guidance(leap_assessment(db, org.id)))


@app.get("/reports/sbtn")
def get_sbtn_report(org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .services.nature import sbtn_report
    return JSONResponse(with_guidance(sbtn_report(db, org.id)))


@app.post("/assurance/engagements")
def create_engagement(run_id: int = Query(...), standard: str = Query(...),
                      level: str = Query(...), assuror_name: Optional[str] = None,
                      period_label: Optional[str] = None, materiality_pct: float = 5.0,
                      org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import AssuranceEngagement, CalculationRun
    from .services.calc import _utcnow_iso
    if standard not in ("ISAE_3410", "ISO_14064_3", "ISSA_5000"):
        raise HTTPException(status_code=400, detail="standard must be ISAE_3410|ISO_14064_3|ISSA_5000")
    if level not in ("limited", "reasonable"):
        raise HTTPException(status_code=400, detail="level must be limited|reasonable")
    if not (0 < materiality_pct <= 100):
        raise HTTPException(status_code=400, detail="materiality_pct must be in (0, 100]")
    run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                          CalculationRun.organisation_id == org.id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run_id not found for this organisation")
    eng = AssuranceEngagement(organisation_id=org.id, run_id=run_id, standard=standard,
                              level=level, assuror_name=assuror_name,
                              period_label=period_label, materiality_pct=materiality_pct,
                              status="planned", created_at=_utcnow_iso())
    db.add(eng); db.commit(); db.refresh(eng)
    return {"id": eng.id, "run_id": run_id, "standard": standard, "level": level}


def _own_engagement(db: Session, org: Organisation, engagement_id: int):
    from .models import AssuranceEngagement
    eng = db.query(AssuranceEngagement).filter(
        AssuranceEngagement.id == engagement_id,
        AssuranceEngagement.organisation_id == org.id).first()
    if eng is None:
        raise HTTPException(status_code=404, detail="engagement not found for this organisation")
    return eng


@app.post("/assurance/engagements/{engagement_id}/findings")
def add_finding(engagement_id: int, severity: str = Query(...),
                description: str = Query(...), line_item_id: Optional[int] = None,
                org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import AssuranceFinding, EmissionLineItem
    from .services.calc import _utcnow_iso
    eng = _own_engagement(db, org, engagement_id)
    if eng.status == "concluded":
        raise HTTPException(status_code=409, detail="engagement is concluded; reopen not supported")
    if severity not in ("observation", "minor", "material"):
        raise HTTPException(status_code=400, detail="severity must be observation|minor|material")
    if line_item_id is not None:
        li = db.query(EmissionLineItem).filter(EmissionLineItem.id == line_item_id,
                                               EmissionLineItem.run_id == eng.run_id).first()
        if li is None:
            raise HTTPException(status_code=404, detail="line_item_id not in this engagement's run")
    if eng.status == "planned":
        eng.status = "in_progress"
    f = AssuranceFinding(engagement_id=eng.id, line_item_id=line_item_id, severity=severity,
                         description=description, status="open", created_at=_utcnow_iso())
    db.add(f); db.commit(); db.refresh(f)
    return {"id": f.id, "severity": f.severity, "status": f.status}


@app.post("/assurance/findings/{finding_id}/resolve")
def resolve_finding(finding_id: int, resolution_note: str = Query(...),
                    org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import AssuranceFinding, AssuranceEngagement
    row = db.query(AssuranceFinding, AssuranceEngagement).join(
        AssuranceEngagement, AssuranceEngagement.id == AssuranceFinding.engagement_id)\
        .filter(AssuranceFinding.id == finding_id,
                AssuranceEngagement.organisation_id == org.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="finding not found for this organisation")
    f, eng = row
    # A concluded engagement's findings ledger is frozen — mutating it after the
    # opinion is issued would silently doctor the audit trail behind it.
    if eng.status == "concluded":
        raise HTTPException(status_code=409, detail="engagement is concluded; findings are frozen")
    f.status = "resolved"; f.resolution_note = resolution_note
    db.commit()
    return {"id": f.id, "status": f.status}


@app.post("/assurance/engagements/{engagement_id}/conclude")
def conclude_engagement(engagement_id: int, opinion: str = Query(...),
                        opinion_note: Optional[str] = None,
                        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import CalculationRun, AssuranceFinding
    from .services.assurance import readiness_assessment
    from .services.calc import _utcnow_iso
    eng = _own_engagement(db, org, engagement_id)
    if opinion not in ("unqualified", "qualified", "adverse", "disclaimer"):
        raise HTTPException(status_code=400, detail="opinion must be unqualified|qualified|adverse|disclaimer")
    if eng.status == "concluded":
        raise HTTPException(status_code=409, detail="engagement already concluded")
    import json as _json
    run = db.get(CalculationRun, eng.run_id)
    readiness = readiness_assessment(db, run)
    # An unqualified conclusion cannot overstate the assurance obtained.
    if opinion == "unqualified":
        open_material = db.query(AssuranceFinding).filter(
            AssuranceFinding.engagement_id == eng.id,
            AssuranceFinding.status == "open",
            AssuranceFinding.severity == "material").count()
        if not readiness["ready"] or open_material:
            raise HTTPException(status_code=409,
                                detail="cannot issue unqualified: readiness checklist "
                                       "failing and/or open material findings — use "
                                       "qualified/adverse/disclaimer")
    eng.status = "concluded"; eng.opinion = opinion; eng.opinion_note = opinion_note
    eng.concluded_at = _utcnow_iso()
    eng.readiness_snapshot = _json.dumps(readiness)   # freeze the checklist as-issued
    db.commit()
    return {"id": eng.id, "opinion": opinion, "status": eng.status}


@app.post("/assurance/engagements/{engagement_id}/grant_access")
def grant_assurance_access(engagement_id: int,
                           org: Organisation = Depends(current_org),
                           db: Session = Depends(get_db)):
    """Mint a read-only token so an external assuror can view the engagement +
    the run's lineage WITHOUT an org key."""
    import secrets
    from .services.calc import _utcnow_iso  # noqa: F401
    eng = _own_engagement(db, org, engagement_id)
    token = secrets.token_urlsafe(32)
    eng.access_token_hash = _hash_key(token)
    db.commit()
    return {"engagement_id": eng.id, "assurance_token": token,
            "note": "Read-only. Provide as X-Assurance-Token. Shown once."}


def _engagement_for_reader(db: Session, engagement_id: int,
                           x_api_key: Optional[str], x_assurance_token: Optional[str]):
    """Resolve an engagement for either the owning org (X-API-Key) or an
    assuror holding the engagement's read-only token (X-Assurance-Token)."""
    import hmac
    from .models import AssuranceEngagement
    eng = db.get(AssuranceEngagement, engagement_id)
    # Check credentials against the engagement only if it exists — a nonexistent
    # id and an unauthorized one return the SAME 401, so a credential-less caller
    # cannot enumerate valid engagement ids (no existence oracle).
    if eng is not None:
        if x_assurance_token and eng.access_token_hash and \
                hmac.compare_digest(_hash_key(x_assurance_token), eng.access_token_hash):
            return eng, "assuror"
        if x_api_key:
            o = db.query(Organisation).filter(
                Organisation.api_key_hash == _hash_key(x_api_key)).first()
            if o and eng.organisation_id == o.id:
                return eng, "owner"
    raise HTTPException(status_code=401, detail="valid X-API-Key (owner) or X-Assurance-Token required")


@app.get("/assurance/engagements/{engagement_id}")
def get_engagement(engagement_id: int, x_api_key: Optional[str] = Header(None),
                   x_assurance_token: Optional[str] = Header(None),
                   db: Session = Depends(get_db)):
    from .services.assurance import engagement_view
    eng, role = _engagement_for_reader(db, engagement_id, x_api_key, x_assurance_token)
    return JSONResponse(engagement_view(db, eng, include_owner_fields=(role == "owner")))


@app.get("/assurance/engagements/{engagement_id}/lineage")
def get_engagement_lineage(engagement_id: int, x_api_key: Optional[str] = Header(None),
                           x_assurance_token: Optional[str] = Header(None),
                           db: Session = Depends(get_db)):
    """The run's frozen lineage, readable by the assuror via the engagement token."""
    import json as _json
    from .models import EmissionLineItem, CalculationRun
    eng, _role = _engagement_for_reader(db, engagement_id, x_api_key, x_assurance_token)
    run = db.get(CalculationRun, eng.run_id)
    rows = db.query(EmissionLineItem, ActivityRecord)\
        .join(ActivityRecord, ActivityRecord.id == EmissionLineItem.activity_id)\
        .filter(EmissionLineItem.run_id == run.id).order_by(EmissionLineItem.id).all()
    return {
        "engagement_id": eng.id, "run_id": run.id,
        "line_items": [{
            "id": li.id, "scope": li.scope, "method": li.method, "co2e": li.co2e,
            "detail": _json.loads(li.details or "{}"),
            "activity": {"id": a.id, "date": a.date, "category": a.category,
                         "quantity": a.quantity, "unit": a.unit, "source_file": a.source_file},
        } for li, a in rows],
    }


@app.get("/reports/assurance_readiness")
def get_assurance_readiness(run_id: Optional[int] = None,
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    from .models import CalculationRun
    from .services.assurance import readiness_assessment
    if run_id is not None:
        run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                              CalculationRun.organisation_id == org.id).first()
    else:
        run = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
            .order_by(CalculationRun.id.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    return JSONResponse(readiness_assessment(db, run))


@app.post("/targets")
def create_target(name: str = Query(...), target_type: str = Query(...),
                  base_run_id: int = Query(...), base_year: int = Query(...),
                  target_year: int = Query(...), target_reduction_pct: float = Query(...),
                  scope_coverage: str = "1+2", ambition: Optional[str] = None,
                  sbti_validated: bool = False,
                  org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import EmissionsTarget, CalculationRun
    from .services.calc import _utcnow_iso
    from .services.sbti import VALID_SCOPES
    if target_type not in ("near_term", "long_term", "net_zero"):
        raise HTTPException(status_code=400, detail="target_type must be near_term|long_term|net_zero")
    coverage_tokens = {s.strip() for s in (scope_coverage or "").split("+") if s.strip()}
    if not coverage_tokens or (coverage_tokens - VALID_SCOPES):
        raise HTTPException(status_code=400,
                            detail="scope_coverage must combine scopes 1/2/3 (e.g. '1+2', '1+2+3')")
    if not (0.0 <= target_reduction_pct <= 1.0):
        raise HTTPException(status_code=400, detail="target_reduction_pct must be in [0, 1]")
    if target_year <= base_year:
        raise HTTPException(status_code=400, detail="target_year must be after base_year")
    base = db.query(CalculationRun).filter(CalculationRun.id == base_run_id,
                                           CalculationRun.organisation_id == org.id).first()
    if base is None:
        raise HTTPException(status_code=404, detail="base_run_id not found for this organisation")
    t = EmissionsTarget(organisation_id=org.id, name=name, target_type=target_type,
                        scope_coverage=scope_coverage, base_run_id=base_run_id,
                        base_year=base_year, target_year=target_year,
                        target_reduction_pct=target_reduction_pct, ambition=ambition,
                        sbti_validated=sbti_validated, created_at=_utcnow_iso())
    db.add(t); db.commit(); db.refresh(t)
    return {"id": t.id, "name": t.name}


@app.get("/reports/sbti")
def get_sbti_report(target_id: int = Query(...), current_run_id: Optional[int] = None,
                    current_year: Optional[int] = None,
                    org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    return JSONResponse(with_guidance(sbti_report(db, org.id, target_id, current_run_id=current_run_id,
                                    current_year=current_year)))


@app.post("/credits")
def add_credit(registry: str = Query(...), quantity_tco2e: float = Query(...),
               credit_type: str = Query(...), project_id: Optional[str] = None,
               serial_number: Optional[str] = None, vintage_year: Optional[int] = None,
               ccp_approved: bool = False, vcmi_claim: Optional[str] = None,
               org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import CarbonCredit
    from .services.calc import _utcnow_iso
    if credit_type not in ("removal", "reduction", "avoidance"):
        raise HTTPException(status_code=400, detail="credit_type must be removal|reduction|avoidance")
    if not math.isfinite(quantity_tco2e) or quantity_tco2e <= 0:
        raise HTTPException(status_code=400, detail="quantity_tco2e must be a finite number > 0")
    from sqlalchemy.exc import IntegrityError
    c = CarbonCredit(organisation_id=org.id, registry=registry, project_id=project_id,
                     serial_number=serial_number, vintage_year=vintage_year,
                     quantity_tco2e=quantity_tco2e, credit_type=credit_type,
                     ccp_approved=ccp_approved, vcmi_claim=vcmi_claim,
                     created_at=_utcnow_iso())
    db.add(c)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail=f"a credit with registry {registry!r} serial "
                                   f"{serial_number!r} is already registered")
    db.refresh(c)
    return {"id": c.id, "registry": c.registry, "quantity_tco2e": c.quantity_tco2e}


@app.post("/credits/{credit_id}/retire")
def retire_credit(credit_id: int, run_id: int = Query(...),
                  org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import CarbonCredit, CalculationRun
    from .services.calc import _utcnow_iso
    c = db.query(CarbonCredit).filter(CarbonCredit.id == credit_id,
                                      CarbonCredit.organisation_id == org.id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="credit not found for this organisation")
    run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                          CalculationRun.organisation_id == org.id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run_id not found for this organisation")
    if c.retired:
        raise HTTPException(status_code=409, detail="credit already retired")
    c.retired = True
    c.retirement_date = _utcnow_iso()
    c.applied_to_run_id = run_id
    db.commit()
    return {"id": c.id, "retired": True, "applied_to_run_id": run_id}


@app.get("/reports/neutrality")
def get_neutrality_report(run_id: Optional[int] = None, basis: str = "location",
                          org: Organisation = Depends(current_org),
                          db: Session = Depends(get_db)):
    from .models import CalculationRun
    if basis not in ("location", "market"):
        raise HTTPException(status_code=400, detail="basis must be location|market")
    run = None
    if run_id is not None:
        run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                              CalculationRun.organisation_id == org.id).first()
    else:
        run = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
            .order_by(CalculationRun.id.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    return JSONResponse(with_guidance(neutrality_assessment(db, org.id, run, basis=basis)))


@app.get("/reports/issb_s2")
def get_issb_s2_report(run_id: Optional[int] = None,
                       jurisdiction: str = "ISSB",
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """IFRS S2 payload; jurisdiction: ISSB | UK_SRS | JP_SSBJ | SG_SGX | HK_HKEX."""
    return JSONResponse(with_guidance(issb_s2_report(db, org.id, run_id=run_id,
                                       jurisdiction=jurisdiction)))


@app.get("/reports/gri")
def get_gri_report(run_id: Optional[int] = None,
                   base_run_id: Optional[int] = None,
                   intensity_denominator: Optional[float] = None,
                   intensity_denominator_unit: Optional[str] = None,
                   org: Organisation = Depends(current_org),
                   db: Session = Depends(get_db)):
    """GRI 305/302 content-index payload (305-5 needs base_run_id)."""
    if intensity_denominator is not None and (
            not math.isfinite(intensity_denominator) or intensity_denominator <= 0):
        raise HTTPException(status_code=400,
                            detail="intensity_denominator must be a finite number > 0")
    return JSONResponse(with_guidance(gri_report(db, org.id, run_id=run_id, base_run_id=base_run_id,
                                   intensity_denominator=intensity_denominator,
                                   intensity_denominator_unit=intensity_denominator_unit)))


@app.get("/reports/cdp")
def get_cdp_export(run_id: Optional[int] = None,
                   intensity_denominator: Optional[float] = None,
                   intensity_denominator_unit: Optional[str] = None,
                   verification_status: str = "no_third_party_verification",
                   org: Organisation = Depends(current_org),
                   db: Session = Depends(get_db)):
    """CDP Climate questionnaire export (classic C-codes, labelled)."""
    if intensity_denominator is not None and (
            not math.isfinite(intensity_denominator) or intensity_denominator <= 0):
        raise HTTPException(status_code=400,
                            detail="intensity_denominator must be a finite number > 0")
    return JSONResponse(with_guidance(cdp_export(db, org.id, run_id=run_id,
                                   intensity_denominator=intensity_denominator,
                                   intensity_denominator_unit=intensity_denominator_unit,
                                   verification_status=verification_status)))


@app.get("/reports/esrs_e1")
def get_esrs_e1_report(run_id: Optional[int] = None,
                       net_revenue_millions: Optional[float] = None,
                       revenue_currency: str = "EUR",
                       credits_as_of: Optional[str] = None,
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """CSRD ESRS E1 quantitative disclosure payload with pre-submission gates.

    credits_as_of (ISO timestamp) freezes the E1-7 credits section for a filing.
    """
    if net_revenue_millions is not None and (
            not math.isfinite(net_revenue_millions) or net_revenue_millions <= 0):
        raise HTTPException(status_code=400,
                            detail="net_revenue_millions must be a finite number > 0")
    return JSONResponse(with_guidance(esrs_e1_report(db, org.id, run_id=run_id,
                                       net_revenue_millions=net_revenue_millions,
                                       revenue_currency=revenue_currency,
                                       credits_as_of=credits_as_of)))


@app.get("/reports/sb253")
def get_sb253_report(run_id: Optional[int] = None,
                     assurance_level: str = "none",
                     assurance_provider: Optional[str] = None,
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    """California SB 253 (CCDAA) filing payload with pre-submission gates."""
    return JSONResponse(with_guidance(sb253_report(db, org.id, run_id=run_id,
                                     assurance_level=assurance_level,
                                     assurance_provider=assurance_provider)))


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
    return JSONResponse(with_guidance(secr_report(db, org.id, run_id=run_id,
                                    intensity_denominator=intensity_denominator,
                                    intensity_denominator_unit=intensity_denominator_unit)))


@app.post("/taxonomy/activities")
def add_taxonomy_activity(name: str = Query(...), reporting_year: int = Query(...),
                          turnover: float = 0.0, capex: float = 0.0, opex: float = 0.0,
                          eligible: bool = False, substantial_contribution: bool = False,
                          dnsh_pass: bool = False, minimum_safeguards_pass: bool = False,
                          objective: Optional[str] = None,
                          org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import TaxonomyActivity
    from .services.calc import _utcnow_iso
    for nm, v in (("turnover", turnover), ("capex", capex), ("opex", opex)):
        if not math.isfinite(v) or v < 0:
            raise HTTPException(status_code=400, detail=f"{nm} must be a finite number >= 0")
    a = TaxonomyActivity(organisation_id=org.id, name=name, reporting_year=reporting_year,
                         turnover=turnover, capex=capex, opex=opex, eligible=eligible,
                         substantial_contribution=substantial_contribution,
                         dnsh_pass=dnsh_pass, minimum_safeguards_pass=minimum_safeguards_pass,
                         objective=objective, created_at=_utcnow_iso())
    db.add(a); db.commit(); db.refresh(a)
    return {"id": a.id, "name": a.name, "reporting_year": a.reporting_year}


@app.get("/reports/eu_taxonomy")
def get_taxonomy_report(reporting_year: int = Query(...),
                        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .reports.compliance_extra import taxonomy_report
    return JSONResponse(with_guidance(taxonomy_report(db, org.id, reporting_year)))


@app.get("/reports/ets_mrv")
def get_ets_mrv_report(scheme: str = "EU ETS", run_id: Optional[int] = None,
                       verified: bool = False,
                       org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .reports.compliance_extra import ets_mrv_report
    if scheme not in ("EU ETS", "UK ETS"):
        raise HTTPException(status_code=400, detail="scheme must be 'EU ETS' or 'UK ETS'")
    return JSONResponse(with_guidance(ets_mrv_report(db, org.id, scheme, run_id=run_id,
                                                     verified=verified)))


@app.get("/reports/esos")
def get_esos_report(run_id: Optional[int] = None,
                    org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .reports.compliance_extra import esos_report
    return JSONResponse(with_guidance(esos_report(db, org.id, run_id=run_id)))


@app.get("/frameworks")
def get_frameworks(category: Optional[str] = None):
    """List every framework/standard the platform touches, with support status.
    Public reference data — no authentication."""
    items = list_frameworks()
    if category:
        items = [f for f in items if f["category"].lower() == category.lower()]
    return items


@app.get("/frameworks/{key}")
def get_framework_guidance(key: str):
    """Full guidance for one framework/standard."""
    g = FRAMEWORKS.get(key)
    if g is None:
        raise HTTPException(status_code=404, detail=f"unknown framework {key!r}")
    return {"key": key, **g}


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
