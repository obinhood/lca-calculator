"""RICS Whole Life Carbon Assessment (2nd ed.) — the carbon figures in RICS groupings.

RICS WLCA sits on EN 15978 and re-groups its life-cycle modules into the aggregations the
built-environment industry reports and benchmarks against:

  * UPFRONT carbon      = A1-A5   (product + construction — emitted before the building is
                                   in use; the headline metric for a new build).
  * USE-STAGE embodied  = B1-B5   (maintenance, repair, replacement, refurbishment).
  * END-OF-LIFE         = C1-C4.
  * EMBODIED carbon     = A1-A5 + B1-B5 + C1-C4  (everything except operational).
  * OPERATIONAL carbon  = B6 (energy) + B7 (water).
  * WHOLE-LIFE carbon   = embodied + operational  (= the declared A-C total).
  * Module D            = benefits & loads BEYOND the boundary — reported SEPARATELY, never
                          netted into whole-life carbon.

Reported both absolute and per m² of the declared area (the assessment's functional unit).

HONEST about scope — this is the arithmetic RICS asks for, NOT a full RICS-compliant WLCA:
  * GWP-FOSSIL only. This platform computes fossil CO2e and reports biogenic CO2 as a
    SEPARATE physical figure. RICS whole-life carbon accounts biogenic SEQUESTRATION with a
    signed uptake/release convention the platform does not model, so a timber-heavy building
    differs from the RICS figure by its biogenic accounting. GWP-total / biogenic
    sequestration are stated omissions, never folded in.
  * The RICS reporting template, reference service-life and B-module SCENARIO assumptions,
    target BENCHMARKING (RICS / LETI / RIBA), the defined building system boundary, and
    independent review are the preparer's / assessor's, not the engine's. Listed under
    `not_covered`.

Same fail-closed doctrine as every other renderer.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..services.lca import compute_assessment, EN_MODULES

# Each life-cycle module → the RICS aggregation it belongs to. A1/A2/A3 are folded into
# A1-A3 before lookup. Module D is deliberately absent — it is reported separately and is
# never part of whole-life carbon. This map PARTITIONS the A-C modules: every one belongs
# to exactly one of upfront / use_stage_embodied / end_of_life / operational.
_MODULE_GROUP = {
    "A1-A3": "upfront", "A4": "upfront", "A5": "upfront",
    "B1": "use_stage_embodied", "B2": "use_stage_embodied", "B3": "use_stage_embodied",
    "B4": "use_stage_embodied", "B5": "use_stage_embodied",
    "B6": "operational", "B7": "operational",
    "C1": "end_of_life", "C2": "end_of_life", "C3": "end_of_life", "C4": "end_of_life",
}
# Which aggregations roll up into "embodied" (everything that is not operational).
_EMBODIED_GROUPS = ("upfront", "use_stage_embodied", "end_of_life")

# The map must PARTITION the EN A-C modules exactly: every one in a group, none but the
# A1/A2/A3 sub-modules (folded into A1-A3) and Module D outside it. Proven at import so a
# future EN module added to EN_MODULES but not grouped here fails loudly rather than
# silently dropping out of whole-life carbon.
_expected_ac = EN_MODULES - {"A1", "A2", "A3", "D"}
if set(_MODULE_GROUP) != _expected_ac:
    raise RuntimeError(
        "RICS module partition is not exact — A-C modules "
        f"{sorted(_expected_ac ^ set(_MODULE_GROUP))} are ungrouped or misgrouped")

_NOT_COVERED = [
    "GWP-total and biogenic sequestration accounting (RICS §2 / EN 15804+A2): the platform "
    "reports GWP-fossil and a SEPARATE physical biogenic-CO2 figure, not RICS's signed "
    "sequestration convention — a timber-heavy building differs by its biogenic accounting",
    "reference service life and the B-module scenario assumptions (maintenance, "
    "replacement, refurbishment cycles) that drive B1-B5, and the B6/B7 operational "
    "energy/water scenarios — these are the assessor's modelling inputs, entered as line items",
    "benchmarking against RICS / LETI / RIBA targets, and the RICS reporting template",
    "the defined building-system boundary and completeness of the modelled elements "
    "(RICS scope), and independent third-party review",
]


def rics_report(db: Session, organisation_id: int, assessment_id: int,
                gia_unit: Optional[str] = None) -> dict:
    """RICS Whole Life Carbon groupings for one en_15978 building assessment.

    `gia_unit` labels the area denominator (e.g. "m2 GIA"); the quantity is the
    assessment's functional-unit quantity.
    """
    from ..models import LcaAssessment
    a = db.query(LcaAssessment).filter(
        LcaAssessment.id == assessment_id,
        LcaAssessment.organisation_id == organisation_id).first()
    if a is None:
        return {"framework": "RICS Whole Life Carbon (EN 15978)", "disclosure_ready": False,
                "blockers": ["assessment not found for this organisation"]}

    r = compute_assessment(db, a)
    blockers = []

    # RICS WLCA is a whole-BUILDING assessment. A product EPD (en_15804) or the ISO
    # methodologies are a different artifact; the upfront/operational/whole-life groupings
    # and the per-GIA basis do not apply to them.
    if a.standard != "en_15978":
        blockers.append(
            f"assessment standard is {a.standard!r} — a RICS Whole Life Carbon assessment "
            f"is a whole-building assessment and requires an `en_15978` assessment. A "
            f"product (`en_15804`) assessment is an EPD, not a building WLCA")

    if not r["complete"]:
        blockers.append(
            f"assessment is INCOMPLETE — {len(r['excluded'])} item(s) could not be computed "
            f"(unmapped factor or incompatible unit); whole-life carbon must cover the "
            f"modelled building system, not a partial one")

    by_stage = dict(r.get("by_stage_kg", {}))
    # RICS reports the product stage as A1-A3; fold any separately-declared A1/A2/A3.
    _sub = [s for s in ("A1", "A2", "A3") if s in by_stage]
    if _sub:
        by_stage["A1-A3"] = by_stage.get("A1-A3", 0.0) + sum(by_stage.pop(s) for s in _sub)

    unknown = sorted(s for s in by_stage if s not in EN_MODULES)
    if unknown:
        blockers.append(
            f"assessment declares non-EN-15978 stage(s) {unknown} — the module groupings "
            f"are defined only over A1-A5 / B / C / D")

    # UPFRONT (A1-A5) is the mandatory core of a building assessment — every real building
    # has product + construction carbon. Without it, an empty (or Module-D-only) assessment
    # would read disclosure_ready with a physically-impossible whole-life carbon of ZERO.
    # Mirrors the EPD renderer's mandatory-product-stage gate; fail-closed, not blessed-zero.
    if not any(_MODULE_GROUP.get(s) == "upfront" for s in by_stage):
        blockers.append(
            "no upfront-carbon (A1-A5) result — a whole-life carbon assessment must model "
            "the building's product and construction stages; a whole-life figure with no "
            "upfront carbon is not a building")

    # EXACT conservation guard (no floating point — the EPD-round-off lesson): every
    # declared A-C stage must fall into exactly one RICS grouping, or be Module D. A stage
    # that maps nowhere would silently drop out of whole-life carbon.
    _unmapped = sorted(s for s in by_stage if s not in _MODULE_GROUP and s != "D")
    if _unmapped:
        blockers.append(
            f"internal: declared stage(s) {_unmapped} map to no RICS grouping and are not "
            f"Module D — whole-life carbon would understate. This is a platform defect, "
            f"not a data problem")

    # Aggregate GWP-fossil into the RICS groupings.
    groups = {"upfront": 0.0, "use_stage_embodied": 0.0, "end_of_life": 0.0,
              "operational": 0.0}
    for stage, kg in by_stage.items():
        g = _MODULE_GROUP.get(stage)
        if g is not None:
            groups[g] += kg

    embodied = sum(groups[g] for g in _EMBODIED_GROUPS)
    operational = groups["operational"]
    whole_life = embodied + operational      # == the A-C declared total (Module D aside)
    module_d = r.get("module_d_kg_separate", 0.0)

    gia_qty = (a.functional_unit_quantity or 0.0) or 1.0     # never divide by zero

    def _per_gia(v):
        return round(v / gia_qty, 6)

    return {
        "framework": "RICS Whole Life Carbon Assessment (2nd ed., EN 15978)",
        "disclosure_ready": not blockers,
        "blockers": blockers,
        "indicator": "GWP-fossil (kg CO2e); NOT RICS whole-life carbon incl. biogenic "
                     "sequestration",
        "assessment": {
            "building": a.name,
            "area_basis": a.functional_unit,
            "area_quantity": gia_qty,
            "area_unit": (gia_unit or "").strip() or None,
            "gwp_set": a.gwp_set,
        },
        # The RICS headline metrics, absolute (kg CO2e) and per unit area.
        "whole_life_carbon_kg": round(whole_life, 6),
        "whole_life_carbon_per_area_kg": _per_gia(whole_life),
        "upfront_carbon_kg": round(groups["upfront"], 6),
        "upfront_carbon_per_area_kg": _per_gia(groups["upfront"]),
        "embodied_carbon_kg": round(embodied, 6),
        "embodied_carbon_per_area_kg": _per_gia(embodied),
        "operational_carbon_kg": round(operational, 6),
        "operational_carbon_per_area_kg": _per_gia(operational),
        "by_grouping_kg": {
            "upfront_A1_A5": round(groups["upfront"], 6),
            "use_stage_embodied_B1_B5": round(groups["use_stage_embodied"], 6),
            "end_of_life_C1_C4": round(groups["end_of_life"], 6),
            "operational_B6_B7": round(operational, 6),
        },
        # Module D — benefits and loads BEYOND the system boundary — SEPARATE, never in
        # whole-life carbon (the LCA engine already enforces this non-netting).
        "module_D_beyond_boundary_kg": module_d,
        "biogenic_co2_kg_separate": r.get("biogenic_co2_kg_separate", 0.0),
        "biogenic_co2_note": ("Physical biogenic CO2 (own line, never in the carbon "
                              "figures). This is NOT RICS biogenic sequestration accounting, "
                              "which uses a signed uptake/release convention over the study "
                              "period that the platform does not model."),
        "not_covered": _NOT_COVERED,
        "methodology": (
            f"RICS WLCA module groupings over EN 15978 assessment #{a.id}; {a.gwp_set} "
            f"GWP-100. GWP-FOSSIL only; biogenic CO2 a separate physical figure (not RICS "
            f"sequestration accounting); Module D separate, never netted into whole-life "
            f"carbon. Scenario/service-life inputs are the assessor's line items."),
    }
