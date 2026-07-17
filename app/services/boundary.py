"""GHG Protocol Corporate Standard Ch.3 — the organisational boundary.

The consolidation approach decides WHAT SHARE of each operation's emissions enters
the inventory. It was previously declared on the Organisation but never applied, so
a 40%-owned JV was counted at 100% — a 2.5x overstatement.

Doctrine (inherited from ghgp.py):
  1. FAIL-OPEN ON THE NUMBER, FAIL-CLOSED ON THE DISCLOSURE. An unresolvable share
     includes the line at 100% — the OVERSTATING direction — and returns
     resolved=False, which hard-blocks the disclosure. Understating is the one thing
     that must never happen silently, so a missing fact is never treated as 0%.
  2. A NULL IS NOT A FALSE. operational_control IS NULL means "not asserted", never
     "no control".
  3. REPRODUCTION CONTRACT. The boundary is frozen onto the run; renderers read only
     frozen state, so a later ownership change is DETECTED drift, never a silent
     restatement of a filed figure.
  4. accounting_category NEVER appears in a weight branch. This is the single most
     important property of entity_weight(): under a control approach the SAME 20%
     associate is consolidated at 100% or 0% purely on whether control is asserted
     (IFRS S2 educational material Ex. 2A vs 2B). Any scheme deriving inclusion from
     `equity_share_pct >= 50` is wrong. The category drives DISCLOSURE only.
"""
import hashlib
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from .ghgp import is_boilerplate, MIN_JUSTIFICATION_CHARS

BOUNDARY_VERSION = "ghgp-corporate-2004/ch3"

# Code-level whitelist, deliberately NOT a DB CheckConstraint: organisations is an FK
# target (a constraint would need batch_alter_table), and the Corporate Standard is
# under revision with the consolidation approaches themselves in scope — the valid set
# must be changeable without a migration.
APPROACHES = ("operational_control", "financial_control", "equity_share")

ACCOUNTING_CATEGORIES = (
    "subsidiary", "joint_venture_incorporated", "joint_operation", "associate",
    "fixed_asset_investment", "franchise", "lease_finance", "lease_operating",
)

# Where an operation EXCLUDED by the boundary belongs in Scope 3 (Scope 3 Standard
# §5.2). Used by the gate to demand the preparer declare it — the platform measures
# the hole, it never invents the routing (see the module note on B8 below).
_RESIDUAL_SCOPE3_CATEGORY = {
    "associate": 15, "joint_venture_incorporated": 15, "joint_operation": 15,
    "fixed_asset_investment": 15,
    "lease_finance": 8, "lease_operating": 8,      # upstream leased assets (13 if downstream)
    "franchise": 14,
    "subsidiary": 15,
}


def entity_weight(approach: str, e) -> Tuple[float, str, bool]:
    """(share_factor, share_basis, resolved) for ONE entity under ONE approach.

    ``e is None`` => the reporting organisation itself.

    accounting_category appears in NO branch below — deliberately (see module doc).
    """
    if e is None:
        return 1.0, "reporting_entity_itself", True

    if approach == "equity_share":
        # Economic interest, INDEPENDENT of control: the 40% JV is 40% whether or not
        # you operate it; a 100%-operated leased asset with no equity is 0%.
        if e.equity_share_pct is None:
            return 1.0, "unresolved_no_equity_share_pct", False
        return e.equity_share_pct / 100.0, "equity_share_pct", True

    if approach == "financial_control":
        if e.joint_financial_control is True:
            # The one place a control approach falls back to a percentage.
            if e.equity_share_pct is None:
                return 1.0, "unresolved_joint_fc_no_equity_pct", False
            return e.equity_share_pct / 100.0, "joint_financial_control_equity_share", True
        if e.financial_control is None:
            return 1.0, "unresolved_financial_control_not_asserted", False
        # Explicitly CAN be True below 50% ownership — never derived from equity %.
        if e.financial_control:
            return 1.0, "financial_control", True
        # financial_control is False. Excluding at 0% is only honest if the preparer
        # ALSO asserted the entity is not JOINTLY controlled: a 50/50 JV whose joint
        # control was never asserted would otherwise vanish at 0% with resolved=True —
        # a SILENT UNDERSTATEMENT, the one failure the doctrine forbids. NULL is not
        # a False.
        if e.joint_financial_control is None:
            return 1.0, "unresolved_joint_financial_control_not_asserted", False
        return 0.0, "no_financial_control", True

    if approach == "operational_control":
        # An asserted judgement: 0% equity + operational control => 100%.
        if e.operational_control is None:
            return 1.0, "unresolved_operational_control_not_asserted", False
        return (1.0, "operational_control", True) if e.operational_control \
            else (0.0, "no_operational_control", True)

    return 1.0, "unresolved_unknown_approach", False


def group_class(e) -> str:
    """IFRS S2 29(a)(iv) / ESRS E1 disaggregation bucket.

    Derived from in_consolidated_accounting_group — a FINANCIAL-reporting fact — not
    from the GHGP accounting_category and not from the share: both clauses split on
    the financial-statement group. A 0.0-weight entity still gets its real bucket;
    those rows ARE the "other investees excluded" list the clauses ask for.
    """
    if e is None:
        return "consolidated_accounting_group"       # the reporting parent
    if e.in_consolidated_accounting_group is True:
        return "consolidated_accounting_group"
    if e.in_consolidated_accounting_group is False:
        return "other_investee"
    return "unclassified"                            # NULL -> blocker B10


def consolidation_fingerprint(approach: Optional[str], reason: Optional[str], entities) -> str:
    """Hash of the boundary DETERMINANTS.

    activities_fingerprint hashes activities and is structurally blind to an
    equity_share_pct 40->100 edit or an approach flip — either of which changes every
    number while every run still reports FRESH. This closes that.
    """
    parts = sorted(
        f"{e.id}:{e.accounting_category}:{e.equity_share_pct}:{e.financial_control}:"
        f"{e.joint_financial_control}:{e.operational_control}:"
        f"{e.in_consolidated_accounting_group}:{e.effective_from}:{e.effective_to}"
        for e in entities)
    payload = f"{approach}|{(reason or '').strip()}|" + "|".join(parts)
    return "cons-v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def boundary_completeness(db: Session, run) -> dict:
    """Blockers + warnings for one run's organisational boundary, from FROZEN state."""
    from ..models import RunEntityBoundary, ReportingEntity, Organisation

    # B1 — legacy run: never render a boundary claim it never made.
    if not run.boundary_version:
        return {
            "assessable": False,
            "blockers": ["run predates the GHGP organisational-boundary dimension — "
                         "recompute to produce a consolidation statement (GHGP Ch.3)"],
            "warnings": [],
        }

    blockers, warnings = [], []
    rows = db.query(RunEntityBoundary).filter(RunEntityBoundary.run_id == run.id).all()

    # B2 — unknown approach.
    if run.consolidation_approach not in APPROACHES:
        blockers.append(f"unknown consolidation approach {run.consolidation_approach!r} — "
                        f"must be one of {list(APPROACHES)}")

    # B3/B4/B5 — an unresolved share. The emissions are IN the total at 100% (never
    # understated), but the boundary cannot be honestly disclosed.
    unresolved = [r for r in rows if not r.resolved]
    if unresolved:
        not_found = [r for r in unresolved if r.share_basis == "unresolved_entity_not_found"]
        others = [r for r in unresolved if r.share_basis != "unresolved_entity_not_found"]
        if not_found:
            blockers.append(
                f"{len(not_found)} activity group(s) point at an entity that does not exist for "
                f"this organisation — their emissions are included at 100% (never understated) "
                f"but cannot be attributed; fix the activity attribution")
        if others:
            names = sorted({r.entity_name for r in others})
            bases = sorted({r.share_basis for r in others})
            blockers.append(
                f"entities {names} have an UNRESOLVED share under {run.consolidation_approach} "
                f"({bases}) — their emissions are included at 100% (never understated) but the "
                f"boundary cannot be disclosed; assert the missing fact")

    # B10 — no financial-statement consolidation status: the S2/ESRS disaggregation
    # between the accounting group and other investees is not derivable.
    unclassified = [r.entity_name for r in rows if r.group_class == "unclassified"]
    if unclassified:
        blockers.append(
            f"entities {sorted(unclassified)} have no financial-statement consolidation status "
            f"asserted — IFRS S2 29(a)(iv) requires Scope 1 and 2 to be disaggregated between "
            f"the consolidated accounting group and other investees")

    # B6 — the reason for the approach. Fail-closed ONLY where the approach actually
    # determines the number (the org holds entities); a single-entity org whose
    # approach changes nothing gets a warning, not a false blocker.
    has_entities = any(r.entity_key != "self" for r in rows)
    if is_boilerplate(run.consolidation_reason):
        msg = (f"the consolidation approach ({run.consolidation_approach}) is declared without a "
               f"reason for the choice (>= {MIN_JUSTIFICATION_CHARS} chars) — GHG Protocol Ch.3 "
               f"asks a company to state and justify its chosen approach")
        if has_entities:
            blockers.append(msg + "; required because your boundary spans multiple entities, so "
                                  "the approach determines the reported figures")
        else:
            warnings.append(msg)

    # B7 — forgery-by-edit: the boundary changed since the run froze it.
    org = db.get(Organisation, run.organisation_id)
    live_entities = db.query(ReportingEntity).filter(
        ReportingEntity.organisation_id == run.organisation_id)\
        .order_by(ReportingEntity.id).all()
    live_fp = consolidation_fingerprint(
        (org.consolidation_approach or "operational_control") if org else None,
        org.consolidation_approach_reason if org else None, live_entities)
    if run.consolidation_fingerprint and live_fp != run.consolidation_fingerprint:
        blockers.append(
            "the organisational boundary has been EDITED since this run froze it — the filed "
            "figures no longer match the declared boundary; recompute. A change of consolidation "
            "approach is a change to the inventory boundary and triggers a GHG Protocol Ch.5 "
            "base-year recalculation assessment (organic growth does not)")

    # B8 — the residual has no Scope 3 home. Applying a share without re-routing the
    # excluded operations creates a REAL completeness hole; the platform measures it
    # and refuses to file, but will NOT invent the routing (a wrong category is worse
    # than a declared gap).
    residual = run.total_co2e_non_consolidated or 0.0
    if residual > 0:
        from ..models import RunScope3Declaration
        decls = {d.category: d for d in db.query(RunScope3Declaration)
                 .filter(RunScope3Declaration.run_id == run.id).all()}
        # Only entities that actually contributed an excluded residual: a declared
        # entity with no activities in this run has nothing to re-route, so demanding
        # a Scope 3 declaration for it would be a false blocker.
        excluded = [r for r in rows
                    if r.entity_key != "self" and r.gross_co2e > r.consolidated_co2e]
        needed = sorted({_RESIDUAL_SCOPE3_CATEGORY.get(r.accounting_category, 15)
                         for r in excluded})
        undeclared = [c for c in needed
                      if decls.get(c) is None
                      or decls[c].status not in ("included", "not_material")]
        if undeclared:
            names = sorted({r.entity_name for r in excluded})
            blockers.append(
                f"{round(residual, 3)} kgCO2e were EXCLUDED from the inventory by the "
                f"{run.consolidation_approach} boundary ({names}). Scope 3 Standard section 5.2: "
                f"operations excluded by the boundary move to the Scope 3 inventory "
                f"(category {undeclared}). This platform does NOT auto-route them — declare and "
                f"quantify the category, or the inventory is incomplete")

    # B9 — an entity inside the boundary for only part of the period.
    if run.reporting_period_id is not None:
        from ..models import ReportingPeriod
        from .calc import _parse_iso_date
        p = db.get(ReportingPeriod, run.reporting_period_id)
        if p is not None:
            ps, pe = _parse_iso_date(p.start_date), _parse_iso_date(p.end_date)
            for r in rows:
                ef, et = _parse_iso_date(r.effective_from), _parse_iso_date(r.effective_to)
                if (ef and ps and ef > ps) or (et and pe and et < pe):
                    blockers.append(
                        f"entity {r.entity_name} was inside the boundary for only part of the "
                        f"reporting period — the engine does not time-slice a share within a "
                        f"period; split the reporting period")

    # --- warnings ---
    for r in rows:
        if r.entity_key != "self" and r.line_count == 0:
            warnings.append(f"entity {r.entity_name} is declared but has no activities in this "
                            f"run — a data-attribution gap?")
        if r.equity_share_pct is not None and is_boilerplate(r.equity_share_basis):
            warnings.append(f"entity {r.entity_name} asserts an equity share with no basis — GHG "
                            f"Protocol: economic substance overrides legal ownership; record why")
        if (r.accounting_category == "lease_finance"
                and run.consolidation_approach == "equity_share"
                # `or 0` would read an UNASSERTED share as 0% and warn about a fact the
                # preparer never gave — the unasserted case is already a blocker.
                and r.equity_share_pct is not None
                and r.equity_share_pct < 100):
            warnings.append(f"entity {r.entity_name} is a finance lease with an equity share below "
                            f"100% under equity_share — check the capitalisation basis")

    return {"assessable": True, "blockers": blockers, "warnings": warnings,
            "approach": run.consolidation_approach,
            "entities": len([r for r in rows if r.entity_key != "self"]),
            "non_consolidated_kg": residual}
