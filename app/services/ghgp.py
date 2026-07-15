"""GHG Protocol Scope 3 category taxonomy, derivation, and the completeness gate.

The platform previously keyed Scope 3 on free-text activity categories
("flight", "spend"), so a firm uploading electricity + gas + flights got
coverage_pct=100 and disclosure_ready=true while 12 of the 15 GHG Protocol
categories were invisibly absent. This module is the fix.

Governing rules:
  1. FAIL-OPEN ON THE NUMBER, FAIL-CLOSED ON THE DISCLOSURE. A Scope 3 line whose
     category cannot be derived still computes and still sums into total_co2e —
     dropping it would understate the footprint, the exact sin being fixed. It
     makes the run NOT disclosure_ready.
  2. A WRONG CATEGORY IS WORSE THAN A DECLARED GAP — ambiguous free-text is never
     auto-bound; it is surfaced with candidates.
  3. REPRODUCTION CONTRACT. A run's category breakdown and completeness statement
     are read ONLY from what was frozen onto the run (line details +
     RunScope3Declaration), never from the live activity/factor/declaration
     tables — so re-rendering a filed run years later returns the same statement.
  4. A NULL IS NOT A ZERO. Five states, never three.
"""
import json
from typing import Optional, Tuple

from sqlalchemy.orm import Session

# The taxonomy version frozen onto every run and every Scope 3 line.
GHGP_STANDARD_VERSION = "ghgp-scope3-2011"
# The version of OUR free-text -> category derivation map.
CATEGORY_MAP_VERSION = "s3map-v1"

# APPEND-ONLY. When the GHG Protocol re-cuts its categories, ADD a new version key;
# NEVER edit an existing entry — renderers resolve names from the version frozen on
# the run, so a filing keeps the category names it was made under, forever.
GHGP_TAXONOMIES = {
    "ghgp-scope3-2011": {
        1: dict(name="Purchased goods and services", direction="upstream",
                min_boundary="cradle_to_gate",
                accepts_boundary={"cradle_to_gate", "cradle_to_grave"},
                sale_year_lifetime=False),
        2: dict(name="Capital goods", direction="upstream",
                min_boundary="cradle_to_gate",
                accepts_boundary={"cradle_to_gate", "cradle_to_grave"},
                sale_year_lifetime=True),
        3: dict(name="Fuel- and energy-related activities (not included in scope 1 or scope 2)",
                direction="upstream", min_boundary="wtt_and_td_losses",
                accepts_boundary={"well_to_tank", "wtt", "td_loss"},
                sale_year_lifetime=False),
        4: dict(name="Upstream transportation and distribution", direction="upstream",
                min_boundary="supplier_scope1_2",
                accepts_boundary={"ttw", "wtw", "combustion", "scope1_2"},
                sale_year_lifetime=False),
        5: dict(name="Waste generated in operations", direction="upstream",
                min_boundary="supplier_scope1_2",
                accepts_boundary={"waste_treatment", "ttw", "wtw", "scope1_2"},
                sale_year_lifetime=False),
        6: dict(name="Business travel", direction="upstream",
                min_boundary="supplier_scope1_2",
                accepts_boundary={"ttw", "wtw", "combustion", "scope1_2"},
                sale_year_lifetime=False),
        7: dict(name="Employee commuting", direction="upstream",
                min_boundary="supplier_scope1_2",
                accepts_boundary={"ttw", "wtw", "combustion", "scope1_2"},
                sale_year_lifetime=False),
        8: dict(name="Upstream leased assets", direction="upstream",
                min_boundary="lessor_scope1_2",
                accepts_boundary={"ttw", "wtw", "generation", "scope1_2"},
                sale_year_lifetime=False),
        9: dict(name="Downstream transportation and distribution", direction="downstream",
                min_boundary="supplier_scope1_2",
                accepts_boundary={"ttw", "wtw", "combustion", "scope1_2"},
                sale_year_lifetime=False),
        10: dict(name="Processing of sold products", direction="downstream",
                 min_boundary="processor_scope1_2",
                 accepts_boundary={"ttw", "wtw", "scope1_2"}, sale_year_lifetime=False),
        11: dict(name="Use of sold products", direction="downstream",
                 min_boundary="direct_use_phase",
                 accepts_boundary=None,          # not assessable from factor boundary
                 sale_year_lifetime=True),
        12: dict(name="End-of-life treatment of sold products", direction="downstream",
                 min_boundary="supplier_scope1_2",
                 accepts_boundary={"waste_treatment", "ttw", "wtw", "scope1_2"},
                 sale_year_lifetime=True),
        13: dict(name="Downstream leased assets", direction="downstream",
                 min_boundary="lessee_scope1_2",
                 accepts_boundary={"generation", "ttw", "wtw", "scope1_2"},
                 sale_year_lifetime=False),
        14: dict(name="Franchises", direction="downstream",
                 min_boundary="franchisee_scope1_2",
                 accepts_boundary={"generation", "ttw", "wtw", "scope1_2"},
                 sale_year_lifetime=False),
        15: dict(name="Investments", direction="downstream",
                 min_boundary="investee_scope1_2_attributed",
                 accepts_boundary=None, sale_year_lifetime=False),
    },
}

CATEGORIES = tuple(range(1, 16))

# The seven relevance criteria a "not_material" exclusion must be screened against
# (GHG Protocol Scope 3 Standard Ch. 6; ESRS E1 AR 46(d)).
SEVEN_CRITERIA = ("size", "influence", "risk", "stakeholders", "outsourcing",
                  "sector_guidance", "other")

# The five states. `undeclared` is DERIVED (the absence of a live declaration) and
# is frozen onto the run as a first-class status, so a run's 15-row artifact is
# complete by construction — an assurer sees fifteen statements, not an absence.
STORABLE_STATUSES = ("included", "not_applicable", "not_material", "not_measured")
ALL_STATUSES = STORABLE_STATUSES + ("undeclared",)
# Statuses that can appear in a disclosure-ready inventory.
PASSING_STATUSES = ("included", "not_applicable", "not_material")

# "We have not measured it" is a disclosure of incompleteness, not a justification
# for excluding a category.
BOILERPLATE_JUSTIFICATIONS = {
    "", "n/a", "na", "none", "not measured", "no data", "tbd", "-", "unknown",
    "not applicable", "not material",
}
MIN_JUSTIFICATION_CHARS = 20


def taxonomy(version: Optional[str] = None) -> dict:
    return GHGP_TAXONOMIES[version or GHGP_STANDARD_VERSION]


def category_name(cat: int, version: Optional[str] = None) -> str:
    return taxonomy(version)[cat]["name"]


def boundary_meets_minimum(cat: int, lca_boundary: Optional[str]) -> Optional[bool]:
    """True | False | None(not assessable).

    None when the factor carries no lca_boundary (the DEFRA loader writes None) or
    the category's minimum boundary isn't checkable from a factor boundary. NEVER
    silently True — a machine assertion of "minimum boundary met" from absent data
    would be a lie, and nothing would be checked on day one.
    """
    accepts = taxonomy()[cat]["accepts_boundary"]
    if accepts is None or not lca_boundary:
        return None
    return lca_boundary.strip().lower() in accepts


# --- Derivation ---------------------------------------------------------------
# ONLY unambiguous mappings. Ambiguous free-text is ABSENT ON PURPOSE — absence
# resolves to `unassigned`, which is a hard blocker, not a guess.
CATEGORY_TO_GHGP = {
    "business_travel": 6,
    "commuting": 7,
    # "flight" -> 6 is safe ONLY because "commuting" is a separate platform category.
    # If commuting-by-air ever becomes representable here, DELETE this key and move
    # "flight" into AMBIGUOUS. No test can catch that for you.
    "flight": 6,
}

# Surfaced WITH candidates for a human to resolve — never auto-bound.
AMBIGUOUS = {
    "train": (6, 7),     # business travel vs commuting — depends on trip purpose
    "car": (6, 7),
    "waste": (5, 12),    # operational waste vs end-of-life of SOLD products
    "freight": (4, 9),   # upstream vs downstream — depends who paid for the leg
    "spend": (1, 2),     # goods/services vs capital goods — depends on the EEIO sector
    "water": (1, 5),     # supply (purchased good) vs wastewater treatment
}


def derive_ghgp_category(scope: str, activity_category: Optional[str],
                         explicit: Optional[int]) -> Tuple[Optional[int], str, Optional[list]]:
    """(category, source, candidates) for one line. Never guesses.

    source ∈ explicit | category_rule | ambiguous_unassigned | unassigned |
             invalid_explicit | conflict_non_scope3 | n/a_scope1 | n/a_scope2
    """
    cat = (activity_category or "").strip().lower()
    if scope != "3":
        if explicit is not None:
            # A Scope 3 category on a Scope 1/2 line is a contradiction: keep the
            # line (the number is still right) but BLOCK the disclosure.
            return None, "conflict_non_scope3", None
        return None, f"n/a_scope{scope}", None
    if explicit is not None:
        try:
            e = int(explicit)
        except (TypeError, ValueError):
            return None, "invalid_explicit", None
        if 1 <= e <= 15:
            return e, "explicit", None
        return None, "invalid_explicit", None
    if cat in CATEGORY_TO_GHGP:
        return CATEGORY_TO_GHGP[cat], "category_rule", None
    if cat in AMBIGUOUS:
        return None, "ambiguous_unassigned", list(AMBIGUOUS[cat])
    return None, "unassigned", None


UNASSIGNED_SOURCES = ("unassigned", "ambiguous_unassigned", "invalid_explicit",
                      "conflict_non_scope3")


def is_boilerplate(justification: Optional[str]) -> bool:
    j = (justification or "").strip()
    return j.lower() in BOILERPLATE_JUSTIFICATIONS or len(j) < MIN_JUSTIFICATION_CHARS


def declarations_fingerprint(decls) -> str:
    """Hash of the live declaration set, so a run can detect that the screen it
    froze has since been edited (an exclusion statement must not be forgeable
    after the fact)."""
    import hashlib
    parts = sorted(
        f"{d.category}:{d.status}:{(d.justification or '').strip()}:"
        f"{d.screening_estimate_tco2e}:{d.materiality_threshold_pct}:{d.screened_at}"
        for d in decls)
    return "s3decl-v1:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# --- The completeness gate ----------------------------------------------------

def scope3_completeness(db: Session, run) -> dict:
    """Blockers + warnings for one run, read ONLY from what the run froze.

    This is what stops "3 of 15 categories" reading as "100% complete".
    """
    from ..models import RunScope3Declaration, EmissionLineItem, Scope3CategoryDeclaration

    blockers, warnings = [], []

    # B1 — legacy run: never render a clean 15x0.0 table for a run that predates
    # the category dimension. It has no completeness statement to show.
    if not run.ghgp_standard_version:
        return {
            "assessable": False,
            "blockers": ["run predates the GHGP 15-category dimension — recompute to "
                         "produce a Scope 3 completeness statement (ESRS AR 46(i) / "
                         "IFRS S2 29(a)(vi))"],
            "warnings": [],
        }

    # B2 — a completeness assertion is inherently period-bound.
    if run.reporting_period_id is None:
        blockers.append("run is not scoped to a reporting period — a Scope 3 completeness "
                        "statement is period-bound; recompute against a reporting period")

    decls = {d.category: d for d in db.query(RunScope3Declaration)
             .filter(RunScope3Declaration.run_id == run.id).all()}

    # Frozen Scope 3 lines, grouped by category / source.
    lines_by_cat: dict = {}
    unassigned_sources: dict = {}
    boundary_fail: dict = {}
    for details, co2e in db.query(EmissionLineItem.details, EmissionLineItem.co2e)\
            .filter(EmissionLineItem.run_id == run.id,
                    EmissionLineItem.method == "location").all():
        d = json.loads(details or "{}")
        src = d.get("ghgp_category_source") or ""
        if src in UNASSIGNED_SOURCES:
            unassigned_sources[src] = unassigned_sources.get(src, 0) + 1
            continue
        cat = d.get("ghgp_category")
        if cat is None:
            continue
        lines_by_cat.setdefault(cat, []).append((d, co2e or 0.0))
        if d.get("ghgp_min_boundary_met") is False:
            boundary_fail[cat] = boundary_fail.get(cat, 0) + 1

    by_status: dict = {s: [] for s in ALL_STATUSES}
    for c in CATEGORIES:
        d = decls.get(c)
        by_status[d.status if d else "undeclared"].append(c)

    # B3 — the failure this whole change exists to kill.
    if by_status["undeclared"]:
        blockers.append(
            f"Scope 3 categories {by_status['undeclared']} are UNDECLARED — every one of the "
            f"15 GHG Protocol categories must be screened and either quantified or excluded "
            f"with a justification (GHGP Scope 3 Ch.5; ESRS AR 46(i))")
    # B4 — a known data gap must never pass as a zero.
    if by_status["not_measured"]:
        blockers.append(
            f"Scope 3 categories {by_status['not_measured']} are declared NOT MEASURED — a known "
            f"data gap cannot be disclosed as zero")
    # B5 — a Scope 3 line with no category (it IS in the total, but cannot be disclosed).
    if unassigned_sources:
        blockers.append(
            f"Scope 3 lines carry no GHGP category ({unassigned_sources}) — they ARE included in "
            f"total_co2e but cannot be attributed to a category; set activities.ghgp_category")

    for c in CATEGORIES:
        d = decls.get(c)
        if d is None:
            continue
        n_lines = len(lines_by_cat.get(c, []))
        # B6 — not_material must be SCREENED, not asserted.
        if d.status == "not_material":
            missing = []
            if d.screening_estimate_tco2e is None:
                missing.append("screening_estimate_tco2e")
            if d.materiality_threshold_pct is None:
                missing.append("materiality_threshold_pct")
            try:
                crit = json.loads(d.criteria or "{}")
            except ValueError:
                crit = {}
            absent = [k for k in SEVEN_CRITERIA if k not in crit or crit.get(k) is None]
            if absent:
                missing.append(f"criteria {absent}")
            if missing:
                blockers.append(f"category {c} declared NOT MATERIAL without {missing} — an "
                                f"immaterial exclusion must be screened against all seven "
                                f"relevance criteria with an estimate and a threshold")
        # B7 — boilerplate is not a justification.
        if d.status in ("not_applicable", "not_material", "not_measured") and \
                is_boilerplate(d.justification):
            blockers.append(f"category {c} excluded with a blank/boilerplate justification — "
                            f"state why the category does not occur or is immaterial "
                            f"(min {MIN_JUSTIFICATION_CHARS} chars)")
        # B8 — declared-vs-observed contradiction.
        if d.status in ("not_applicable", "not_material") and n_lines:
            blockers.append(f"category {c} declared {d.status} but the run contains {n_lines} "
                            f"emission line(s) in it — the declaration contradicts the data")
        if d.status == "included" and not n_lines and c != 15:
            blockers.append(f"category {c} declared INCLUDED but the run contains no emission "
                            f"lines in it")
        if d.status == "included" and is_boilerplate(d.method_description):
            blockers.append(f"category {c} declared INCLUDED without a method_description "
                            f"(ESRS AR 46(h))")
        # B12 — quantified BELOW the category's minimum boundary is a partial figure.
        if d.status == "included" and boundary_fail.get(c):
            blockers.append(f"category {c} has {boundary_fail[c]} line(s) whose factor does not "
                            f"meet the category's minimum boundary "
                            f"({taxonomy()[c]['min_boundary']}) — a PARTIAL category, not a "
                            f"compliant Cat-{c} figure (GHGP Table 5.4)")
        # W1 — can't assess the boundary at all (factor carries none).
        if d.status == "included" and n_lines:
            not_assessable = sum(1 for dd, _ in lines_by_cat[c]
                                 if dd.get("ghgp_min_boundary_met") is None)
            if not_assessable:
                warnings.append(f"category {c}: minimum boundary NOT ASSESSABLE for "
                                f"{not_assessable} line(s) — their factors carry no "
                                f"lca_boundary; Table 5.4 conformance rests on your declaration")
        # W2 — the engine's period model does not fit these categories.
        if d.status == "included" and taxonomy()[c]["sale_year_lifetime"]:
            warnings.append(f"category {c} is a lifetime/acquisition-year category, but the engine "
                            f"computes activity x factor for the PERIOD. If the uploaded quantity "
                            f"is not already the lifetime/acquisition quantity, this figure is "
                            f"UNDERSTATED — state the treatment in method_description")

    # B9 — anti-gaming: a category cannot be "not applicable" when the run's own
    # content proves the activity occurs.
    has_fuel_or_power = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id,
        EmissionLineItem.method == "location",
        EmissionLineItem.scope.in_(("1", "2"))).first() is not None
    if has_fuel_or_power and decls.get(3) is not None and decls[3].status == "not_applicable":
        blockers.append("category 3 (fuel- & energy-related activities) declared NOT APPLICABLE "
                        "while the run reports Scope 1/2 energy — upstream fuel/T&D emissions "
                        "necessarily occur")
    if lines_by_cat.get(5) and decls.get(5) is not None and decls[5].status == "not_applicable":
        blockers.append("category 5 declared NOT APPLICABLE while waste lines exist")

    # B10 — the screen was edited after the run froze it (forgery-by-edit).
    if run.reporting_period_id is not None and run.scope3_declaration_fingerprint:
        live = db.query(Scope3CategoryDeclaration).filter(
            Scope3CategoryDeclaration.organisation_id == run.organisation_id,
            Scope3CategoryDeclaration.reporting_period_id == run.reporting_period_id).all()
        if declarations_fingerprint(live) != run.scope3_declaration_fingerprint:
            blockers.append("the Scope 3 screen has been EDITED since this run froze it — the "
                            "run's exclusion statement no longer matches the live declarations; "
                            "recompute so the filed statement is the one you screened")

    # --- Category 15: PCAF financed emissions (frozen onto the run) ---
    from ..models import FinancedPosition, RunFinancedLine
    from ..services.calc import _financed_fingerprint, financed_included_positions
    n_financed_lines = db.query(RunFinancedLine).filter(
        RunFinancedLine.run_id == run.id).count()
    # B13 — the run holds BOTH activity-derived Cat-15 lines and PCAF financed lines.
    # The platform refuses to sum them (an equity-stake activity and a loan book are
    # different accounting; investee-vs-own double counting is out of reach).
    if n_financed_lines and len(lines_by_cat.get(15, [])):
        blockers.append("Cat 15 has BOTH activity-derived lines and PCAF financed lines — "
                        "the platform will not sum them; move investments to FinancedPosition "
                        "or remove the activity-derived Cat 15 lines")
    positions = db.query(FinancedPosition).filter(
        FinancedPosition.organisation_id == run.organisation_id).all()
    if positions:
        # B9 (Cat 15) — can't be "not applicable" when the org holds financed positions.
        if decls.get(15) is not None and decls[15].status == "not_applicable":
            blockers.append("category 15 (investments) declared NOT APPLICABLE while the org "
                            "holds financed positions — financed emissions necessarily occur")
        # B14a — positions exist but financed emissions are not in the run's figure.
        if run.financed_co2e is None:
            if run.financed_as_of:
                # The freeze left it None because the as_of cutoff excluded EVERY
                # position (the silent-empty-portfolio case, surfaced here).
                blockers.append(f"the financed as_of {run.financed_as_of} excluded every financed "
                                f"position although the org holds {len(positions)} — recompute with "
                                "a valid as_of (check the positions' as_of dates)")
            else:
                blockers.append(f"{len(positions)} financed position(s) exist but this run did not "
                                "evaluate financed emissions (Scope 3 Cat 15) — recompute with "
                                "financed emissions included")
        else:
            # B14b — the positions that FED this run's Cat 15 figure changed since it
            # was filed. Fingerprint the as_of-included set, so a position dated AFTER
            # the cutoff (not in the figure) does not false-flag a correct run.
            included = financed_included_positions(positions, run.financed_as_of)
            if run.financed_fingerprint and _financed_fingerprint(included) != run.financed_fingerprint:
                blockers.append("the financed positions feeding this run's Cat 15 changed since it "
                                "was filed — the frozen figure no longer matches; recompute")

    accounted = sum(1 for c in CATEGORIES
                    if decls.get(c) is not None and decls[c].status in PASSING_STATUSES)
    return {
        "assessable": True,
        "blockers": blockers,
        "warnings": warnings,
        "by_status": {s: by_status[s] for s in ALL_STATUSES},
        "categories_accounted_for": accounted,
        "inventory_coverage_pct": round(100.0 * accounted / 15.0, 2),
        "unassigned_sources": unassigned_sources,
    }
