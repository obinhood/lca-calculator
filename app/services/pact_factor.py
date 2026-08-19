"""Turning a supplier's PACT footprint into primary data in our own inventory.

This is where the exchange pays off, and the design decision that makes it cheap
is to change NOTHING in the calculation engine. A received PCF is materialised as
an ordinary ``EmissionFactor`` carrying ``method_type="supplier_specific"``, and
every existing mechanism then applies to it unmodified:

  * ``services/dq.py`` maps supplier_specific to reliability 1 — the BEST pedigree
    score, against 5 for spend_based — so the line's lognormal sigma narrows and
    the Monte Carlo interval tightens without a line of new uncertainty code.
  * the primary-data share in the summary counts it automatically.
  * the GWP-vintage guard, the unit conversion, the boundary metadata, the frozen
    per-line lineage and the evidence pack all behave as they do for any factor.

Forking `compute_co2e` to special-case supplier footprints would have duplicated
all of that and then drifted from it.

THREE REFUSALS, because a factor is a live input to every future run:

1. A DECLARED UNIT WE CANNOT CONVERT IS NOT MATERIALISED. PACT's unit vocabulary
   is not ours; each is mapped explicitly and an unmapped one refuses. Guessing a
   mapping is an order-of-magnitude error waiting to happen, and it would be
   invisible — the number would simply be wrong.

2. A GWP SET WE DO NOT SUPPORT IS NOT MATERIALISED. The engine's gwp_mismatch
   guard compares the run's set against the factor's; a factor carrying an
   unrecognised set would slip past it, silently mixing AR5 and AR6 in one total.

3. A DEPRECATED FOOTPRINT IS NOT MATERIALISED. Under v3 a Deprecated PCF has been
   superseded by its author. Turning one into a live factor would put a figure the
   supplier has withdrawn into next year's inventory.
"""
import json
from typing import Optional

from sqlalchemy.orm import Session

from ..models import EmissionFactor, ProductFootprint
from .gwp import SUPPORTED_GWP_SETS

# PACT declared units -> the unit strings this engine's converter understands.
# Verified against services/units.convert rather than assumed: "square meter" maps
# to "m**2" because the bare "m2" form is not dimensionally understood by pint,
# and a factor whose unit only ever matches itself silently stops converting.
DECLARED_UNIT_MAP = {
    "kilogram": "kg",
    "liter": "L",
    "cubic meter": "m3",
    "kilowatt hour": "kWh",
    "megajoule": "MJ",
    "ton kilometer": "tkm",
    "square meter": "m**2",
    "hour": "hour",
    "megabit second": "Mb*s",
    # A count, not a physical dimension: pint cannot convert it to anything else,
    # so an activity must be recorded in exactly this unit to match. That is
    # correct behaviour — "3 pieces" and "3 kg" are not interchangeable — but it
    # is disclosed rather than left to be discovered.
    "piece": "piece",
}

DIMENSIONLESS_UNITS = {"piece"}

# A PACT PCF is cradle-to-gate by definition of the methodology.
PACT_LCA_BOUNDARY = "cradle_to_gate"
PACT_FACTOR_SOURCE = "PACT"


def _gwp_set_from(doc_factors) -> Optional[str]:
    """The GWP vintage a PCF was characterised with, e.g. ['AR6'] -> 'AR6'."""
    if not isinstance(doc_factors, list):
        return None
    for raw in doc_factors:
        if isinstance(raw, str) and raw.strip().upper() in SUPPORTED_GWP_SETS:
            return raw.strip().upper()
    return None


def _year_from(iso: Optional[str]) -> Optional[int]:
    """The vintage year, from the reference period END — the latest activity data.

    Drives the temporal pedigree indicator, so it must reflect how recent the
    supplier's data actually is, not when we happened to import it.
    """
    if not isinstance(iso, str) or len(iso) < 4 or not iso[:4].isdigit():
        return None
    return int(iso[:4])


def materialisation_verdict(db: Session, row: ProductFootprint) -> dict:
    """Whether a held footprint can become a factor, and what that factor would be.

    Separated from the act of creating one so a caller can see the answer — and
    the reason for a refusal — without writing anything.
    """
    problems = []

    if row.status == "Deprecated":
        problems.append(
            "the footprint is Deprecated: its author has superseded it, and "
            "materialising it would put a withdrawn figure into a future inventory")

    unit = DECLARED_UNIT_MAP.get(row.declared_unit)
    if unit is None:
        problems.append(
            f"declared unit {row.declared_unit!r} has no mapping to this engine's unit "
            f"vocabulary. Guessing one is an order-of-magnitude error that would be "
            f"invisible in the result; supported: {sorted(DECLARED_UNIT_MAP)}")

    try:
        doc = json.loads(row.document)
    except (ValueError, TypeError):
        doc = {}
    cf = doc.get("pcf") or {}
    gwp_set = _gwp_set_from(cf.get("ipccCharacterizationFactors"))
    if gwp_set is None:
        problems.append(
            f"the footprint's ipccCharacterizationFactors "
            f"({cf.get('ipccCharacterizationFactors')!r}) is not a GWP set this engine "
            f"supports {list(SUPPORTED_GWP_SETS)}. The run's GWP-mismatch guard "
            f"compares vintages, and an unrecognised one would slip past it — mixing "
            f"AR5 and AR6 inside a single total")

    value = row.kg_co2e_per_unit_excl_biogenic
    if value is None:
        problems.append("no per-unit figure could be derived from the footprint")
    elif value < 0:
        problems.append(
            "the per-unit figure is negative; a negative factor would net a removal "
            "into the gross total. Removals belong in RemovalRecord, reported separately")

    year = _year_from(row.reference_period_end)
    geography = row.geography_value or "Global"

    return {
        "can_materialise": not problems,
        "problems": problems,
        "factor_preview": None if problems else {
            "source": PACT_FACTOR_SOURCE,
            "version": row.pf_id,
            "unit": unit,
            "value": value,
            "gwp_set": gwp_set,
            "year": year,
            "geography": geography,
            "method_type": "supplier_specific",
            "lca_boundary": PACT_LCA_BOUNDARY,
            "dimensionless": row.declared_unit in DIMENSIONLESS_UNITS,
        },
    }


def materialise(db: Session, row: ProductFootprint, *,
                category: Optional[str] = None,
                subcategory: Optional[str] = None) -> dict:
    """Create (or return) the emission factor for a held footprint.

    Idempotent by (source, version): the PACT id is the factor's version, so
    materialising twice returns the same factor rather than minting a duplicate
    that would split a portfolio across two identical rows.
    """
    verdict = materialisation_verdict(db, row)
    if not verdict["can_materialise"]:
        return {"materialised": False, "problems": verdict["problems"],
                "reason": "; ".join(verdict["problems"])}

    p = verdict["factor_preview"]
    existing = db.query(EmissionFactor).filter(
        EmissionFactor.source == PACT_FACTOR_SOURCE,
        EmissionFactor.version == row.pf_id).first()
    if existing is not None:
        return {"materialised": False, "idempotent": True, "factor_id": existing.id,
                "pf_id": row.pf_id,
                "reason": "a factor already exists for this footprint id"}

    factor = EmissionFactor(
        source=p["source"], version=p["version"],
        geography=p["geography"], year=p["year"],
        # The category defaults to the supplier's product name so the factor is
        # recognisable in a review queue; a caller who knows the buyer-side
        # category can supply it.
        category=(category or (row.product_name or "supplier_product")).strip().lower(),
        subcategory=(subcategory or row.company_name or "").strip(),
        unit=p["unit"], gwp_set=p["gwp_set"], value=p["value"],
        method_type="supplier_specific", lca_boundary=PACT_LCA_BOUNDARY)
    db.add(factor)
    db.commit()
    db.refresh(factor)

    return {
        "materialised": True, "factor_id": factor.id, "pf_id": row.pf_id,
        "factor": {
            "id": factor.id, "source": factor.source, "version": factor.version,
            "category": factor.category, "subcategory": factor.subcategory,
            "unit": factor.unit, "value": factor.value, "gwp_set": factor.gwp_set,
            "year": factor.year, "geography": factor.geography,
            "method_type": factor.method_type, "lca_boundary": factor.lca_boundary,
        },
        "effect": {
            "method_type": "supplier_specific",
            "pedigree_reliability": 1,
            "note": "Binding an activity to this factor moves its calculation method "
                    "from average_data or spend_based to supplier_specific. That is "
                    "the BEST pedigree reliability score (1, against 5 for "
                    "spend_based), so the line's lognormal sigma narrows, the Monte "
                    "Carlo interval tightens, and the primary-data share rises — all "
                    "through the existing machinery, with no special case in the "
                    "calculation engine.",
        },
        "dimensionless_unit_note": (
            f"The declared unit {row.declared_unit!r} is a count, not a physical "
            f"dimension. An activity must be recorded in exactly '{p['unit']}' to "
            f"match it; no conversion is possible to or from any other unit."
        ) if p["dimensionless"] else None,
    }


def bind_activities(db: Session, organisation_id: int, factor_id: int,
                    activity_ids: list) -> dict:
    """Bind activities to a materialised supplier factor.

    An explicit, per-activity decision — never an automatic match on product name.
    The buyer knows which of their purchases the supplier's product actually is;
    a fuzzy match would put someone else's footprint on their line and there would
    be nothing in the result to reveal it.
    """
    from ..models import ActivityRecord

    factor = db.get(EmissionFactor, factor_id)
    if factor is None or factor.source != PACT_FACTOR_SOURCE:
        return {"bound": 0, "reason": "factor_id is not a materialised PACT factor"}

    bound, skipped = [], []
    for aid in activity_ids:
        a = db.query(ActivityRecord).filter(
            ActivityRecord.id == aid,
            ActivityRecord.organisation_id == organisation_id).first()
        if a is None:
            skipped.append({"activity_id": aid, "reason": "not found for this organisation"})
            continue
        # The unit must be convertible NOW, at bind time, rather than failing later
        # inside a run where it becomes an excluded row in a coverage counter.
        from .units import convert, UnitConversionError
        try:
            convert(a.quantity if a.quantity is not None else 1.0, a.unit, factor.unit)
        except UnitConversionError as exc:
            skipped.append({"activity_id": aid,
                            "reason": f"unit {a.unit!r} cannot convert to the "
                                      f"footprint's {factor.unit!r}: {exc}"})
            continue
        a.factor_id = factor.id
        a.mapping_status = "overridden"
        a.mapping_basis = "exact"
        a.mapping_confidence = 1.0
        bound.append(a.id)
    db.commit()
    return {"bound": len(bound), "activity_ids": bound,
            "skipped": skipped,
            "note": "Binding is an explicit per-activity decision. Matching a "
                    "supplier's footprint to purchases by name would put someone "
                    "else's figure on a line with nothing in the result to reveal it."}
