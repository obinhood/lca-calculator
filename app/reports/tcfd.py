"""TCFD (Task Force on Climate-related Financial Disclosures) — a CROSS-REFERENCE MAP, not
a report the engine authors.

The TCFD disbanded in 2023 and its monitoring passed to the ISSB; IFRS S2 is built directly
on the four TCFD pillars, so an organisation reporting under S2 is already producing TCFD
content. TCFD itself is still named by regimes that have not moved to ISSB — California
SB 261, Switzerland's climate ordinance, Brazil, and others — which is why a TCFD-shaped
view still has a job to do.

That job is HONEST MAPPING, not fabrication. TCFD has 11 recommended disclosures across four
pillars, and only ONE of them — Metrics & Targets (b), gross Scope 1/2/3 GHG — is something a
calculation engine produces. The other ten are governance, strategy, scenario-resilience and
risk-management NARRATIVES that a carbon platform does not and cannot write. So this renderer:

  * maps the platform's quantitative output (sourced from the built ISSB S2 renderer, so the
    numbers are byte-identical to what S2 discloses — one source of truth, no re-derivation)
    onto Metrics & Targets (b);
  * marks every other recommended disclosure `produced: false` with an explicit
    "narrative — preparer-supplied" source, so a reader sees the whole framework and exactly
    which slivers are machine-backed;
  * NEVER claims a complete TCFD report is ready. `report_scope` says so at the top, and the
    readiness signal that IS meaningful — whether the quantitative Metrics & Targets core is
    sound — is reported under its own precise name, mirroring the underlying S2 gates.

Same honest-scope doctrine as the EPD/RICS renderers: surface exactly what is and isn't
covered; do not let a partial artefact read as the whole standard.
"""
from typing import Optional

from sqlalchemy.orm import Session

from .issb_s2 import issb_s2_report

# The 11 TCFD recommended disclosures. `produced` marks the ONLY one a carbon engine backs
# with computed figures; the rest are narrative the preparer must author.
_PILLARS = [
    {"pillar": "Governance", "disclosures": [
        {"ref": "Governance (a)", "topic": "board oversight of climate risks & opportunities",
         "produced": False},
        {"ref": "Governance (b)", "topic": "management's role in assessing & managing them",
         "produced": False},
    ]},
    {"pillar": "Strategy", "disclosures": [
        {"ref": "Strategy (a)", "topic": "climate risks & opportunities over short/medium/long term",
         "produced": False},
        {"ref": "Strategy (b)", "topic": "impact on businesses, strategy and financial planning",
         "produced": False},
        {"ref": "Strategy (c)", "topic": "resilience under climate scenarios incl. 2°C or lower",
         "produced": False},
    ]},
    {"pillar": "Risk Management", "disclosures": [
        {"ref": "Risk Management (a)", "topic": "processes for identifying & assessing climate risks",
         "produced": False},
        {"ref": "Risk Management (b)", "topic": "processes for managing climate risks",
         "produced": False},
        {"ref": "Risk Management (c)", "topic": "integration into overall risk management",
         "produced": False},
    ]},
    {"pillar": "Metrics & Targets", "disclosures": [
        {"ref": "Metrics & Targets (a)", "topic": "metrics used to assess climate risks & opportunities",
         "produced": False,
         "note": "GHG intensity per revenue is available via GRI 305-4 (/reports/gri) when a "
                 "denominator is supplied; the broader risk/opportunity metric set is narrative."},
        {"ref": "Metrics & Targets (b)", "topic": "gross Scope 1, Scope 2 and Scope 3 GHG emissions",
         "produced": True},
        {"ref": "Metrics & Targets (c)", "topic": "climate targets and performance against them",
         "produced": False,
         "note": "Science-based targets are tracked by the SBTi module (/reports/sbti); this "
                 "TCFD map does not fold them in."},
    ]},
]

_NARRATIVE_NOT_PRODUCED = [
    "Governance (a)(b): board and management oversight narratives",
    "Strategy (a)(b): climate risks/opportunities and their business & financial impact",
    "Strategy (c): climate scenario analysis and resilience (incl. a 2°C-or-lower scenario)",
    "Risk Management (a)(b)(c): risk identification, management and integration processes",
    "Metrics & Targets (a): the full climate risk/opportunity metric set beyond GHG",
    "Metrics & Targets (c): targets and performance (see the SBTi module separately)",
]


def tcfd_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
                jurisdiction_reference: Optional[str] = None) -> dict:
    """Map the platform's quantitative climate output onto the four TCFD pillars.

    `jurisdiction_reference` is a free-text note of WHY TCFD (not ISSB S2) is being reported
    against — e.g. "California SB 261" — recorded for traceability only.
    """
    # Single source of truth for the numbers: the built ISSB S2 renderer. TCFD is S2's
    # parent framework, so S2's gross Scope 1/2/3 IS the Metrics & Targets (b) content.
    s2 = issb_s2_report(db, organisation_id, run_id=run_id)

    # The meaningful readiness signal is whether that quantitative core is sound — mirror
    # S2's own gates verbatim. This is NOT "a TCFD report is ready" (see report_scope).
    metrics_ready = bool(s2.get("disclosure_ready"))
    metrics_blockers = list(s2.get("blockers", []))
    ghg = s2.get("ghg_emissions_tco2e")

    # Attach the computed figures to Metrics & Targets (b); leave every other disclosure as
    # an explicit narrative gap. Build a fresh copy so the module-level template is never
    # mutated across requests.
    pillars = []
    for p in _PILLARS:
        discs = []
        for d in p["disclosures"]:
            row = dict(d)
            if row["ref"] == "Metrics & Targets (b)":
                row["source"] = "ISSB S2 (/reports/issb_s2) — same immutable run"
                row["ghg_emissions_tco2e"] = ghg
                row["quantitative_core_ready"] = metrics_ready
                row["blockers"] = metrics_blockers
            else:
                row["source"] = "narrative — preparer-supplied (not produced by the platform)"
            discs.append(row)
        pillars.append({"pillar": p["pillar"], "recommended_disclosures": discs})

    return {
        "framework": "TCFD (Task Force on Climate-related Financial Disclosures)",
        # This is the load-bearing honesty statement: never let this read as a whole TCFD report.
        "report_scope": (
            "A CROSS-REFERENCE MAP of the platform's quantitative climate output onto the four "
            "TCFD pillars — NOT a complete TCFD disclosure. Only Metrics & Targets (b) (gross "
            "Scope 1/2/3 GHG) is produced by the platform; the other ten recommended "
            "disclosures — governance, strategy, scenario-resilience and risk-management "
            "narratives, plus the non-GHG metrics (a) and targets (c) — are the preparer's to "
            "author."),
        "status_note": (
            "TCFD disbanded in 2023; its framework is delivered through ISSB IFRS S2, on which "
            "the GHG figures here are sourced. Report against TCFD only where a regime still "
            "requires it rather than ISSB S2."),
        "jurisdiction_reference": (jurisdiction_reference or "").strip() or None,
        # Precisely-named readiness: ONLY the Metrics & Targets (b) gross-GHG core, not the
        # whole M&T pillar ((a) intensity and (c) targets are NOT produced) and not the report.
        "metrics_and_targets_b_quantitative_ready": metrics_ready,
        "metrics_and_targets_b_quantitative_blockers": metrics_blockers,
        "run": s2.get("run"),
        "pillars": pillars,
        "narrative_disclosures_not_produced": _NARRATIVE_NOT_PRODUCED,
        "methodology": (
            "TCFD 11 recommended disclosures across four pillars. Metrics & Targets (b) is "
            "sourced verbatim from the ISSB S2 renderer over the same immutable calculation "
            "run (one source of truth; identical figures). All governance/strategy/risk and "
            "the non-GHG metrics and targets disclosures are narrative and are not produced."),
    }
