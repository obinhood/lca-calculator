"""ISO 14068-1 carbon-neutrality accounting over the credits register.

Only RETIRED credits applied to a specific run count toward neutrality. The
residual after retirement determines arithmetic neutrality; a defensible CLAIM
additionally needs quality criteria (removals over avoidance, CCP-approved,
retired, reasonable vintage) — surfaced as claim warnings, plus the EU ECGT
restriction on offset-based "carbon neutral" product claims from Sept 2026.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CarbonCredit, CalculationRun


def neutrality_assessment(db: Session, organisation_id: int, run: CalculationRun,
                          basis: str = "location") -> dict:
    gross_kg = run.total_co2e if basis == "location" else run.total_co2e_market
    gross_t = (gross_kg or 0.0) / 1000.0

    applied = db.query(CarbonCredit).filter(
        CarbonCredit.organisation_id == organisation_id,
        CarbonCredit.retired.is_(True),
        CarbonCredit.applied_to_run_id == run.id).all()
    applied_total = sum(c.quantity_tco2e for c in applied)
    removals_total = sum(c.quantity_tco2e for c in applied if c.credit_type == "removal")

    residual_t = gross_t - applied_total
    neutral = residual_t <= 1e-9

    # Register hygiene (context, not claim-affecting on their own).
    n_unretired = db.query(CarbonCredit).filter(
        CarbonCredit.organisation_id == organisation_id,
        CarbonCredit.retired.is_(False)).count()

    warnings = []
    if not neutral:
        warnings.append(f"NOT neutral: {round(residual_t, 6)} tCO2e residual remains "
                        f"after applied retirements — retire more credits or reduce first")
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
