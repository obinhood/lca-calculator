"""ISO 14025 Type III Environmental Product Declaration — the GWP indicator, EN 15804 form.

This renders an EN 15804 construction-product assessment into the module structure an EPD
declares against (A1-A3, A4, A5, B1-B7, C1-C4, D), with the declared unit and the metadata
a Product Category Rule requires. It reports ONE EN 15804+A2 indicator — GWP-fossil — which
is exactly what this platform's fossil CO2e total is. It is deliberately HONEST about scope:

  * It is NOT a verified EPD. ISO 14025 Type III declarations are third-party verified
    against a PCR and published by a programme operator — none of which a calculation
    engine can do. What this produces is the QUANTITATIVE core (the GWP-fossil indicator by
    module) that a verifier would check, plus the declaration metadata the preparer must
    supply. `verification_status` says so, and disclosure_ready is never True on the basis
    of the numbers alone.
  * It covers GWP-FOSSIL only. EN 15804+A2's headline GWP indicator is GWP-total =
    GWP-fossil + GWP-biogenic + GWP-LULUC; the platform computes only the fossil term.
    GWP-total, GWP-biogenic, GWP-LULUC and the dozen non-GWP impact categories are all
    under `not_covered`. Presenting GWP-fossil AS GWP-total would misstate every bio-based
    product by its whole biogenic flux, so the figures are labelled GWP-fossil throughout.
  * Module D is reported SEPARATELY and never netted into the declared A-C total — the
    same non-netting rule the LCA engine already enforces (EN 15804 Section 6 / EN 15978).

Same fail-closed doctrine as every other renderer: a partial or wrong-standard assessment
does not read as declaration-ready.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..services.lca import compute_assessment, en_module_group, EN_MODULES

# The modules an EN 15804 EPD reports against, in declaration order, grouped as the
# standard groups them. A1-A3 is the mandatory product stage (the "cradle-to-gate" core);
# everything else is declared where the PCR and product life cycle require it.
_EPD_MODULE_ORDER = (
    "A1-A3", "A4", "A5",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7",
    "C1", "C2", "C3", "C4",
)

# Non-GWP impact categories EN 15804+A2 requires that this carbon-only platform does not
# produce — listed so their absence is explicit, never implied complete.
_NOT_COVERED = [
    "GWP-total, GWP-biogenic and GWP-LULUC (EN 15804+A2): the platform reports GWP-fossil "
    "and a SEPARATE physical biogenic-CO2 figure, not the signed GWP-biogenic sub-indicator "
    "nor their GWP-total sum — for a bio-based product these differ by the whole biogenic flux",
    "non-GWP impact categories (acidification, eutrophication, ozone depletion, "
    "photochemical ozone, abiotic resource use, water use) — EN 15804+A2 core indicators",
    "inventory flows (waste categories, output flows) and scenario documentation",
    "third-party verification and programme-operator publication (ISO 14025 Section 8) — "
    "this is a data report a verifier would check, not a verified declaration",
]


def epd_report(db: Session, organisation_id: int, assessment_id: int,
               pcr_reference: Optional[str] = None,
               programme_operator: Optional[str] = None) -> dict:
    """An ISO 14025 / EN 15804 EPD-shaped GWP declaration for one LCA assessment.

    `pcr_reference` and `programme_operator` are the preparer's declarations; without a PCR
    an EPD cannot be made, so its absence is a blocker, not a silent omission.
    """
    from ..models import LcaAssessment
    a = db.query(LcaAssessment).filter(
        LcaAssessment.id == assessment_id,
        LcaAssessment.organisation_id == organisation_id).first()  # noqa
    if a is None:
        return {"framework": "ISO 14025 / EN 15804 EPD", "disclosure_ready": False,
                "blockers": ["assessment not found for this organisation"]}

    r = compute_assessment(db, a)
    blockers = []

    # An EPD is a PRODUCT declaration (EN 15804). en_15978 is a whole-BUILDING assessment
    # (EN 15978 / RICS whole-life carbon) — labelling one as a product EPD would overclaim
    # its scope; iso_14067 / iso_14040_44 / iso_14083 are different methodologies entirely.
    if a.standard != "en_15804":
        blockers.append(
            f"assessment standard is {a.standard!r} — an EN 15804 Type III EPD is a PRODUCT "
            f"declaration and requires an `en_15804` assessment. A whole-building "
            f"(`en_15978`) assessment is a different artifact (EN 15978 / RICS whole-life "
            f"carbon), not a product EPD")

    if not r["complete"]:
        blockers.append(
            f"assessment is INCOMPLETE — {len(r['excluded'])} item(s) could not be "
            f"computed (unmapped factor or incompatible unit); an EPD must declare the "
            f"whole product system, not a partial one")

    # A1-A3 (the product stage) is the mandatory core of an EN 15804 EPD.
    by_stage = r.get("by_stage_kg", {})
    if not any(s in by_stage for s in ("A1-A3", "A1", "A2", "A3")):
        blockers.append(
            "no product-stage (A1-A3) result — EN 15804 requires the product stage as the "
            "mandatory declared core of every EPD")

    if not (pcr_reference or "").strip():
        blockers.append(
            "no Product Category Rule (PCR) referenced — ISO 14025 requires the EPD to "
            "declare the PCR it was made against; pass `pcr_reference`")

    # Any declared stage outside the EN vocabulary is a data error that would corrupt the
    # module table.
    unknown = sorted(s for s in by_stage if s not in EN_MODULES)
    if unknown:
        blockers.append(
            f"assessment declares non-EN-15804 stage(s) {unknown} — an EPD's module table "
            f"is defined only over A1-A5 / B / C / D")

    fu_qty = (a.functional_unit_quantity or 0.0) or 1.0    # never divide by zero
    # EN 15804 lets the product stage be declared as the A1-A3 AGGREGATE or as A1, A2, A3
    # SEPARATELY. The EPD table reports the aggregate, so fold any separate sub-modules into
    # it — otherwise a product declared as A1+A2+A3 would drop out of the table entirely and
    # read as a zero-total, no-product-stage EPD (a silent understatement to zero).
    stage_kg = dict(by_stage)
    _sub = [s for s in ("A1", "A2", "A3") if s in stage_kg]
    if _sub:
        stage_kg["A1-A3"] = stage_kg.get("A1-A3", 0.0) + sum(stage_kg.pop(s) for s in _sub)

    # Per-module, in declaration order, INCLUDING modules the assessment did not declare
    # (they are a first-class "MND" — module not declared — not an absent row, so an
    # assurer sees the whole life cycle rather than an omission).
    modules = {}
    for m in _EPD_MODULE_ORDER:
        kg = stage_kg.get(m)
        modules[m] = {
            "group": en_module_group(m),
            "declared": kg is not None,
            "gwp_fossil_kg": round(kg, 6) if kg is not None else None,
            "gwp_fossil_per_unit_kg": round(kg / fu_qty, 6) if kg is not None else None,
            "status": "declared" if kg is not None else "MND",  # module not declared
        }

    declared_total = round(sum(
        v["gwp_fossil_kg"] for v in modules.values() if v["gwp_fossil_kg"] is not None), 6)

    # Conservation guard, EXACT (no floating point): every declared stage must land in a
    # table cell or be Module D (the one stage deliberately outside the declared A-C total).
    # A stage that maps nowhere would silently understate the declaration. This is a SET
    # fact — reconciling rounded sums instead would false-block valid EPDs on 6dp round-off
    # accumulated across the ~14 modules.
    _unmapped = sorted(st for st in stage_kg if st not in _EPD_MODULE_ORDER and st != "D")
    if _unmapped:
        blockers.append(
            f"internal: declared stage(s) {_unmapped} do not map to the EPD module table "
            f"and are not Module D — the declaration would understate. This is a platform "
            f"defect, not a data problem")

    return {
        "framework": "ISO 14025 / EN 15804 Type III EPD (GWP indicator)",
        "disclosure_ready": not blockers,
        "blockers": blockers,
        "verification_status": "unverified_data_report",
        "verification_note": (
            "This is the quantitative GWP core a verifier would check, NOT a verified EPD. "
            "An ISO 14025 Type III declaration must be independently verified against the "
            "PCR and published by a programme operator (ISO 14025 §8) before it may be "
            "presented as an EPD."),
        "declaration": {
            "product": a.name,
            "declared_unit": a.functional_unit,
            "declared_unit_quantity": fu_qty,
            "pcr_reference": (pcr_reference or "").strip() or None,
            "programme_operator": (programme_operator or "").strip() or None,
            "standard": "EN 15804+A2 (via ISO 14025)",
            "gwp_set": a.gwp_set,
        },
        # GWP-FOSSIL by module (EN 15804+A2 GWP-fossil sub-indicator = this platform's
        # fossil CO2e). NOT GWP-total: biogenic CO2 is a separate physical figure below,
        # never netted into these numbers.
        "indicator": "GWP-fossil (kg CO2e); NOT the EN 15804+A2 GWP-total",
        "gwp_fossil_by_module_kg": modules,
        "declared_modules_gwp_fossil_kg": declared_total,
        "declared_modules_gwp_fossil_per_unit_kg": round(declared_total / fu_qty, 6),
        # Module D — benefits and loads BEYOND the system boundary — SEPARATE, never netted
        # into the A-C declared total. Netting a recycling credit in understates in-boundary
        # whole-life carbon (the LCA engine already enforces this).
        "module_D_beyond_boundary_kg": r.get("module_d_kg_separate", 0.0),
        # A separate PHYSICAL biogenic-CO2 figure — deliberately NOT the signed EN 15804+A2
        # GWP-biogenic sub-indicator, and never added into the GWP-fossil module figures.
        "biogenic_co2_kg_separate": r.get("biogenic_co2_kg_separate", 0.0),
        "biogenic_co2_note": ("Physical biogenic CO2 (own line, never in the module "
                              "figures). This is NOT the EN 15804+A2 GWP-biogenic "
                              "sub-indicator, which uses a signed uptake/release convention "
                              "the platform does not model."),
        "not_covered": _NOT_COVERED,
        "methodology": (
            f"EN 15804+A2 module structure over LCA assessment #{a.id}; {a.gwp_set} "
            f"GWP-100. GWP-FOSSIL sub-indicator only (not GWP-total); biogenic CO2 a "
            f"separate physical figure; Module D separate, never netted into the declared "
            f"A-C total. Carbon-only: the other EN 15804+A2 impact categories are not produced."),
    }
