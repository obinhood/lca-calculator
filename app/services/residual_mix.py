"""GHG Protocol Scope 2 Guidance: the RESIDUAL MIX for uncovered market-based load.

THE DEFECT THIS CLOSES. Under the market-based method, consumption not covered by a
contractual instrument must be priced at the residual mix — the grid average with the
attributes other purchasers have already claimed removed. The engine previously priced
that remainder at the plain LOCATION-BASED grid average. Residual mix is always >= the
grid average (the clean attributes have been stripped out), so the old behaviour double
counted attributes someone else had claimed and UNDERSTATED the market-based figure —
the failure class this platform privileges above all others.

DOCTRINE
  * fail-open on the NUMBER: when no residual mix resolves, the grid-average arithmetic is
    kept EXACTLY as before. A rate is never invented, never interpolated, never broadened
    from a neighbouring market or year, and never max()-ed up to the grid rate.
  * fail-closed on the DISCLOSURE: the substitution stops being silent. It is frozen per
    (market, year) and the gate says so, quantified in kWh and kg.
  * the severity line is drawn on WHO OWNS THE MISSING FACT. Nobody can conjure a residual
    mix for a market where none is published, and publishers release year Y around mid-Y+1,
    so a filer closing FY2025 in Q1 2026 legitimately has none. Absence therefore WARNS.
    What BLOCKS is org-fixable or provably wrong.
"""
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import ResidualMixRate, RunResidualMixStatement, CalculationRun

RESIDUAL_MIX_VERSION = "s2rm-v1"

# A residual mix below the grid average is arithmetically impossible for a correct rate —
# it is proof of a wrong key, year, unit or row, and it errs in the understating direction.
# The tolerance absorbs honest basis differences (a CO2-only published rate against a CO2e
# location factor), not a real inversion.
RESIDUAL_INVERSION_TOLERANCE = 0.02
_EPS_KWH = 1e-9

# Sentinels. SQLite treats NULLs as DISTINCT in a unique index, so a nullable
# (market_key, year_key) would silently admit duplicate statement rows per run.
MARKET_UNKNOWN = "__unknown__"
YEAR_UNKNOWN = 0

STATEMENT_STATUSES = (
    "fully_contractual", "org_instrument", "reference_rate", "not_published",
    "unresolved_no_reference_data", "market_unknown", "year_unknown",
)
# Statuses that priced kWh at a residual rate (and therefore MUST carry one).
_PRICED_STATUSES = ("org_instrument", "reference_rate")


def market_key(value: Optional[str]) -> Optional[str]:
    """The market identity used for BOTH instrument matching and residual-mix lookup.

    Extracted verbatim from _InstrumentPool._applies, which already defines market
    equality as ``strip().upper()`` between MarketInstrument.market and the activity's
    geo. Sharing one function is the point: a residual mix must never resolve for a market
    the instrument matcher considers different.

    The key is OPAQUE — there is no hierarchy. 'DE-BW' never resolves 'DE' and 'US-NEWE'
    never resolves 'US'; broadening would be inventing a rate for a grid nobody published.
    """
    v = (value or "").strip().upper()
    return v or None


def resolve_reference_rate(db: Session, mkey: Optional[str], year: Optional[int],
                           run_gwp_set: Optional[str]) -> dict:
    """Resolve the published residual mix for (market, year) under the run's GWP vintage.

    TWO-PASS and deterministic: an exact GWP-vintage match always beats a vintage-less row,
    regardless of insertion order. A single query ordered by id would let a newer
    vintage-less row shadow an older exact-vintage one purely by when it was typed.

    Returns a snapshot dict; `status` is a STATEMENT_STATUSES value.
    """
    blank = {"status": "unresolved_no_reference_data", "rate": None,
             "reference_rate_id": None, "reference_rate_kg_per_kwh": None,
             "gwp_match": None, "gas_basis": None, "publisher": None,
             "publication": None, "gwp_vintage_mismatch": False}
    if not mkey or not year:
        return blank

    base = db.query(ResidualMixRate).filter(
        ResidualMixRate.market == mkey, ResidualMixRate.year == year)

    row, gwp_match = None, None
    if run_gwp_set:
        row = base.filter(func.upper(ResidualMixRate.gwp_set) == run_gwp_set.upper())\
            .order_by(ResidualMixRate.id.desc()).first()
        if row is not None:
            gwp_match = "matched"
    if row is None:
        # The source states no vintage (AIB publishes a CO2-focused mix with none).
        row = base.filter(ResidualMixRate.gwp_set.is_(None))\
            .order_by(ResidualMixRate.id.desc()).first()
        if row is not None:
            gwp_match = "matched_gwp_unstated"
    if row is None:
        # Rows exist for this market/year but only under another GWP vintage. Applying one
        # would silently mix vintages, so it prices at grid and warns instead.
        return {**blank, "gwp_vintage_mismatch": base.first() is not None}

    if row.status == "not_published":
        # A first-class ATTESTED absence — a fact an assurer needs, not an empty result.
        return {"status": "not_published", "rate": None,
                "reference_rate_id": row.id, "reference_rate_kg_per_kwh": None,
                "gwp_match": gwp_match, "gas_basis": row.gas_basis,
                "publisher": row.publisher, "publication": row.publication,
                "gwp_vintage_mismatch": False}
    return {"status": "reference_rate", "rate": row.kg_co2e_per_kwh,
            "reference_rate_id": row.id,
            "reference_rate_kg_per_kwh": row.kg_co2e_per_kwh,
            "gwp_match": gwp_match, "gas_basis": row.gas_basis,
            "publisher": row.publisher, "publication": row.publication,
            "gwp_vintage_mismatch": False}


def _pct(part: float, whole: float) -> Optional[float]:
    return round(100.0 * part / whole, 2) if whole > 0 else None


def scope2_residual_mix_completeness(db: Session, run) -> dict:
    """Blockers + warnings for one run's Scope 2 residual-mix treatment.

    Reads ONLY frozen state (RunResidualMixStatement), except RM-B5, which deliberately
    re-reads the reference table to detect an in-place edit of append-only data.
    """
    version = getattr(run, "scope2_residual_mix_version", None)
    rows = db.query(RunResidualMixStatement).filter(
        RunResidualMixStatement.run_id == run.id).order_by(
        RunResidualMixStatement.id).all()

    # LEGACY BRANCH — first and short-circuiting. Every rule below is structurally
    # unreachable for a run frozen before the requirement existed. scope2_residual_mix_
    # completeness is re-evaluated at RENDER time on already-filed runs, so without this
    # the change would retroactively block history — the cliff a previous phase rejected.
    if version is None:
        return {
            "assessable": True, "legacy": True, "blockers": [], "warnings": [
                "this run predates the Scope 2 residual-mix requirement: uncovered "
                "market-based load was priced at the LOCATION grid average, which double "
                "counts attributes other purchasers claimed and UNDERSTATES the "
                "market-based figure — recompute to price it at the residual mix"],
            "statements": [],
        }

    blockers, warnings = [], []
    total_kwh = sum((r.kwh_contractual + r.kwh_priced_at_residual + r.kwh_priced_at_grid)
                    for r in rows)
    gap_total = sum(r.gap_consolidated_co2e_kg for r in rows)

    # Which markets carry a rank-0 CONTRACTUAL claim — an org taking credit for attributes
    # in a market. An org-supplied residual_mix instrument is NOT an attribute claim and
    # must not arm RM-B2.
    claimed_markets = {(r.market_key, r.year_key) for r in rows
                       if r.kwh_contractual > _EPS_KWH}

    for r in rows:
        m, y = r.market_key, r.year_key
        where = f"market {m} / {y}"
        if (r.kwh_priced_at_grid > _EPS_KWH
                and (m == MARKET_UNKNOWN or y == YEAR_UNKNOWN)):
            # RM-B1 — org-fixable: the platform cannot look up a market it was not told.
            blockers.append(
                f"Scope 2: {r.kwh_priced_at_grid:.0f} kWh of uncovered electricity has no "
                f"{'market (set activities.geo)' if m == MARKET_UNKNOWN else 'parseable date'} "
                f"— the residual mix cannot be resolved, so it was priced at the grid "
                f"average and the market-based figure is UNDERSTATED")
        if (r.kwh_priced_at_grid > _EPS_KWH and m != MARKET_UNKNOWN
                and y != YEAR_UNKNOWN and (m, y) in claimed_markets):
            # RM-B2 — the org claims attributes in this market for part of its load while
            # pricing the rest as if the grid still held average attributes. That is the
            # double count, and here it is the org's own claim that creates it.
            blockers.append(
                f"Scope 2 ({where}): you claim contractual instruments in this market but "
                f"{r.kwh_priced_at_grid:.0f} kWh of uncovered load was priced at the GRID "
                f"AVERAGE because no residual mix is on file — that double counts the "
                f"attributes others claimed. Load the published residual mix, record an "
                f"attested not_published row, or supply your supplier's residual rate")
        if (r.rate_kg_co2e_per_kwh is not None and r.grid_rate_avg_kg_per_kwh
                and r.gwp_match != "unverified"
                and r.rate_kg_co2e_per_kwh
                < r.grid_rate_avg_kg_per_kwh * (1 - RESIDUAL_INVERSION_TOLERANCE)):
            # RM-B3 — arithmetically impossible for a correct residual mix. The rate is
            # still applied AS GIVEN; max()-ing it up to the grid rate would invent a number.
            blockers.append(
                f"Scope 2 ({where}): the applied residual mix "
                f"{r.rate_kg_co2e_per_kwh:.5f} is BELOW the grid average "
                f"{r.grid_rate_avg_kg_per_kwh:.5f} kgCO2e/kWh — impossible for a residual "
                f"mix, which has other purchasers' clean attributes removed. Check the "
                f"market key, year, unit and source "
                f"({'org instrument' if r.status == 'org_instrument' else 'reference table'})")
        _org_rate = r.org_rate_kg_co2e_per_kwh
        if (r.instrument_id is not None and r.reference_rate_kg_co2e_per_kwh is not None
                and _org_rate is not None
                and _org_rate
                < r.reference_rate_kg_co2e_per_kwh * (1 - RESIDUAL_INVERSION_TOLERANCE)):
            # RM-B4 — frozen-vs-frozen: an org rate that undercuts the published one.
            blockers.append(
                f"Scope 2 ({where}): your own residual rate {_org_rate:.5f} is "
                f"BELOW the published residual mix "
                f"{r.reference_rate_kg_co2e_per_kwh:.5f} kgCO2e/kWh — substantiate the "
                f"lower rate or price at the published mix")
        # Scoped to buckets whose UNCOVERED load actually depended on this row — either
        # priced against it, or relying on its attested absence. A fully-contractual
        # bucket had no uncovered load at all, so an edit to a row it never consulted must
        # not retroactively block a filed run over admin-owned data the org cannot fix.
        if (r.reference_rate_id is not None
                and (r.kwh_priced_at_residual > _EPS_KWH or r.kwh_priced_at_grid > _EPS_KWH)):
            live = db.get(ResidualMixRate, r.reference_rate_id)
            if live is None or live.kg_co2e_per_kwh != r.reference_rate_kg_co2e_per_kwh:
                # RM-B5 — residual_mix_rates is append-only BY CONTRACT. An in-place edit
                # means a filed figure no longer reproduces from the series as entered.
                blockers.append(
                    f"Scope 2 ({where}): the residual-mix row this run priced against "
                    f"(id {r.reference_rate_id}) has been {'DELETED' if live is None else 'EDITED IN PLACE'} "
                    f"— that table is append-only; corrections must be INSERTed so the "
                    f"filed figure still reproduces")

        if r.unpriceable_lines:
            warnings.append(
                f"Scope 2 ({where}): {r.unpriceable_lines} electricity line(s) could not be "
                f"converted to kWh, so their market-based figure fell back to the location "
                f"factor and no residual mix could be applied — fix the unit")

        # --- warnings ---
        if (r.kwh_priced_at_grid > _EPS_KWH and (m, y) not in claimed_markets
                and m != MARKET_UNKNOWN and y != YEAR_UNKNOWN):
            warnings.append(
                f"Scope 2 ({where}): no residual mix "
                f"{'is published' if r.publisher else 'is on file'} — "
                f"{r.kwh_priced_at_grid:.0f} kWh "
                f"({_pct(r.kwh_priced_at_grid, total_kwh)}% of electricity) priced at the "
                f"grid average. The market-based figure is UNDERSTATED by an amount that "
                f"cannot be quantified without a published rate: the residual mix is "
                f"always >= the grid average, so the shortfall is >= 0 with no computable "
                f"upper bound. Load the rate to price and size it")
        if r.gwp_vintage_mismatch:
            warnings.append(
                f"Scope 2 ({where}): a residual mix IS on file for this market and year "
                f"but only under a different GWP vintage than this run's {run.gwp_set} — "
                f"it was NOT applied (mixing vintages would be silent), so the load "
                f"priced at the grid average. Load the rate for {run.gwp_set}")
        if r.gwp_match == "matched_gwp_unstated":
            warnings.append(
                f"Scope 2 ({where}): the residual mix states no GWP vintage, so it could "
                f"not be verified against this run's {run.gwp_set}")
        if r.gas_basis == "co2":
            warnings.append(
                f"Scope 2 ({where}): the residual mix is CO2-only while the location "
                f"factor is CO2e — the platform will NOT gross it up; the market figure "
                f"omits the non-CO2 share of the uncovered load")
        if r.status == "org_instrument" and r.instrument_id is not None:
            from ..models import MarketInstrument
            inst = db.get(MarketInstrument, r.instrument_id)
            if inst is not None and not (inst.rate_source or "").strip():
                warnings.append(
                    f"Scope 2 ({where}): your residual rate carries no rate_source — "
                    f"record where it came from (supplier letter, national publication)")

    return {
        "assessable": True,
        "legacy": False,
        "version": version,
        "blockers": blockers,
        "warnings": warnings,
        "kwh_electricity_total": round(total_kwh, 6),
        "understatement_remaining_consolidated_kg": round(gap_total, 6),
        "statements": [{
            "market": None if r.market_key == MARKET_UNKNOWN else r.market_key,
            "year": None if r.year_key == YEAR_UNKNOWN else r.year_key,
            "status": r.status,
            "rate_kg_co2e_per_kwh": r.rate_kg_co2e_per_kwh,
            "reference_rate_kg_co2e_per_kwh": r.reference_rate_kg_co2e_per_kwh,
            "publisher": r.publisher,
            "gwp_match": r.gwp_match,
            "gas_basis": r.gas_basis,
            "kwh_contractual": round(r.kwh_contractual, 6),
            "kwh_priced_at_residual": round(r.kwh_priced_at_residual, 6),
            "kwh_priced_at_grid": round(r.kwh_priced_at_grid, 6),
            "grid_rate_avg_kg_per_kwh": r.grid_rate_avg_kg_per_kwh,
            # Understatement still carried, on the CONSOLIDATED basis the disclosed
            # market total uses — an unweighted gap beside a weighted total is the
            # like-for-like error that bit the Cat 11 arithmetic check.
            "understatement_consolidated_kg": round(r.gap_consolidated_co2e_kg, 6),
        } for r in rows],
    }


def residual_mix_comparable(db: Session, base_run, run) -> Optional[str]:
    """Why two runs' MARKET-based totals cannot be compared, or None if they can.

    A year-on-year 'reduction' spanning a residual-mix methodology change is an artefact,
    not abatement — the same trap the GWP-vintage guard closes for GRI 305-5.

    It must fire on EVIDENCE, not on the version stamp. Nothing is back-filled, so every
    pre-existing base run carries a NULL stamp: keying on the stamp alone would block the
    305-5 disclosure of every organisation on the platform — including orgs with no
    electricity at all, and the day-one case where no rate exists so nothing moved. So the
    test is whether either run ACTUALLY priced load at a residual, or whether a shared
    (market, year) was priced differently between them. That also catches the case the
    stamp cannot see: two runs on the SAME version where the reference table gained a rate
    in between.
    """
    def _priced(r):
        return {(x.market_key, x.year_key): x.rate_kg_co2e_per_kwh
                for x in db.query(RunResidualMixStatement).filter(
                    RunResidualMixStatement.run_id == r.id).all()
                if x.kwh_priced_at_residual > _EPS_KWH}

    a, b = _priced(base_run), _priced(run)
    if not a and not b:
        return None                     # neither run priced anything at a residual
    moved = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    if not moved:
        return None                     # same markets, same rates — comparable
    return (f"market-based totals are not comparable: uncovered Scope 2 load was priced "
            f"differently between the base run and this one for {sorted(moved)!r} "
            f"(residual-mix policy {getattr(base_run, 'scope2_residual_mix_version', None) or 'none'} "
            f"vs {getattr(run, 'scope2_residual_mix_version', None) or 'none'}) — the "
            f"difference includes a methodology change, not only abatement. Recompute the "
            f"base run to compare like with like")
