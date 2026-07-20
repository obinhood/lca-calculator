"""GHG Protocol Land Sector and Removals Guidance (LSRG) — the removals gate.

Inventory removals are the org's OWN carbon removals within its boundary
(technological: DAC+storage, BECCS, enhanced weathering; land-based: afforestation,
soil carbon, biochar). They are reported SEPARATELY from gross emissions — never
netted into total_co2e — and are distinct from a purchased offset credit (CarbonCredit)
and from biogenic-CO2 flux.

This gate reads ONLY frozen run state (run columns + run_removal_lines) and, like the
Scope 3 and boundary gates, fails closed: a removal with no permanence/monitoring
basis, a removal also sold as a credit, an as_of that fabricates a zero, or a ledger
edited after filing all BLOCK the disclosure. Permanence is disclosed as recorded and
NEVER used to discount or credit — the engine refuses to overclaim durability.
"""
import json

from sqlalchemy.orm import Session


def _net_removals_kg(run) -> float:
    """Gross removals minus reversals booked this period (may be negative)."""
    if run.total_removals_co2e is None:
        return 0.0
    return run.total_removals_co2e - (run.removals_reversed_co2e or 0.0)


def removals_completeness(db: Session, run) -> dict:
    """Blockers + warnings for one run's inventory removals, from FROZEN state."""
    from ..models import RemovalRecord, RunRemovalLine, CarbonCredit
    from ..services.calc import _removals_fingerprint

    # Scope to the run's period EXACTLY as compute_co2e's auto-detect does (calc.py),
    # so "does this run have removals it should have evaluated?" matches reality. Org-
    # wide here would false-flag a modern FY25 run as "legacy" merely because an FY24
    # removal exists in another period — and recompute could never clear it.
    rq = db.query(RemovalRecord).filter(RemovalRecord.organisation_id == run.organisation_id)
    if run.reporting_period_id is not None:
        rq = rq.filter(RemovalRecord.reporting_period_id == run.reporting_period_id)
    org_records = rq.all()

    # R1 — legacy: a run computed before the dimension existed, for a period that DOES
    # hold removal records. (No version + no in-period records = nothing to assess.)
    if not run.removals_lsrg_version:
        if org_records:
            return {"assessable": False,
                    "blockers": ["run predates the GHGP Land Sector & Removals dimension — "
                                 "recompute to produce a removals statement"],
                    "warnings": []}
        return {"assessable": True, "blockers": [], "warnings": [], "has_removals": False}

    blockers, warnings = [], []

    # R2 — false zero: the dimension was evaluated but an as_of excluded every record.
    if run.total_removals_co2e is None:
        blockers.append(f"the removals as_of {run.removals_as_of} excluded every removal record "
                        "although the org holds some — recompute with a valid as_of; a false zero "
                        "cannot be filed")
        return {"assessable": True, "blockers": blockers, "warnings": warnings}

    lines = db.query(RunRemovalLine).filter(RunRemovalLine.run_id == run.id).all()
    org_credit_serials = {(c.registry, c.serial_number) for c in
                          db.query(CarbonCredit.registry, CarbonCredit.serial_number)
                          .filter(CarbonCredit.organisation_id == run.organisation_id).all()
                          if c.serial_number}

    for ln in lines:
        d = json.loads(ln.details or "{}")
        # R3 — permanence: a land-based removal with no monitoring OR no reversal
        # accounting is not reportable (LSRG Ch.7). Technological is a warning (lower bar).
        if ln.removal_category == "land_based" and ln.record_kind == "removal":
            missing = [k for k in ("monitoring_method", "reversal_accounting")
                       if not (d.get(k) or "").strip()]
            if missing:
                blockers.append(f"land-based removal (record {ln.removal_record_id}) is missing "
                                f"{missing} — a land-based removal without monitoring and reversal "
                                f"accounting is not reportable (GHGP LSRG Ch.7)")
        # R4 — double count: the removed carbon must not ALSO be sold as a credit.
        if d.get("attribute_retained") is False:
            blockers.append(f"removal record {ln.removal_record_id} has attribute_retained=false — "
                            f"the removed carbon was transferred/sold as a credit and cannot also "
                            f"be counted in the inventory (double claim)")
        reg, ser = d.get("credit_registry"), d.get("credit_serial_if_sold")
        if ser and (reg, ser) in org_credit_serials:
            blockers.append(f"removal record {ln.removal_record_id} carries a credit serial "
                            f"({reg} {ser}) that matches a carbon credit held by this org — the "
                            f"same tonne is claimed as both an inventory removal and a credit")

    # R5 — forgery: the live removals ledger was edited since the run froze it. The
    # fingerprint hashes attribute_retained + credit_serial too, so a POST-FILING SALE
    # (recording the removed tonne as also sold, to escape R4's frozen-detail reads)
    # moves the fingerprint and is caught here — recompute then trips R4.
    if run.removals_fingerprint:
        rem_as_of = run.removals_as_of
        included = [r for r in org_records
                    if rem_as_of is None or (r.as_of_date or "") <= rem_as_of]
        if _removals_fingerprint(included) != run.removals_fingerprint:
            blockers.append("the removals ledger changed since this run froze it — the run's "
                            "removals figure no longer matches; recompute")

    # R6 — flux is period-bound.
    if run.reporting_period_id is None:
        blockers.append("removals were evaluated but the run is not scoped to a reporting period — "
                        "a removal/sequestration flux is inherently period-bound")

    # Warnings (technological lower bar; missing durability metadata).
    for ln in lines:
        d = json.loads(ln.details or "{}")
        if ln.removal_category == "technological" and ln.record_kind == "removal":
            if not (d.get("monitoring_method") or "").strip():
                warnings.append(f"technological removal (record {ln.removal_record_id}) has no "
                                f"monitoring_method recorded")
            if d.get("quantification_method") != "metered":
                warnings.append(f"technological removal (record {ln.removal_record_id}) is not "
                                f"metered ({d.get('quantification_method')}) — a lower-rigour basis")
        if d.get("expected_durability_years") is None:
            warnings.append(f"removal record {ln.removal_record_id} has no expected_durability_years "
                            f"— durability tier is unstated")

    return {"assessable": True, "blockers": blockers, "warnings": sorted(set(warnings)),
            "has_removals": True,
            "gross_removals_kg": run.total_removals_co2e,
            "reversed_kg": run.removals_reversed_co2e or 0.0,
            "net_removals_kg": _net_removals_kg(run)}
