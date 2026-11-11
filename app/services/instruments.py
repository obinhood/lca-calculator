"""Identity of one market-instrument submission.

Lives in its own module because two places need the same answer and must not drift
apart: the write path, which refuses a replay, and the migration that backfilled rows
written before the column existed.

THE NORMALISATION HAS TO MATCH THE ALLOCATOR. `_InstrumentPool._applies` compares
markets as `.strip().upper()`, so "GB" and "gb" are the SAME market when coverage is
allocated. If identity here were case-sensitive, those two would be different
certificates on the way in and one certificate on the way out — two rows each declaring
the full volume against the same load, which is exactly the double-count this is for.
"""

import hashlib
import json

#: Bumped if the identifying field set changes. Rows carry the hash their version
#: produced, so a bump must be accompanied by a backfill, not a silent re-interpretation.
IDENTITY_VERSION = "mi-v1"


def market_key(market):
    """The market as the allocator compares it. None stays None — an undeclared market
    is not the empty string, and the two must not collapse into one identity."""
    if market is None:
        return None
    key = market.strip().upper()
    return key or None


def submission_hash(organisation_id: int, instrument_type: str, kg_co2e_per_kwh: float,
                    coverage_kwh, gwp_set, market, start_date, end_date) -> str:
    """Stable identity of one submission, over the NORMALISED identifying fields.

    `description` is deliberately excluded. It is prose attached to the certificate, not
    part of its substance, and including it would let a retry with a corrected typo
    create a second instrument and double the declared coverage. The safe direction of
    error is refusing a genuine second certificate, not accepting a duplicate.
    """
    blob = json.dumps(
        [IDENTITY_VERSION, organisation_id, (instrument_type or "").strip().lower(),
         repr(float(kg_co2e_per_kwh)),
         None if coverage_kwh is None else repr(float(coverage_kwh)),
         (gwp_set or "").strip().upper() or None,
         market_key(market),
         (start_date or None), (end_date or None)],
        separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def find_duplicate(db, model, organisation_id: int, *, instrument_type, kg_co2e_per_kwh,
                   coverage_kwh, gwp_set, market, start_date, end_date):
    """An existing instrument identical in SUBSTANCE, or None.

    Matched on the fields rather than on the stored hash, because a row written before
    the hash column existed carries NULL and would otherwise be invisible — the fix
    would protect only future rows while every instrument already in the database stayed
    freely duplicable. The hash and its unique index remain, for the race a query cannot
    close; this is the part that covers history.
    """
    from sqlalchemy import func

    key = market_key(market)
    q = db.query(model).filter(
        model.organisation_id == organisation_id,
        model.instrument_type == instrument_type,
        model.kg_co2e_per_kwh == kg_co2e_per_kwh,
        model.gwp_set == gwp_set,
        model.start_date.is_(None) if start_date is None else model.start_date == start_date,
        model.end_date.is_(None) if end_date is None else model.end_date == end_date,
    )
    # NULL coverage (unbounded) is its own value, not a wildcard, and NULL != NULL in SQL.
    q = q.filter(model.coverage_kwh.is_(None) if coverage_kwh is None
                 else model.coverage_kwh == coverage_kwh)
    q = q.filter(model.market.is_(None) if key is None
                 else func.upper(func.trim(model.market)) == key)
    return q.first()


def over_declaration_gate(db, run) -> dict:
    """Blockers + warnings for one run's DECLARED contractual volume.

    The shared rule, with every renderer that publishes a market-based figure as a
    caller. It existed with one caller — the assurance readiness checklist — which meant
    a run declaring twice the certificate volume it consumed, and publishing a
    market-based Scope 2 of zero on the strength of it, sailed through all six
    disclosure renderers untouched. The defect was reachable by simply not asking for
    assurance.

    LEGACY RUNS ARE WARNED, NOT BLOCKED. A run frozen before the comparison existed
    carries no statement, and re-evaluating it at render time would retroactively
    invalidate a filing that was correct when made — the cliff
    `scope2_residual_mix_completeness` deliberately refuses, and this follows it.

    The ASSURANCE readiness check is stricter on purpose: there an unevaluated run does
    not pass, because that verdict is forward-looking ("can this be assured") and a
    readiness gate that passes over an absent figure is the failure mode it exists to
    close. A disclosure gate answers a different question about an already-filed run.
    """
    import json

    raw = getattr(run, "scope2_coverage_statement", None)
    if not raw:
        return {"assessable": False, "legacy": True, "blockers": [], "statement": None,
                "warnings": ["this run predates the Scope 2 declared-volume comparison: "
                             "whether its contractual instruments declared more "
                             "certificate volume than the load they covered was never "
                             "evaluated — recompute to obtain the verdict"]}
    try:
        cov = json.loads(raw)
    except (TypeError, ValueError):
        return {"assessable": False, "legacy": False, "blockers": [], "statement": None,
                "warnings": ["the frozen Scope 2 declared-volume statement could not be "
                             "read; treat the market-based figure as unverified"]}

    blockers, warnings = [], []
    if cov.get("over_declared") is True:
        excess = cov.get("over_declared_kwh")
        blockers.append(
            f"market-based Scope 2 OVER-DECLARED: contractual instruments declare "
            f"{cov.get('declared_contractual_kwh')} kWh against "
            f"{cov.get('load_kwh')} kWh of electricity load"
            + (f" (excess {excess} kWh)" if excess else "")
            + " — more certificate volume is claimed than was consumed, so the "
              "market-based figure overstates the renewable claim. "
            + str(cov.get("reason", "")))
    elif cov.get("determinable") is False:
        # Not "no". An unbounded contractual instrument claims the whole inventory with
        # no volume behind it, which is the shape of the defect, so it cannot publish.
        blockers.append(
            "market-based Scope 2 cannot be verified: contractual instrument(s) "
            f"{cov.get('unbounded_contractual_instruments')} declare no coverage_kwh, "
            "so the volume claimed over this inventory is unbounded and cannot be "
            "compared with the load consumed")
    return {"assessable": True, "legacy": False, "blockers": blockers,
            "warnings": warnings, "statement": cov}
