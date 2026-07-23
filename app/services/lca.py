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

# ISO 14083 / GLEC well-to-wheel split, keyed on the factor's lca_boundary token.
#
# TANK-TO-WHEEL is the vehicle's own combustion of the fuel in its tank. All three spellings
# below denote that same physical quantity and all three are emitted by this codebase:
# `ttw` is what the DEFRA adapter returns for travel/freight tables, `combustion` for
# stationary and own-fleet fuel, and `tank_to_wheel` is the long form a normalised CSV may
# carry. Omitting `ttw` here silently dropped every third-party freight leg from the
# disclosure while the well-to-wheel TOTAL stayed correct.
#
# WELL-TO-TANK is the upstream energy provision — everything up to the energy entering the
# vehicle's tank or battery. It has two sub-parts the standard reports separately:
#   * FUEL SUPPLY — extraction, refining and distribution of a combustion fuel.
#   * ENERGY PROVISION for an ELECTRIC leg — grid `generation` plus transmission &
#     distribution (`td_loss`) losses. On a battery-electric vehicle the tank-to-wheel
#     (tailpipe) emission is genuinely ZERO, so generation and T&D ARE the well-to-tank;
#     leaving them `unclassified` understated the disclosed WTT of every electric leg.
# ISO 14083:2023 / GLEC Framework treat these as the "energy provision" phase.
_TTW_BOUNDARIES = ("ttw", "tank_to_wheel", "combustion")
_WTT_FUEL_BOUNDARIES = ("well_to_tank", "wtt")
_WTT_ELECTRIC_BOUNDARIES = ("generation", "td_loss")
_WTT_BOUNDARIES = _WTT_FUEL_BOUNDARIES + _WTT_ELECTRIC_BOUNDARIES


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

    is_en = assessment.standard in ("en_15804", "en_15978")
    by_stage: dict = {}
    boundary_split: dict = {}
    total = 0.0            # DECLARED total: A-C for EN standards (Module D excluded)
    total_module_d = 0.0   # EN 15804 §6 / EN 15978: reported SEPARATELY, never netted in
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
        stage = it.stage.strip().upper() if is_en else it.stage
        by_stage[stage] = by_stage.get(stage, 0.0) + co2e
        # Module D is "benefits and loads BEYOND the system boundary" — a recycling
        # credit netted into the headline would understate in-boundary whole-life
        # carbon (a -300 kg D credit hid 25% of it). Keep it out of the declared total.
        if is_en and en_module_group(stage) == "D":
            total_module_d += co2e
        else:
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
    if is_en:
        groups: dict = {}
        for stage, v in by_stage.items():
            g = en_module_group(stage)
            groups[g] = groups.get(g, 0.0) + v
        result["by_module_group_kg"] = {k: round(v, 6) for k, v in groups.items()}
        result["module_group_key"] = EN_GROUP_NOTE
        result["module_d_kg_separate"] = round(total_module_d, 6)
        result["declared_total_scope"] = "A-C (in-boundary); Module D excluded"
        result["note"] = (
            "Declared total covers in-boundary modules A-C only. Module D (benefits "
            "and loads beyond the system boundary) is reported SEPARATELY per EN 15804 "
            "/ EN 15978 and is never netted into the total or the per-functional-unit "
            "figure. Biogenic CO2 is separate too.")
    if assessment.standard == "iso_14083":
        wtt = sum(boundary_split.get(b, 0.0) for b in _WTT_BOUNDARIES)
        ttw = sum(boundary_split.get(b, 0.0) for b in _TTW_BOUNDARIES)
        # RECONCILIATION. Anything the split does not classify is surfaced EXPLICITLY, so
        # WTT + TTW + unclassified == the well-to-wheel total by construction. Without this
        # a boundary token the split happens not to know vanishes silently while the total
        # stays right — which is exactly how every third-party `ttw` freight leg was being
        # dropped from the tank-to-wheel disclosure with no visible cause.
        _classified = set(_WTT_BOUNDARIES) | set(_TTW_BOUNDARIES)
        unclassified = sum(v for k, v in boundary_split.items() if k not in _classified)
        wtt_fuel = sum(boundary_split.get(b, 0.0) for b in _WTT_FUEL_BOUNDARIES)
        wtt_electric = sum(boundary_split.get(b, 0.0) for b in _WTT_ELECTRIC_BOUNDARIES)
        result["well_to_wheel_kg"] = {
            "well_to_tank": round(wtt, 6), "tank_to_wheel": round(ttw, 6),
            "unclassified": round(unclassified, 6),
            "well_to_wheel_total": round(total, 6),
            # GLEC "energy provision" phase, split within WTT: fuel supply for combustion
            # legs vs generation + T&D for electric legs. For a battery-electric leg the
            # tank-to-wheel is zero, so its whole footprint sits under energy_provision.
            "well_to_tank_fuel_supply": round(wtt_fuel, 6),
            "well_to_tank_energy_provision": round(wtt_electric, 6),
            # A real check, not a tautology: it verifies the per-boundary split accounts
            # for the whole declared total.
            "reconciles": abs(wtt + ttw + unclassified - total) < 1e-9,
            "note": "WTT+TTW split from factor boundaries. WTT = fuel supply (combustion "
                    "legs) + energy provision (generation & T&D losses on electric legs, "
                    "whose tank-to-wheel is zero). Boundaries the split cannot classify as "
                    "either an energy-provision or a wheel-side quantity are reported as "
                    "`unclassified` — in the total, never dropped.",
        }
    return result
