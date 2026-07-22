"""ISO 14068-1 carbon-neutrality accounting over the credits register.

Only RETIRED credits applied to a specific run count toward neutrality. The
residual after retirement determines arithmetic neutrality; a defensible CLAIM
additionally needs quality criteria (removals over avoidance, CCP-approved,
retired, reasonable vintage) — surfaced as claim warnings, plus the EU ECGT
restriction on offset-based "carbon neutral" product claims from Sept 2026.
"""
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CarbonCredit, CalculationRun


def neutrality_assessment(db: Session, organisation_id: int, run: CalculationRun,
                          basis: str = "location") -> dict:
    gross_kg = run.total_co2e if basis == "location" else run.total_co2e_market
    gross_t = (gross_kg or 0.0) / 1000.0

    # A market-basis neutrality claim rests on total_co2e_market, which is exactly the
    # figure the Scope 2 residual-mix gate polices. Without this, an ISO 14068 claim could
    # be made on a market figure every OTHER framework hard-blocks — and understated
    # uncovered load makes neutrality easier to reach, so the omission cut the wrong way.
    residual_blockers = []
    if basis == "market":
        from .residual_mix import scope2_residual_mix_completeness
        residual_blockers = scope2_residual_mix_completeness(db, run).get("blockers", [])

    applied = db.query(CarbonCredit).filter(
        CarbonCredit.organisation_id == organisation_id,
        CarbonCredit.retired.is_(True),
        CarbonCredit.applied_to_run_id == run.id).all()
    applied_total = sum(c.quantity_tco2e for c in applied)
    removals_total = sum(c.quantity_tco2e for c in applied if c.credit_type == "removal")

    residual_t = gross_t - applied_total
    # RELATIVE tolerance. `gross_t` is the sum of thousands of float line items, so its
    # accumulated representation error scales with the inventory: for a megatonne
    # footprint it comfortably exceeds a fixed 1e-9 tCO2e (one microgram). Judging exact
    # neutrality against an absolute microgram therefore decided the claim on float noise
    # rather than on the accounting — an org that had retired precisely enough could be
    # told it was NOT neutral, and the error grew with the org.
    #
    # The tolerance is DISCLOSED beside the residual rather than hidden, and it is applied
    # symmetrically: it can only ever forgive a rounding-scale residual, never a real one
    # (1e-9 relative on a megatonne is a single kilogram).
    neutrality_tolerance_t = max(1e-9, abs(gross_t) * 1e-9)
    neutral = residual_t <= neutrality_tolerance_t
    # True only when the verdict RESTS on the tolerance — surfaced so an assurer can see
    # that the claim was decided within rounding rather than with room to spare.
    within_tolerance = neutral and residual_t > 0

    # Register hygiene (context, not claim-affecting on their own).
    n_unretired = db.query(CarbonCredit).filter(
        CarbonCredit.organisation_id == organisation_id,
        CarbonCredit.retired.is_(False)).count()

    warnings = []
    if not neutral:
        warnings.append(f"NOT neutral: {round(residual_t, 6)} tCO2e residual remains "
                        f"after applied retirements — retire more credits or reduce first")
    elif within_tolerance:
        warnings.append(f"neutral WITHIN TOLERANCE: a residual of {residual_t:.3e} tCO2e "
                        f"remains, at or below the {neutrality_tolerance_t:.3e} tCO2e "
                        f"float-accumulation tolerance for an inventory of this size — the "
                        f"claim rests on rounding, not on a margin")
    avoidance = [c for c in applied if c.credit_type == "avoidance"]
    if avoidance:
        warnings.append(f"{len(avoidance)} applied credit(s) are avoidance-type — ISO 14068 "
                        f"and good practice prefer removals for residual offsetting")
    non_ccp = [c for c in applied if not c.ccp_approved]
    if non_ccp:
        warnings.append(f"{len(non_ccp)} applied credit(s) are not ICVCM CCP-approved — "
                        f"integrity not independently assured")
    if applied and neutral:
        warnings.append("EU ECGT (from Sept 2026) bans offset-based 'carbon neutral' "
                        "product claims — this neutrality is offset-based; confirm the "
                        "claim's permissibility and jurisdiction before publishing")

    # An ISO 14068-conformant claim: arithmetically neutral, offset with retired
    # credits, and the residual fully covered by removals with integrity signals.
    iso14068_conformant = bool(
        neutral and applied and not avoidance and not non_ccp
        and removals_total + 1e-9 >= max(0.0, gross_t))

    return {
        "residual_tco2e": round(residual_t, 9),
        "neutrality_tolerance_tco2e": neutrality_tolerance_t,
        "neutral_within_tolerance_only": within_tolerance,
        "scope2_residual_mix_blockers": residual_blockers,
        "claim_supportable": not residual_blockers,
        "framework": "ISO 14068-1 carbon neutrality",
        "basis": basis,
        "gross_tco2e": round(gross_t, 6),
        "credits_applied_tco2e": round(applied_total, 6),
        "credits_applied_removals_tco2e": round(removals_total, 6),
        "residual_tco2e": round(residual_t, 6),
        "neutral": neutral,
        "iso14068_conformant_claim": iso14068_conformant,
        "credits": [{
            "id": c.id, "registry": c.registry, "project_id": c.project_id,
            "vintage_year": c.vintage_year, "quantity_tco2e": c.quantity_tco2e,
            "credit_type": c.credit_type, "ccp_approved": c.ccp_approved,
            "vcmi_claim": c.vcmi_claim, "retirement_date": c.retirement_date,
        } for c in applied],
        "unretired_credits_in_register": n_unretired,
        "claim_warnings": warnings,
        "note": "Only retired credits applied to this run count. Reduction hierarchy "
                "(reduce first, offset the residual) is expected under ISO 14068; this "
                "assessment does not enforce prior-reduction evidence.",
    }
