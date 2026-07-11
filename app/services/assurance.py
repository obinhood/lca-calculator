"""Assurance readiness + engagement views (ISAE 3410 / ISO 14064-3 / ISSA 5000).

The immutable run's frozen lineage is the evidence base. Readiness maps the
run's own quality signals to the assurance criteria an assuror would test, so
"assurance-ready" is a checked claim, not a hope.
"""
import json
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..models import (
    CalculationRun, EmissionLineItem, AssuranceEngagement, AssuranceFinding,
)
from ..reports.summary import summary


def readiness_assessment(db: Session, run: CalculationRun) -> dict:
    """Map a run's quality signals to the assurance criteria (completeness,
    consistency, accuracy, traceability, reproducibility, data quality)."""
    s = summary(db, organisation_id=run.organisation_id, run_id=run.id)
    cov = s["coverage"]
    dq = s.get("data_quality") or {}

    # Traceability: every location line item carries a frozen factor id.
    loc_lines = db.query(EmissionLineItem.details).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "location").all()
    n_lines = len(loc_lines)
    n_traceable = sum(1 for (d,) in loc_lines if json.loads(d or "{}").get("factor_id"))

    checks = []

    def chk(name, ok, criterion, detail=""):
        checks.append({"check": name, "pass": bool(ok), "criterion": criterion,
                       "detail": detail})

    chk("completeness", cov["coverage_pct"] >= 100.0 and not s.get("partial"),
        "ISO 14064-3 / ISAE 3410 — complete organisational boundary, no silent exclusions",
        f"coverage {cov['coverage_pct']}%; partial={bool(s.get('partial'))}")
    chk("no_excluded_activities",
        (run.unmapped or 0) == 0 and (run.unit_errors or 0) == 0
        and (run.data_errors or 0) == 0 and (run.gwp_mismatch or 0) == 0,
        "all activities calculated (excluded items would be a scope limitation)",
        f"unmapped={run.unmapped} unit_errors={run.unit_errors} "
        f"data_errors={run.data_errors} gwp_mismatch={run.gwp_mismatch}")
    chk("consistency_single_gwp", bool(run.gwp_set),
        "single GWP vintage applied consistently across the inventory", run.gwp_set)
    chk("reproducibility", not cov["stale"],
        "run is immutable and not superseded by later activity changes",
        f"stale={cov['stale']}")
    chk("data_quality_quantified", bool(dq.get("has_data")),
        "quantified data-quality (ecoinvent pedigree) supporting the estimate",
        f"score={dq.get('emissions_weighted_score')}")
    chk("traceability", n_lines > 0 and n_traceable == n_lines,
        "every reported figure traceable to source records and a pinned factor version",
        f"{n_traceable}/{n_lines} line items carry a frozen factor id")

    ready = all(c["pass"] for c in checks)
    return {"ready": ready, "checks": checks,
            "note": "Automated readiness against assurance criteria; does not "
                    "constitute assurance — an accredited assuror must perform the "
                    "engagement."}


def engagement_view(db: Session, eng: AssuranceEngagement,
                    include_owner_fields: bool = True) -> dict:
    run = db.get(CalculationRun, eng.run_id)
    findings = db.query(AssuranceFinding).filter(
        AssuranceFinding.engagement_id == eng.id).order_by(AssuranceFinding.id).all()
    materiality_t = (eng.materiality_pct / 100.0) * (run.total_co2e or 0.0) / 1000.0
    open_material = [f for f in findings if f.status == "open" and f.severity == "material"]

    live_readiness = readiness_assessment(db, run)
    # For a concluded engagement show the readiness FROZEN at conclusion, and flag
    # if the run has since drifted (so a reader isn't misled by a live recompute).
    snapshot = json.loads(eng.readiness_snapshot) if eng.readiness_snapshot else None
    readiness_shown = snapshot if snapshot is not None else live_readiness
    ready_now = live_readiness["ready"]
    ready_permitted = ready_now and not open_material

    view = {
        "engagement": {
            "id": eng.id, "standard": eng.standard, "level": eng.level,
            "assuror_name": eng.assuror_name, "period_label": eng.period_label,
            "materiality_pct": eng.materiality_pct, "status": eng.status,
            "opinion": eng.opinion, "opinion_note": eng.opinion_note,
            "created_at": eng.created_at, "concluded_at": eng.concluded_at,
        },
        "run": {"id": run.id, "gwp_set": run.gwp_set,
                "total_co2e_tco2e": round((run.total_co2e or 0.0) / 1000.0, 6),
                "created_at": run.created_at},
        "materiality_tco2e": round(materiality_t, 6),
        "readiness": readiness_shown,
        "readiness_is_snapshot_at_conclusion": snapshot is not None,
        "run_changed_since_conclusion": bool(
            snapshot is not None and snapshot.get("ready") != ready_now),
        "findings": [{
            "id": f.id, "severity": f.severity, "status": f.status,
            "description": f.description, "line_item_id": f.line_item_id,
            "resolution_note": f.resolution_note, "created_at": f.created_at,
        } for f in findings],
        "open_material_findings": len(open_material),
        "conclusion_gate": {
            "unqualified_permitted": ready_permitted,
            "reason": ("ready and no open material findings" if ready_permitted
                       else "readiness checklist failing and/or open material findings"),
        },
    }
    if not include_owner_fields:
        view["engagement"].pop("assuror_name", None)  # keep the assuror view lean
    return view
