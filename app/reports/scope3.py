"""Scope 3 inventory by GHG Protocol category — the 15-row completeness artifact.

REPRODUCTION CONTRACT: this reads ONLY what the run froze —
``calculation_runs``, ``emission_line_items.details`` and
``run_scope3_declarations``. It never joins ActivityRecord, EmissionFactor, or
the live declaration ledger, so re-rendering a filed run years later returns the
same statement even after activities are re-mapped, factors are corrected, or the
screen is edited.

This is the artifact behind ESRS E1 AR 46(i) and IFRS S2 ¶29(a)(vi).
"""
import json
from typing import Optional

from sqlalchemy.orm import Session

from ..models import EmissionLineItem, RunScope3Declaration
from ..services.ghgp import (
    CATEGORIES, GHGP_TAXONOMIES, UNASSIGNED_SOURCES, scope3_completeness,
)


def _tax(run):
    return GHGP_TAXONOMIES.get(run.ghgp_standard_version or "", {})


def scope3_by_ghgp_category(db: Session, run) -> dict:
    """The 15 categories + the unassigned bucket, from frozen lineage only."""
    if not run.ghgp_standard_version:
        return {
            "assessable": False,
            "standard_version": None,
            "note": "This run predates the GHGP 15-category dimension. It has no Scope 3 "
                    "completeness statement — recompute to produce one. It is deliberately "
                    "NOT rendered as a clean 15 x 0.0 table.",
        }

    tax = _tax(run)
    decls = {d.category: d for d in db.query(RunScope3Declaration)
             .filter(RunScope3Declaration.run_id == run.id).all()}

    cats = {c: {"co2e_kg": 0.0, "line_count": 0, "method_mix_kg": {},
                "boundary_not_assessable_lines": 0, "boundary_failed_lines": 0}
            for c in CATEGORIES}
    unassigned = {"co2e_kg": 0.0, "line_count": 0, "by_activity_category": {},
                  "by_source": {}, "candidates": {}}

    for details, co2e in db.query(EmissionLineItem.details, EmissionLineItem.co2e)\
            .filter(EmissionLineItem.run_id == run.id,
                    EmissionLineItem.method == "location",
                    EmissionLineItem.scope == "3").all():
        d = json.loads(details or "{}")
        kg = co2e or 0.0
        src = d.get("ghgp_category_source") or "unassigned"
        cat = d.get("ghgp_category")
        if cat is None or src in UNASSIGNED_SOURCES:
            unassigned["co2e_kg"] += kg
            unassigned["line_count"] += 1
            ac = d.get("activity_category") or "?"
            unassigned["by_activity_category"][ac] = \
                unassigned["by_activity_category"].get(ac, 0) + 1
            unassigned["by_source"][src] = unassigned["by_source"].get(src, 0) + 1
            if d.get("ghgp_category_candidates"):
                unassigned["candidates"][ac] = d["ghgp_category_candidates"]
            continue
        b = cats[cat]
        b["co2e_kg"] += kg
        b["line_count"] += 1
        m = d.get("method_type") or "average_data"
        b["method_mix_kg"][m] = b["method_mix_kg"].get(m, 0.0) + kg
        if d.get("ghgp_min_boundary_met") is None:
            b["boundary_not_assessable_lines"] += 1
        elif d.get("ghgp_min_boundary_met") is False:
            b["boundary_failed_lines"] += 1

    out = {}
    for c in CATEGORIES:
        b = cats[c]
        t = tax.get(c, {})
        d = decls.get(c)
        total = b["co2e_kg"]
        primary = (b["method_mix_kg"].get("supplier_specific", 0.0)
                   + b["method_mix_kg"].get("hybrid", 0.0))
        out[str(c)] = {
            "number": c,
            "name": t.get("name"),
            "direction": t.get("direction"),
            "minimum_boundary": t.get("min_boundary"),
            "sale_year_lifetime": t.get("sale_year_lifetime"),
            "co2e_kg": round(total, 6),
            "tco2e": round(total / 1000.0, 6),
            "line_count": b["line_count"],
            "declared_status": d.status if d else "undeclared",
            "justification": d.justification if d else None,
            "screening_estimate_tco2e": d.screening_estimate_tco2e if d else None,
            "materiality_threshold_pct": d.materiality_threshold_pct if d else None,
            "criteria": json.loads(d.criteria) if (d and d.criteria) else None,
            "method_description": d.method_description if d else None,
            "calculation_tools": d.calculation_tools if d else None,
            "method_mix_kg": {k: round(v, 6) for k, v in b["method_mix_kg"].items()},
            # AR 46(g): primary-data share, derived per category from frozen method_type.
            "primary_data_pct": (round(100.0 * primary / total, 2) if total else None),
            "primary_data_basis": "method_type (supplier_specific + hybrid)",
            "boundary_not_assessable_lines": b["boundary_not_assessable_lines"],
            "boundary_failed_lines": b["boundary_failed_lines"],
        }

    unassigned["tco2e"] = round(unassigned["co2e_kg"] / 1000.0, 6)
    unassigned["co2e_kg"] = round(unassigned["co2e_kg"], 6)
    unassigned["note"] = (
        "These Scope 3 lines ARE included in total_co2e — the footprint is not "
        "understated — but they carry no GHGP category and therefore cannot be "
        "disclosed. Set activities.ghgp_category (see candidates).")

    gate = scope3_completeness(db, run)
    assigned_kg = sum(cats[c]["co2e_kg"] for c in CATEGORIES)
    return {
        "assessable": True,
        "standard_version": run.ghgp_standard_version,
        "map_version": run.ghgp_map_version,
        "categories": out,
        "unassigned": unassigned,
        "totals": {
            "scope3_assigned_kg": round(assigned_kg, 6),
            "scope3_unassigned_kg": unassigned["co2e_kg"],
            "scope3_gross_kg": round(assigned_kg + unassigned["co2e_kg"], 6),
        },
        "completeness": {
            "by_status": gate.get("by_status"),
            "categories_accounted_for": gate.get("categories_accounted_for"),
            "inventory_coverage_pct": gate.get("inventory_coverage_pct"),
            "blockers": gate.get("blockers", []),
            "warnings": gate.get("warnings", []),
        },
    }


def category_tco2e(scope3_ghgp: dict) -> dict:
    """Compact {"<cat>": tco2e, "unassigned": tco2e} for the categories that carry
    emissions — for the renderers that only need the numbers, not the full screen.
    Empty dict for a legacy (non-assessable) run."""
    if not scope3_ghgp or not scope3_ghgp.get("assessable"):
        return {}
    out = {k: v["tco2e"] for k, v in scope3_ghgp["categories"].items()
           if v["tco2e"] or v["line_count"]}
    un = scope3_ghgp.get("unassigned", {})
    if un.get("tco2e"):
        out["unassigned"] = un["tco2e"]
    return out


def scope3_inventory_report(db: Session, organisation_id: int,
                            run_id: Optional[int] = None) -> dict:
    """The disclosure-facing wrapper: the 15-row statement + its gate."""
    from .summary import _resolve_run
    run = _resolve_run(db, organisation_id, run_id)
    if run is None:
        return {"framework": "GHG Protocol Scope 3 (15-category inventory)",
                "disclosure_ready": False,
                "blockers": ["no calculation run exists"]}
    body = scope3_by_ghgp_category(db, run)
    blockers = (body.get("completeness", {}) or {}).get("blockers", []) \
        if body.get("assessable") else [body.get("note")]
    return {
        "framework": "GHG Protocol Scope 3 (15-category inventory)",
        "disclosure_ready": bool(body.get("assessable")) and not blockers,
        "blockers": blockers,
        "run": {"id": run.id, "created_at": run.created_at,
                "reporting_period_id": run.reporting_period_id},
        "scope3": body,
        "note": "All 15 GHG Protocol categories must be screened and either quantified "
                "or excluded with a justification. 'undeclared' and 'not_measured' both "
                "block: a category you never looked at, and a known data gap, can never "
                "be disclosed as zero.",
    }
