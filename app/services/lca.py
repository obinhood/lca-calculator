"""LCA / sector assessment engine.

Computes a life-cycle assessment from its item bill, reusing the fail-closed
calc primitives (unit conversion, per-gas GWP at calc time, biogenic
separation). Reports by stage/module, total, and normalised per functional
unit. Standard-specific structure:
  * ISO 14067 / 14040-44 — product footprint by life-cycle stage.
  * ISO 14083 / GLEC — transport chain by leg, with a well-to-tank / tank-to-
    wheel split derived from each factor's lca_boundary (WTT + TTW = WTW).
  * EN 15804 / EN 15978 — construction by lifecycle module, grouped A/B/C/D.
Biogenic CO2 is reported separately (ISO 14067), never in the fossil total.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import LcaAssessment, LcaItem
from .units import convert, UnitConversionError
from .calc import compute_activity_co2e

STANDARDS = {"iso_14067", "iso_14040_44", "iso_14083", "en_15804", "en_15978"}

# EN 15804 / EN 15978 information modules.
EN_MODULES = {
    "A1", "A2", "A3", "A1-A3", "A4", "A5",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7",
    "C1", "C2", "C3", "C4", "D",
}
EN_GROUP_NOTE = {"A": "Product & construction (A1-A5)", "B": "Use (B1-B7)",
                 "C": "End of life (C1-C4)", "D": "Beyond the system boundary (D)"}


def en_module_group(module: str) -> str:
    m = (module or "").strip().upper()
    return m[0] if m and m[0] in "ABCD" else "?"


def valid_stage(standard: str, stage: str) -> bool:
    if standard in ("en_15804", "en_15978"):
        return (stage or "").strip().upper() in EN_MODULES
    return bool((stage or "").strip())   # free-form for product/transport


def _biogenic(item: LcaItem) -> float:
    f = item.factor
    if f is None or f.kg_co2_biogenic is None:
        return 0.0
    try:
        return convert(item.quantity, item.unit, f.unit) * f.kg_co2_biogenic * item.allocation_factor
    except UnitConversionError:
        return 0.0


def compute_assessment(db: Session, assessment: LcaAssessment) -> dict:
    items = db.query(LcaItem).filter(LcaItem.assessment_id == assessment.id)\
        .order_by(LcaItem.id).all()
    gwp = assessment.gwp_set

    by_stage: dict = {}
    boundary_split: dict = {}
    total = 0.0
    total_biogenic = 0.0
    lines = []
    excluded = []
    for it in items:
        if it.factor is None:
            excluded.append({"item_id": it.id, "stage": it.stage,
                             "error": "no emission factor mapped"})
            continue
        try:
            co2e = compute_activity_co2e(it.quantity, it.unit, it.factor, gwp_set=gwp) \
                * it.allocation_factor
        except (UnitConversionError, ValueError) as exc:
            excluded.append({"item_id": it.id, "stage": it.stage, "error": str(exc)})
            continue
        stage = it.stage.strip().upper() if assessment.standard in ("en_15804", "en_15978") \
            else it.stage
        by_stage[stage] = by_stage.get(stage, 0.0) + co2e
        total += co2e
        total_biogenic += _biogenic(it)
        boundary = it.factor.lca_boundary or "unspecified"
        boundary_split[boundary] = boundary_split.get(boundary, 0.0) + co2e
        lines.append({
            "item_id": it.id, "stage": stage, "description": it.description,
            "quantity": it.quantity, "unit": it.unit, "factor_id": it.factor_id,
            "factor_unit": it.factor.unit, "allocation_factor": it.allocation_factor,
            "lca_boundary": it.factor.lca_boundary, "co2e_kg": round(co2e, 6),
        })

    fu_qty = assessment.functional_unit_quantity or 1.0
    result = {
        "framework": {
            "iso_14067": "ISO 14067 product carbon footprint",
            "iso_14040_44": "ISO 14040/14044 life cycle assessment",
            "iso_14083": "ISO 14083 transport chain GHG (GLEC)",
            "en_15804": "EN 15804 construction product EPD",
            "en_15978": "EN 15978 whole-life carbon of buildings",
        }.get(assessment.standard, assessment.standard),
        "assessment": {"id": assessment.id, "name": assessment.name,
                       "standard": assessment.standard, "gwp_set": gwp,
                       "functional_unit": assessment.functional_unit,
                       "functional_unit_quantity": fu_qty},
        "total_co2e_kg": round(total, 6),
        "co2e_per_functional_unit_kg": round(total / fu_qty, 6),
        "biogenic_co2_kg_separate": round(total_biogenic, 6),
        "by_stage_kg": {k: round(v, 6) for k, v in by_stage.items()},
        "lines": lines,
        "excluded": excluded,
        "complete": not excluded,
        "note": "Assessment recomputes from current items against pinned factor "
                "versions; biogenic CO2 is separate, never in the total.",
    }
    if assessment.standard in ("en_15804", "en_15978"):
        groups: dict = {}
        for stage, v in by_stage.items():
            g = en_module_group(stage)
            groups[g] = groups.get(g, 0.0) + v
        result["by_module_group_kg"] = {k: round(v, 6) for k, v in groups.items()}
        result["module_group_key"] = EN_GROUP_NOTE
    if assessment.standard == "iso_14083":
        wtt = boundary_split.get("well_to_tank", 0.0)
        ttw = boundary_split.get("combustion", 0.0) + boundary_split.get("tank_to_wheel", 0.0)
        result["well_to_wheel_kg"] = {
            "well_to_tank": round(wtt, 6), "tank_to_wheel": round(ttw, 6),
            "well_to_wheel_total": round(total, 6),
            "note": "WTT+TTW split from factor boundaries; unspecified boundaries "
                    "are in the total but not the split.",
        }
    return result
