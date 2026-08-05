"""EU CSDDD (Corporate Sustainability Due Diligence Directive) — Article 22 climate
transition-plan CARBON INPUTS readiness, and nothing more.

The CSDDD is overwhelmingly a due-diligence PROCESS directive: identifying, preventing,
mitigating and ending adverse human-rights and environmental impacts across a company's chain
of activities, with stakeholder engagement, a notification/complaints mechanism, monitoring
and public communication (Articles 5-16). A carbon-accounting engine touches essentially none
of that.

The ONE place the platform's data is genuinely an input is Article 22: the obligation to adopt
and put into effect a climate-change transition plan aligned with limiting warming to 1.5°C,
including time-bound (2030 and five-yearly to 2050) and absolute GHG reduction targets. A
quantified GHG inventory and a science-based target with a trajectory are exactly the CARBON
INPUTS that plan is built on — and the platform already produces both (via the ISSB S2 and
SBTi renderers).

So this renderer is a narrow, honest EVIDENCE MAP: for each carbon input to the Article 22
plan, can the platform evidence it, and is that evidence sound? It is emphatic about what it is
NOT — it does not produce the transition plan, the due-diligence process, adverse-impact work,
stakeholder engagement, or any assertion of CSDDD compliance. `report_scope` and a long
`not_produced` list carry that; the readiness signal is scoped precisely to the carbon inputs.

Same honest-scope / one-source-of-truth doctrine as the TCFD and PEF renderers: the inventory
evidence is sourced from the ISSB S2 renderer and the target evidence from the SBTi renderer —
no re-derivation.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import EmissionsTarget
from ..services.sbti import SBTI_MIN_ANNUAL_RATE
from .issb_s2 import issb_s2_report
from .sbti import sbti_report

# CSDDD obligations the platform does NOT touch — listed so the narrow scope is unmistakable.
_NOT_PRODUCED = [
    "the climate transition plan itself (Art 22) — its key actions, the investment/funding "
    "quantification, and the role of the administrative/management bodies; the platform "
    "supplies only the GHG inventory and target INPUTS a plan is built on",
    "the entire risk-based due-diligence PROCESS (Art 5-16): identifying, preventing, "
    "mitigating and bringing to an end adverse impacts across the chain of activities",
    "human-rights and non-climate environmental adverse impacts (biodiversity, pollution, "
    "water, deforestation, labour) — CSDDD is far broader than climate",
    "stakeholder engagement, the notification/complaints mechanism, and monitoring of measures",
    "public communication / the annual statement, and adoption of the plan by the company's "
    "governance bodies",
    "any assertion of CSDDD compliance — this is an evidence-readiness map of carbon inputs, "
    "not a legal-compliance determination",
]


def csddd_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
                 target_id: Optional[int] = None, current_year: Optional[int] = None) -> dict:
    """Readiness of the CARBON INPUTS to a CSDDD Article 22 climate transition plan: the GHG
    inventory (from ISSB S2) and a science-based reduction target with trajectory (from SBTi).
    """
    # --- Input 1: the GHG inventory baseline (ISSB S2, one source of truth). ---
    s2 = issb_s2_report(db, organisation_id, run_id=run_id)
    inv_ready = bool(s2.get("disclosure_ready"))
    inventory = {
        "required_for": "Art 22 GHG reduction targets are set against a quantified Scope 1/2/3 "
                        "inventory baseline",
        "present": s2.get("run") is not None,
        "ready": inv_ready,
        "ghg_emissions_tco2e": s2.get("ghg_emissions_tco2e"),
        "blockers": list(s2.get("blockers", [])),
        "source": "ISSB S2 (/reports/issb_s2) — same immutable run",
        "run": s2.get("run"),
    }

    # --- Input 2: a science-based reduction target with a trajectory (SBTi). ---
    any_target = db.query(EmissionsTarget).filter(
        EmissionsTarget.organisation_id == organisation_id).count() > 0
    targets = {
        "required_for": "Art 22 requires time-bound (2030, five-yearly to 2050) and absolute "
                        "GHG reduction targets aligned with 1.5°C",
        "present": False,
        "ready": False,
        "source": "SBTi (/reports/sbti)",
    }
    if target_id is not None:
        # SBTi treats a current run without a year as an error ("current_year required"). Only
        # ask it to place a trajectory when BOTH are given; otherwise evaluate target readiness
        # on its own so a missing year never spuriously blocks the Art 22 target evidence.
        _traj_run = run_id if current_year is not None else None
        sb = sbti_report(db, organisation_id, target_id, current_run_id=_traj_run,
                         current_year=current_year)
        # "target" is present only once BOTH the target AND its base run resolve (SBTi's early
        # returns omit it), so `found` already means the target input itself is sound.
        found = "target" in sb
        aa = sb.get("ambition_assessment") or {}

        # Art 22 requires alignment with 1.5°C SPECIFICALLY. SBTi's meets_minimum is
        # ambition-RELATIVE — a WB2C target clears its own 2.5%/yr floor — so it does NOT prove
        # 1.5°C alignment. Gate on the ACTUAL 1.5°C criterion: a near-term target's implied
        # linear rate must clear the 1.5°C floor (4.2%/yr) regardless of the declared ambition
        # label; a net-zero/long-term target (>=90% absolute) is 1.5°C-aligned by SBTi.
        ttype = aa.get("target_type")
        if ttype in ("long_term", "net_zero"):
            meets_1p5c = bool(aa.get("meets_minimum"))
        else:
            _rate = aa.get("implied_annual_linear_rate")
            meets_1p5c = (_rate is not None
                          and _rate + 1e-9 >= SBTI_MIN_ANNUAL_RATE["1.5C"])
        meets_1p5c = bool(found and meets_1p5c)

        # Target-INPUT readiness = the target is sound (found) AND 1.5°C-aligned. It must NOT
        # depend on whether a supplementary TRAJECTORY can be placed against a particular
        # current run — those blockers (year < base year, GWP mismatch vs the base run, a
        # base-year recalculation) are surfaced for transparency but do not drag readiness.
        target_ready = meets_1p5c
        _sb_blockers = list(sb.get("blockers", []))
        targets.update({
            "present": found,
            "ready": target_ready,
            "meets_1p5c_minimum": meets_1p5c,
            "blockers": _sb_blockers,
            # When the target resolved, any SBTi blockers are optional trajectory-PLACEMENT
            # issues that do NOT affect target-input readiness; say so. (When not found, the
            # blockers are the not-found reason and this note stays absent.)
            "blockers_note": ("these are optional trajectory-placement issues and do not affect "
                              "target-input readiness" if (found and _sb_blockers) else None),
            "target": sb.get("target"),
            "ambition_assessment": sb.get("ambition_assessment"),
            "trajectory": sb.get("trajectory"),
        })
    else:
        # No specific target chosen: say so honestly, and note whether ANY target exists to pick.
        targets["note"] = (
            "no target_id supplied — pass one to evidence the Art 22 reduction targets. "
            + ("The organisation has at least one target to reference."
               if any_target else "The organisation has NO emissions target on record yet."))

    # The carbon INPUTS are ready only when BOTH a sound inventory baseline and a sound
    # science-based target are in place. This is NOT a CSDDD-compliance or transition-plan
    # readiness claim (see report_scope / not_produced) — only the carbon inputs to Art 22.
    inputs_ready = bool(inventory["ready"] and targets["ready"])

    return {
        "framework": "EU CSDDD (Corporate Sustainability Due Diligence Directive)",
        "report_scope": (
            "A READINESS MAP of the CARBON INPUTS to a CSDDD Article 22 climate transition plan "
            "— the GHG inventory and a science-based reduction target. It is NOT the transition "
            "plan, NOT the due-diligence process (Art 5-16), NOT the human-rights or non-climate "
            "environmental impact work, and NOT an assertion of CSDDD compliance."),
        "climate_transition_plan_carbon_inputs_ready": inputs_ready,
        "article_22_carbon_inputs": {
            "ghg_inventory": inventory,
            "science_based_targets": targets,
        },
        "not_produced": _NOT_PRODUCED,
        "methodology": (
            "Evidence-readiness of the two carbon inputs to a CSDDD Art 22 climate transition "
            "plan: the GHG inventory sourced from the ISSB S2 renderer and the science-based "
            "target + trajectory from the SBTi renderer, both over the organisation's immutable "
            "runs (one source of truth). CSDDD's due-diligence process, non-climate impacts and "
            "the transition plan itself are out of scope and listed under not_produced."),
    }
