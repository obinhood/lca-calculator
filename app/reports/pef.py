"""EU Product Environmental Footprint (PEF) — an honest SINGLE-CATEGORY coverage map.

PEF is a MULTI-IMPACT method: an EF 3.1 profile characterises a product across 16 impact
categories, then normalises and weights them into a single aggregated score, against a
category-specific PEFCR, with independent review. This platform is a CARBON engine — it
computes exactly ONE of those 16 categories, Climate change, and only the GWP-FOSSIL
sub-indicator of it.

So this renderer does NOT pretend to produce a PEF profile. It produces a coverage map that:

  * populates the Climate change category with the platform's GWP-fossil result per declared
    unit (the same number the LCA engine and the EPD renderer report — one source of truth);
  * lists all 16 EF 3.1 impact categories so the 15 the platform does NOT compute are
    explicit, never implied complete;
  * is loud that GWP-fossil is only a SUB-INDICATOR of the Climate change category — PEF's
    Climate change is GWP-total = fossil + biogenic + land-use-change, which differ for any
    bio-based product by the whole biogenic/LULUC flux (the exact mislabelling the EPD
    renderer was corrected for);
  * NEVER reads as a ready PEF profile: normalisation, weighting, the single EF score, PEFCR
    compliance and verification are all explicitly not performed. `disclosure_ready` for a
    PEF PROFILE is therefore always False; the meaningful signal — is the one category we do
    cover soundly computed — is reported under its own precise name.

Same fail-closed / honest-scope doctrine as the EPD and RICS renderers.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..services.lca import compute_assessment

# PEF is a PRODUCT method. These are the product-footprint standards on the platform; a
# transport-chain (iso_14083) or whole-building (en_15978) assessment is not a PEF product study.
_PRODUCT_STANDARDS = {"iso_14067", "iso_14040_44", "en_15804"}

# The 16 EF 3.1 impact categories. `computed` is "partial" for Climate change (GWP-fossil
# sub-indicator only) and False for every other category — this carbon engine does not model them.
_EF_CATEGORIES = [
    {"category": "Climate change", "unit": "kg CO2 eq", "computed": "partial",
     "note": "GWP-FOSSIL sub-indicator only; PEF Climate change is GWP-total (fossil + "
             "biogenic + land use change). Biogenic CO2 is reported separately, not summed."},
    {"category": "Ozone depletion", "unit": "kg CFC-11 eq", "computed": False},
    {"category": "Human toxicity, cancer", "unit": "CTUh", "computed": False},
    {"category": "Human toxicity, non-cancer", "unit": "CTUh", "computed": False},
    {"category": "Particulate matter", "unit": "disease incidence", "computed": False},
    {"category": "Ionising radiation, human health", "unit": "kBq U-235 eq", "computed": False},
    {"category": "Photochemical ozone formation", "unit": "kg NMVOC eq", "computed": False},
    {"category": "Acidification", "unit": "mol H+ eq", "computed": False},
    {"category": "Eutrophication, terrestrial", "unit": "mol N eq", "computed": False},
    {"category": "Eutrophication, freshwater", "unit": "kg P eq", "computed": False},
    {"category": "Eutrophication, marine", "unit": "kg N eq", "computed": False},
    {"category": "Ecotoxicity, freshwater", "unit": "CTUe", "computed": False},
    {"category": "Land use", "unit": "dimensionless (Pt)", "computed": False},
    {"category": "Water use", "unit": "m3 world eq deprived", "computed": False},
    {"category": "Resource use, fossils", "unit": "MJ", "computed": False},
    {"category": "Resource use, minerals and metals", "unit": "kg Sb eq", "computed": False},
]

_NOT_PRODUCED = [
    "the 15 non-climate EF 3.1 impact categories above (computed: false) — a PEF profile "
    "characterises all 16; this carbon engine models only Climate change",
    "GWP-biogenic and GWP-land-use-change sub-indicators — so the FULL Climate change "
    "(GWP-total) category value is not produced either, only its fossil sub-indicator",
    "normalisation and weighting to the single aggregated EF score (PEF's headline result)",
    "compliance with a category-specific PEFCR (product category rules), including the "
    "required data-quality rating and the modelling of the Circular Footprint Formula",
    "independent PEF verification / review — this is a data report, not a verified PEF study",
]


def pef_report(db: Session, organisation_id: int, assessment_id: int) -> dict:
    """A PEF-shaped coverage map for one LCA assessment: the Climate change category populated
    with the platform's GWP-fossil result, all 16 EF 3.1 categories listed, and a loud caveat
    that this is not a PEF profile.
    """
    from ..models import LcaAssessment
    a = db.query(LcaAssessment).filter(
        LcaAssessment.id == assessment_id,
        LcaAssessment.organisation_id == organisation_id).first()
    if a is None:
        return {"framework": "EU Product Environmental Footprint (PEF)",
                "climate_change_gwp_fossil_ready": False,
                "blockers": ["assessment not found for this organisation"]}

    r = compute_assessment(db, a)
    blockers = []

    if a.standard not in _PRODUCT_STANDARDS:
        blockers.append(
            f"assessment standard is {a.standard!r} — PEF is a PRODUCT footprint method and "
            f"requires a product LCA ({sorted(_PRODUCT_STANDARDS)}); a transport-chain "
            f"(iso_14083) or whole-building (en_15978) assessment is not a PEF product study")

    if not r["complete"]:
        blockers.append(
            f"assessment is INCOMPLETE — {len(r['excluded'])} item(s) could not be computed; "
            f"the Climate change result would understate the product system")

    # Minimum-content gate. An assessment with NO computable items yields complete=True (there
    # is nothing to exclude) and a 0 kg total — a vacuous product system that must not read as
    # a soundly-computed, ready Climate change result. Mirrors the EPD renderer's mandatory
    # product-stage gate; the same fail-closed class the ISO 14064-2 empty-run gate closes.
    if not r["lines"]:
        blockers.append(
            "assessment has NO computable emission lines — an empty product system has no "
            "Climate change result; a 0 kg figure here would be a fabricated, not a computed, "
            "value")

    fu_qty = (a.functional_unit_quantity or 0.0) or 1.0     # never divide by zero
    climate_fossil_kg = r["total_co2e_kg"]

    # The GWP-FOSSIL sub-indicator is "ready" only when soundly computed against a non-empty
    # product assessment — this is NOT the full Climate change (GWP-total) category, and NOT a
    # claim that a PEF profile is ready (see pef_profile_status).
    fossil_ready = not blockers

    # Populate the 16-category table. Every row carries `value` (the FULL EF category result)
    # uniformly: it is None on all 16 — including Climate change, whose GWP-TOTAL value the
    # platform does not produce. Climate change additionally carries the GWP-fossil
    # SUB-INDICATOR it does compute, in its own explicitly-named fields.
    categories = []
    for c in _EF_CATEGORIES:
        row = dict(c)
        row["value"] = None                                 # full-category (GWP-total) result: not produced
        if c["category"] == "Climate change":
            row["gwp_fossil_kg"] = round(climate_fossil_kg, 6)
            row["gwp_fossil_per_declared_unit_kg"] = round(climate_fossil_kg / fu_qty, 6)
            row["gwp_fossil_ready"] = fossil_ready
        categories.append(row)

    return {
        "framework": "EU Product Environmental Footprint (PEF)",
        # A PEF PROFILE is multi-impact + normalised + weighted + PEFCR-compliant + verified;
        # a carbon engine cannot produce one. pef_profile_status is always single-category, on
        # purpose. The useful signal below is scoped to the GWP-FOSSIL sub-indicator — it is
        # NOT "the Climate change category is ready" (that needs GWP-total = fossil+bio+LULUC).
        "pef_profile_status": "single_impact_category_only — a full PEF profile is NOT produced",
        "climate_change_gwp_fossil_ready": fossil_ready,
        "blockers": blockers,
        "product": {
            "name": a.name,
            "declared_unit": a.functional_unit,
            "declared_unit_quantity": fu_qty,
            "standard": a.standard,
            "gwp_set": a.gwp_set,
        },
        "impact_categories_ef_3_1": categories,
        "coverage": "1 of 16 EF 3.1 impact categories (Climate change), GWP-fossil "
                    "sub-indicator only",
        # Loud, like the EPD renderer: GWP-fossil is NOT PEF's Climate change (GWP-total).
        "climate_change_indicator": "GWP-fossil (kg CO2 eq); NOT the PEF Climate change "
                                    "(GWP-total) category, which also includes GWP-biogenic "
                                    "and GWP-land-use-change",
        "biogenic_co2_kg_separate": r.get("biogenic_co2_kg_separate", 0.0),
        "not_produced": _NOT_PRODUCED,
        "methodology": (
            f"EF 3.1 impact-category structure over LCA assessment #{a.id}; {a.gwp_set} "
            f"GWP-100. Climate change populated with the platform's GWP-FOSSIL result "
            f"(same figure as the LCA engine); the other 15 categories are not computed. "
            f"No normalisation, weighting, single EF score, PEFCR compliance or verification "
            f"— this is a single-category coverage map, not a PEF profile."),
    }
