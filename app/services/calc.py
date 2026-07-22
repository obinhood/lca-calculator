import json
import math
import hashlib
from datetime import date as date_cls, datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from ..models import (
    ActivityRecord, EmissionFactor, CalculationRun, EmissionLineItem, ReportingPeriod,
    MarketInstrument, Scope3CategoryDeclaration, RunScope3Declaration,
    Organisation, ReportingEntity, RunEntityBoundary, RunResidualMixStatement,
)
from .ghgp import (
    GHGP_STANDARD_VERSION, CATEGORY_MAP_VERSION, CATEGORIES, taxonomy,
    derive_ghgp_category, boundary_verdict, declarations_fingerprint,
    BOUNDARY_POLICY_VERSION, TEMPORAL_BASIS_VERSION,
)
from .boundary import (
    BOUNDARY_VERSION, entity_weight, group_class, consolidation_fingerprint,
)
from .residual_mix import (
    RESIDUAL_MIX_VERSION, MARKET_UNKNOWN, YEAR_UNKNOWN, market_key,
    resolve_reference_rate,
)
from .units import convert, UnitConversionError, QuantityError
from .gwp import co2e_from_gases, gwp
from .dq import line_dq
from .spend import normalize_spend, SpendNormalizationError

# GHG Protocol scope by activity category. Purchased energy carriers (electricity,
# heat, steam, cooling) are Scope 2; on-site fuel combustion and fugitive/process
# emissions are Scope 1; value-chain items are Scope 3. An UNRECOGNISED category is
# not silently assumed — it defaults to Scope 3 but is FLAGGED (scope_source), so
# purchased steam (Scope 2) or a refrigerant leak (Scope 1) can't hide in Scope 3.
SCOPE_RULES = {
    # Scope 2 — purchased/network-supplied energy
    "electricity": "2", "heat": "2", "steam": "2", "cooling": "2",
    "district_heat": "2", "district_heating": "2", "purchased_heat": "2",
    # Scope 1 — direct combustion + fugitive/process
    "gas": "1", "natural_gas": "1", "diesel": "1", "petrol": "1", "gasoline": "1",
    "lpg": "1", "fuel_oil": "1", "oil": "1", "coal": "1",
    "refrigerant": "1", "fugitive": "1", "process": "1",
    # Scope 3 — value chain
    "flight": "3", "train": "3", "car": "3", "waste": "3", "spend": "3",
    "business_travel": "3", "commuting": "3", "freight": "3", "water": "3",
}

class ReportingPeriodError(ValueError):
    """Invalid reporting period for a calculation (wrong org, frozen, or missing)."""


class FactorValueError(ValueError):
    """The emission factor's OWN value / per-gas mass is missing or non-finite.

    The factor — not the activity quantity — is the bad input, so the row is
    routed to ``data_errors`` (surfaced, excluded) rather than crashing the whole
    run (a NULL value would raise) or letting inf/NaN silently poison the total
    (audit Phase 0). The sanctioned loaders already reject such factors on
    ingest; this guards factors inserted by other paths.
    """


# GHG Protocol Scope 2 Guidance instrument hierarchy (lower rank = higher precedence).
_INSTRUMENT_RANK = {"supplier_specific": 0, "ppa": 0, "rec": 0, "residual_mix": 1}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_date(value: Optional[str]) -> Optional[date_cls]:
    """Strict ISO-8601 date parse; None for missing/malformed (never guess).

    String comparison of dates is only valid for zero-padded YYYY-MM-DD, which
    nothing upstream guarantees — so all range checks parse first.
    """
    if not value:
        return None
    try:
        return date_cls.fromisoformat(str(value).strip())
    except ValueError:
        return None


# Bumped whenever the fingerprint's INPUTS change, so a run stamped under an older
# scheme is reported as "staleness not assessable" rather than falsely STALE.
FINGERPRINT_VERSION = "v5"   # v5: coverage_start/end — a declared consumption window
                             # changes how much of the record falls in the period.
                             # v4: entity_id — re-attributing an activity to a
                             # different entity changes its SHARE and thus the RESULT.


def activities_fingerprint(acts: List[ActivityRecord]) -> str:
    """Stable hash of the activity set (id/factor/quantity/unit/date/category/ghgp_category).

    Changes if any activity is added, removed, re-mapped, or edited — even when the
    activity count is unchanged — so a run computed against this set can be detected
    as stale by content, not just by count. date and category are included because
    both change the RESULT (period attribution and scope classification); ghgp_category
    because it changes the DISCLOSURE (v3).
    """
    parts = sorted(
        f"{a.id}:{a.factor_id}:{a.quantity}:{a.unit}:{a.date}:{a.category}:{a.ghgp_category}:"
        f"{a.entity_id}:{a.coverage_start}:{a.coverage_end}" for a in acts)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_VERSION}:{digest}"


def activities_in_scope(db: Session, organisation_id: int,
                        period: Optional[ReportingPeriod]) -> List[ActivityRecord]:
    """The activity set a run covers: the org's activities, filtered to the reporting
    period when one is set.

    Rows with a missing/malformed date are KEPT (they become data_errors — they must
    not silently vanish from the run). This is the SINGLE definition of a run's
    activity set: compute_co2e and the staleness check both use it, so a
    period-scoped run can no longer be perpetually 'stale' by comparing a filtered
    fingerprint against the org's unfiltered activity list.
    """
    # Ordered by id so a run is fully DETERMINISTIC — market-instrument allocation
    # consumes a shared instrument pool cumulatively, so an unspecified row order
    # could otherwise change which consumption a REC covers and thus the market total.
    acts = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == organisation_id)\
        .order_by(ActivityRecord.id).all()
    if period is None:
        return acts
    p_start = _parse_iso_date(period.start_date)
    p_end = _parse_iso_date(period.end_date)
    in_period = []
    for a in acts:
        cs = _parse_iso_date(getattr(a, "coverage_start", None))
        ce = _parse_iso_date(getattr(a, "coverage_end", None))
        if cs is not None and ce is not None and ce >= cs:
            # A declared WINDOW decides membership: a December-to-January invoice belongs
            # to both fiscal years, prorated, rather than wholly to whichever one its
            # single `date` lands in.
            if (p_end and cs > p_end) or (p_start and ce < p_start):
                continue
            in_period.append(a)
            continue
        adate = _parse_iso_date(a.date)
        if adate is None:
            in_period.append(a)      # undatable: kept, becomes a data_error
            continue
        if p_start and adate < p_start:
            continue
        if p_end and adate > p_end:
            continue
        in_period.append(a)
    return in_period


def coverage_overlap(a, p_start, p_end):
    """(fraction, evidence) for a declared consumption window against the reporting period.

    Returns ``(1.0, None)`` whenever there is no usable window or the run is not
    period-scoped — byte-identical behaviour to before, which is what keeps every existing
    activity and every filed run unchanged.

    The basis is INCLUSIVE CALENDAR DAYS, frozen onto the line. It is deliberately not
    inferred: a record with no declared window is attributed wholly by its `date`, exactly
    as it always was, because the platform cannot know a window it was not told.
    """
    cs = _parse_iso_date(getattr(a, "coverage_start", None))
    ce = _parse_iso_date(getattr(a, "coverage_end", None))
    # Bail only when the WINDOW itself is unusable — never because a PERIOD bound is open.
    # A reporting period may legitimately have one open bound, and activities_in_scope
    # admits a record on the bound that exists; conflating the two facts here counted the
    # record whole in the open-bounded period AND prorated in its neighbour.
    if cs is None or ce is None or ce < cs:
        return 1.0, None
    total_days = (ce - cs).days + 1
    o_start = max(cs, p_start) if p_start else cs
    o_end = min(ce, p_end) if p_end else ce
    overlap_days = max(0, (o_end - o_start).days + 1)
    frac = overlap_days / total_days
    if frac >= 1.0:
        return 1.0, None            # wholly inside the period: nothing to prorate
    return frac, {
        "coverage_start": cs.isoformat(), "coverage_end": ce.isoformat(),
        "coverage_days": total_days, "days_in_period": overlap_days,
        "overlap_start": o_start.isoformat(), "overlap_end": o_end.isoformat(),
        "proration_fraction": frac, "proration_basis": "inclusive_calendar_days",
    }


def factor_gases(factor: EmissionFactor) -> dict:
    """Per-gas masses (kg gas per activity unit) for a factor with a gas breakdown.

    CH4 is routed to the correct GWP variant by the factor's ``ch4_origin``:
    fossil combustion vs biogenic (landfill/organic); NULL uses the blended value.
    """
    gases = {}
    if factor.kg_co2 is not None:
        gases["CO2"] = factor.kg_co2
    if factor.kg_ch4 is not None:
        origin = getattr(factor, "ch4_origin", None)
        key = {"fossil": "CH4_fossil", "biogenic": "CH4_biogenic"}.get(origin, "CH4")
        gases[key] = factor.kg_ch4
    if factor.kg_n2o is not None:
        gases["N2O"] = factor.kg_n2o
    return gases


def compute_activity_co2e(quantity: Optional[float], unit: str, factor: EmissionFactor,
                          gwp_set: Optional[str] = None) -> float:
    """kg CO2e for one activity.

    The quantity is converted from the activity's unit into the factor's unit
    BEFORE multiplying. Incompatible units raise ``UnitConversionError`` and a
    None/non-finite/non-numeric quantity raises ``QuantityError`` (a subclass) —
    a wrong-by-orders-of-magnitude number is worse than a rejected row (Gap 1).

    If the factor carries a per-gas breakdown AND ``gwp_set`` is given, the GWP
    set is applied HERE, at calculation time (Gap 2: the AR5/AR6 switch changes
    the number). Otherwise the pre-aggregated ``factor.value`` is used.
    """
    if factor is None:
        raise ValueError("no emission factor supplied")
    qty_in_factor_unit = convert(quantity, unit, factor.unit)
    if gwp_set and getattr(factor, "has_gas_breakdown", False):
        gases = factor_gases(factor)
        for g, mass in gases.items():
            if mass is None or not math.isfinite(mass):
                raise FactorValueError(
                    f"factor {factor.id} has a missing/non-finite {g} mass ({mass!r})")
        return qty_in_factor_unit * co2e_from_gases(gases, gwp_set)
    if factor.value is None or not math.isfinite(factor.value):
        raise FactorValueError(
            f"factor {factor.id} has a missing/non-finite value ({factor.value!r})")
    return qty_in_factor_unit * factor.value


class _InstrumentPool:
    """Volume-matched market-instrument allocation for one run (Scope 2 Guidance Ch. 4).

    Instruments are consumed cumulatively across the run's electricity activities
    in hierarchy order; a contractual instrument covers at most its
    ``coverage_kwh`` and the remainder falls through to the next instrument or
    the grid-average (location) rate. Instruments whose GWP vintage differs from
    the run's requested set are never applied (mirrors the aggregate-factor
    vintage check). Dated instruments never match activities with missing or
    malformed dates (unknown date != carte blanche).
    """

    def __init__(self, instruments: List[MarketInstrument], run_gwp_set: str):
        self.instruments = sorted(
            instruments, key=lambda i: (_INSTRUMENT_RANK.get(i.instrument_type, 2), i.id))
        self.remaining = {i.id: i.coverage_kwh for i in self.instruments}  # None = unbounded
        self.run_gwp_set = run_gwp_set
        self.skipped_vintage = sorted({
            i.id for i in self.instruments
            if i.gwp_set and run_gwp_set and i.gwp_set != run_gwp_set})

    def _applies(self, inst: MarketInstrument, activity_date: Optional[str],
                 activity_market: Optional[str]) -> bool:
        if inst.gwp_set and self.run_gwp_set and inst.gwp_set != self.run_gwp_set:
            return False
        # Geography / market matching (Scope 2 Guidance Ch. 7 quality criteria): a
        # contractual instrument may only cover consumption on the SAME market. A
        # DECLARED market that differs from the consumption's grid excludes the
        # instrument — this is what stops a US REC covering German load. A NULL
        # market on either side can't be verified, so the instrument still applies
        # but the allocation is flagged market_unverified (see allocate()).
        if (inst.market and activity_market
                and inst.market.strip().upper() != activity_market.strip().upper()):
            return False
        start = _parse_iso_date(inst.start_date)
        end = _parse_iso_date(inst.end_date)
        if start or end:
            adate = _parse_iso_date(activity_date)
            if adate is None:
                return False        # dated instrument never covers an undated activity
            if start and adate < start:
                return False
            if end and adate > end:
                return False
        return True

    def allocate(self, kwh: float, activity_date: Optional[str],
                 activity_market: Optional[str], grid_rate_per_kwh: float,
                 residual: Optional[dict] = None,
                 line_co2e_gross: float = 0.0) -> dict:
        """Cover ``kwh`` from the pool; price whatever is left at the RESIDUAL MIX.

        Scope 2 Guidance: uncovered load takes the residual mix — the grid average with
        the attributes other purchasers already claimed removed — NOT the plain grid
        average, which double counts those attributes and understates the market figure.
        When no residual mix resolves, the previous grid-average arithmetic is kept
        verbatim (fail-open on the number) and the gate reports it (fail-closed on the
        disclosure). A rate is never invented and never raised to meet the grid rate.
        """
        allocations = []
        needed = kwh
        co2e = 0.0
        # Instruments excluded because their DECLARED market differs from this
        # consumption's grid — computed pool-wide (like skipped_vintage) so the audit
        # signal is complete and id-order-independent, not dependent on whether the
        # loop happened to reach the instrument before coverage was exhausted.
        skipped_market = sorted({
            inst.id for inst in self.instruments
            if inst.market and activity_market
            and inst.market.strip().upper() != activity_market.strip().upper()})
        market_unverified_kwh = 0.0  # kWh covered where the market could not be verified
        for inst in self.instruments:
            if needed <= 0:
                break
            if not self._applies(inst, activity_date, activity_market):
                continue
            rem = self.remaining[inst.id]
            take = needed if rem is None else min(rem, needed)
            if take <= 0:
                continue
            co2e += take * inst.kg_co2e_per_kwh
            if rem is not None:
                self.remaining[inst.id] = rem - take
            needed -= take
            unverified = not (inst.market and activity_market)
            if unverified:
                market_unverified_kwh += take
            allocations.append({
                "instrument_id": inst.id,
                "instrument_type": inst.instrument_type,
                "instrument_market": inst.market,
                "market_match": "unverified" if unverified else "matched",
                "kg_co2e_per_kwh": inst.kg_co2e_per_kwh,
                "kwh_covered": take,
                "undated_instrument": not (inst.start_date or inst.end_date),
            })
        # `covered` keeps its established meaning (everything the instrument pool took),
        # so the frozen `kwh_contractual` key is NOT re-scoped — summary.py and issb_s2.py
        # read it off already-filed runs, and re-scoping a frozen key rewrites history.
        covered = kwh - needed
        # An org-supplied residual_mix instrument is NOT a contractual attribute claim —
        # it is that org's own residual rate. Counting it in the contractual figure made
        # summary report 100% contractual coverage for an org holding ZERO contractual
        # instruments, contradicting this run's own frozen statement. `kwh_contractual`
        # keeps its established meaning (every pool leg) because filed runs read it; the
        # rank-0-only figure is a NEW key, so no history is re-scoped.
        rank0 = sum(x["kwh_covered"] for x in allocations
                    if x.get("instrument_type") != "residual_mix")
        kwh_residual = covered - rank0          # org residual legs

        rate = (residual or {}).get("rate")
        if needed > 0 and rate is not None:
            co2e += needed * rate
            kwh_residual += needed
            # Close the per-line ledger: sum(kwh_covered) + kwh_grid_fallback == kwh.
            allocations.append({
                "instrument_id": None,
                "instrument_type": "residual_mix",
                "source": "reference",
                "reference_rate_id": (residual or {}).get("reference_rate_id"),
                "kg_co2e_per_kwh": rate,
                "kwh_covered": needed,
                "market_match": "matched" if activity_market else "unverified",
            })
            needed = 0.0
        elif needed > 0:
            # EXACT PASSTHROUGH when the pool took nothing: kwh * (co2e_gross / kwh) is not
            # bit-identical to co2e_gross in float, and a zero-instrument org's market total
            # must not drift by a ULP purely because it now walks this path.
            co2e += (line_co2e_gross if (not allocations and needed == kwh)
                     else needed * grid_rate_per_kwh)
        basis = ("contractual_instrument" if rank0 >= kwh and allocations
                 else "residual_mix" if kwh_residual > 0 and rank0 <= 0
                 else "partial_contractual_residual_mix" if kwh_residual > 0
                 else "partial_contractual" if allocations
                 else "grid_average_fallback")
        return {"co2e": co2e, "kwh": kwh, "kwh_contractual": covered,
                "kwh_contractual_rank0": rank0,
                "kwh_residual_mix": kwh_residual,
                "kwh_grid_fallback": needed, "method_basis": basis,
                "activity_market": activity_market,
                "kwh_market_unverified": market_unverified_kwh,
                "instruments_skipped_market": skipped_market,
                "allocations": allocations,
                "instruments_skipped_gwp_vintage": self.skipped_vintage}


def _financed_fingerprint(positions) -> str:
    parts = sorted(
        f"{p.id}:{p.outstanding_amount}:{p.attribution_denominator}:"
        f"{p.investee_scope1_tco2e}:{p.investee_scope2_tco2e}:{p.investee_scope3_tco2e}:"
        f"{p.as_of_date}" for p in positions)
    return "fp-v1:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# GHG Protocol Land Sector and Removals Guidance — the removals dimension version.
LSRG_VERSION = "ghgp-lsrg-2022"


def _removals_fingerprint(records) -> str:
    # attribute_retained / credit_registry / credit_serial_if_sold are hashed so a
    # POST-FILING SALE of an already-filed removal (which R4 only reads from FROZEN
    # detail) moves the fingerprint and is caught by the forgery gate (R5).
    parts = sorted(
        f"{r.id}:{r.removal_category}:{r.method}:{r.scope}:{r.record_kind}:"
        f"{r.quantity_tco2e}:{r.entity_id}:{r.reverses_record_id}:{r.as_of_date}:"
        f"{r.attribute_retained}:{r.credit_registry}:{r.credit_serial_if_sold}"
        for r in records)
    return "rm-v1:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def financed_included_positions(positions, financed_as_of: Optional[str]):
    """The positions that actually feed a run's Cat 15 figure — those at/before the
    as_of cutoff (matching pcaf.portfolio_financed's `<= as_of`). This is the set the
    staleness fingerprint must cover: a position dated AFTER the cutoff is not in the
    filed figure, so adding/editing it must NOT mark the run stale."""
    if not financed_as_of:
        return list(positions)
    cutoff = _parse_iso_date(financed_as_of)
    if cutoff is None:
        return list(positions)
    out = []
    for p in positions:
        d = _parse_iso_date(p.as_of_date)
        if d is not None and d <= cutoff:
            out.append(p)
    return out


def compute_co2e(db: Session, organisation_id: int, gwp_set: str = "AR6",
                 reporting_period_id: Optional[int] = None,
                 include_financed: Optional[bool] = None,
                 financed_as_of: Optional[str] = None,
                 financed_include_scope3: bool = True,
                 include_removals: Optional[bool] = None,
                 removals_as_of: Optional[str] = None) -> CalculationRun:
    """Create a NEW immutable calculation run for one organisation and return it.

    Prior runs are never mutated or deleted (Gap 5), and the calculation is scoped
    to a single organisation's activities (Gap 6 / multi-tenancy). Every activity
    lands in exactly one bucket — mapped | unmapped | unit_errors | data_errors |
    gwp_mismatch — recorded on the run's frozen coverage snapshot; excluded
    activities are surfaced, never silently dropped (Gap 4).

    If ``reporting_period_id`` is supplied it must belong to this organisation and
    not be frozen; only activities whose (parsed) date falls inside the period are
    computed, and activities with missing/malformed dates become data_errors —
    they cannot be attributed to a period, so they must not silently vanish.
    """
    period = None
    if reporting_period_id is not None:
        period = db.get(ReportingPeriod, reporting_period_id)
        if period is None or period.organisation_id != organisation_id:
            raise ReportingPeriodError("reporting period not found for this organisation")
        if period.frozen:
            raise ReportingPeriodError("reporting period is frozen; cannot create a new run")

    # The run's activity set — the SAME definition the staleness check uses.
    acts = activities_in_scope(db, organisation_id, period)
    # In a period-scoped run an undatable row cannot be attributed to the period,
    # so it is kept but bucketed as a data_error below (never silently dropped).
    undatable = ({a.id for a in acts if _parse_iso_date(a.date) is None}
                 if period is not None else set())

    # Hoisted once per run (was an N+1 query inside the loop).
    instruments = db.query(MarketInstrument).filter(
        MarketInstrument.organisation_id == organisation_id).all()
    pool = _InstrumentPool(instruments, gwp_set)
    # Residual mix resolved ONCE per distinct (market, year) the run touches, then reused.
    _rm_cache: dict = {}
    _rm_rollup: dict = {}      # (market_key, year_key) -> frozen statement accumulator

    def _residual_for(mkey, yr):
        k = (mkey, yr)
        if k not in _rm_cache:
            _rm_cache[k] = resolve_reference_rate(db, mkey, yr, gwp_set)
        return _rm_cache[k]

    # --- GHG Protocol Ch.3 organisational boundary -----------------------------
    org = db.get(Organisation, organisation_id)
    approach = ((org.consolidation_approach if org else None) or "operational_control").strip()
    cons_reason = org.consolidation_approach_reason if org else None
    entities = db.query(ReportingEntity).filter(
        ReportingEntity.organisation_id == organisation_id)\
        .order_by(ReportingEntity.id).all()
    ent_by_id = {e.id: e for e in entities}       # loaded ONCE — no N+1 in the loop
    total_non_consolidated = 0.0
    per_entity = {}   # entity_key -> {"gross","consolidated","n","weight","basis","resolved"}

    run = CalculationRun(
        organisation_id=organisation_id,
        reporting_period_id=reporting_period_id,
        created_at=_utcnow_iso(),
        gwp_set=gwp_set,
        status="pending",
        total_activities=len(acts),
        mapped=0, unmapped=0, unit_errors=0, data_errors=0, gwp_mismatch=0,
        total_co2e=0.0,
        activities_fingerprint=activities_fingerprint(acts),
        boundary_version=BOUNDARY_VERSION,
        consolidation_approach=approach,
        consolidation_reason=cons_reason,
        consolidation_fingerprint=consolidation_fingerprint(approach, cons_reason, entities),
    )
    errors = []
    try:
        db.add(run)
        db.flush()  # assign run.id for the line-item FK

        line_items = []
        total = 0.0          # location-based total (headline)
        total_market = 0.0   # dual reporting: Scope 2 swapped to market basis
        total_biogenic = 0.0 # ISO 14067: separate pool, never netted into totals
        dq_weighted_sum = 0.0  # emissions-weighted data-quality accumulator
        _p_start = _parse_iso_date(period.start_date) if period else None
        _p_end = _parse_iso_date(period.end_date) if period else None
        for a in acts:
            if a.id in undatable:
                run.data_errors += 1
                errors.append({"activity_id": a.id,
                               "error": "missing/malformed date in a period-scoped run"})
                continue
            if not a.factor:
                run.unmapped += 1
                continue
            # Temporal straddle: a declared consumption window overlapping the period
            # boundary contributes only its overlapping share. `_qty` replaces a.quantity
            # everywhere below — the emissions figure, the biogenic pool and the Scope 2
            # kWh must all be on the SAME prorated basis or the line contradicts itself.
            _frac, _cov = coverage_overlap(a, _p_start, _p_end)
            # The EFFECTIVE date for period-sensitive pricing. When a share was prorated
            # into this period, that share is the consumption occurring inside the overlap,
            # so it must be priced with the overlap's own vintage — residual-mix year,
            # spend base year and contractual-instrument matching alike.
            _eff_date = _cov["overlap_start"] if _cov else a.date
            _qty = (a.quantity * _frac) if a.quantity is not None else None
            per_gas = a.factor.has_gas_breakdown
            # Aggregate factors bake a GWP vintage into `value`, so a vintage
            # mismatch is unresolvable at calc time. Per-gas factors are vintage-
            # free: GWP is applied below from the requested set, so no check needed.
            if (not per_gas and a.factor.gwp_set and gwp_set
                    and a.factor.gwp_set.upper() != gwp_set.upper()):
                run.gwp_mismatch += 1
                errors.append({"activity_id": a.id,
                               "error": f"factor GWP set {a.factor.gwp_set} != requested {gwp_set}"})
                continue
            if a.quantity is not None and a.quantity < 0:
                run.data_errors += 1
                errors.append({"activity_id": a.id, "error": "negative quantity"})
                continue

            _pdate = _parse_iso_date(_eff_date)
            spend_steps = None
            if (a.factor.method_type or "") == "spend_based":
                # Spend-based EEIO: normalize the amount to the factor's currency
                # and base year (inflation + base-year FX), fail-closed on missing
                # reference data, then apply the per-currency factor.
                try:
                    amt = float(_qty)
                    if not math.isfinite(amt):
                        raise ValueError("non-finite amount")
                except (TypeError, ValueError):
                    run.data_errors += 1
                    errors.append({"activity_id": a.id, "error": "non-numeric/non-finite spend amount"})
                    continue
                try:
                    spend_steps = normalize_spend(
                        db, amt, a.unit, _pdate.year if _pdate else None,
                        a.factor.unit, a.factor.base_year)
                except SpendNormalizationError as exc:
                    run.data_errors += 1
                    errors.append({"activity_id": a.id, "error": str(exc)})
                    continue
                if a.factor.value is None or not math.isfinite(a.factor.value):
                    run.data_errors += 1
                    errors.append({"activity_id": a.id,
                                   "error": f"factor {a.factor_id} has a missing/non-finite value"})
                    continue
                co2e = spend_steps["amount_in_factor_currency"] * a.factor.value
            else:
                try:
                    co2e = compute_activity_co2e(_qty, a.unit, a.factor, gwp_set=gwp_set)
                except QuantityError as exc:          # None / non-finite / non-numeric quantity
                    run.data_errors += 1
                    errors.append({"activity_id": a.id, "error": str(exc)})
                    continue
                except FactorValueError as exc:       # bad factor value / per-gas mass
                    run.data_errors += 1
                    errors.append({"activity_id": a.id, "error": str(exc)})
                    continue
                except UnitConversionError as exc:    # incompatible / ambiguous / malformed
                    run.unit_errors += 1
                    errors.append({"activity_id": a.id, "error": str(exc)})
                    continue

            # --- GHG Protocol Ch.3 organisational boundary ------------------------
            # entity_id NULL = the reporting org itself => share 1.0 (every pre-boundary
            # row), so this is a no-op until entities exist.
            _ent = ent_by_id.get(a.entity_id) if a.entity_id is not None else None
            if a.entity_id is not None and _ent is None:
                # Dangling or CROSS-TENANT entity_id (ent_by_id is org-scoped, so another
                # tenant's entity is never resolvable). Fail-OPEN on the number — the
                # emission is real — and fail-CLOSED on the disclosure via the gate.
                # Its own bucket: keying this to "self" would both mis-attribute the
                # emissions and mark the reporting entity itself unresolved.
                _w, _basis, _resolved = 1.0, "unresolved_entity_not_found", False
                _key = f"e:{a.entity_id}"
            else:
                _w, _basis, _resolved = entity_weight(approach, _ent)
                _key = "self" if _ent is None else f"e:{_ent.id}"
            co2e_gross = co2e
            # The WEIGHTED value is what gets stored AND summed, so
            # `sum(location line items) == run.total_co2e` — the invariant an assurer
            # walks — holds by construction. Weighting at the total instead would make
            # every line-summing consumer report gross while the run reported
            # consolidated. The gross -> share -> consolidated walk is not lost: it is
            # frozen in `details` and re-aggregated in run_entity_boundary.
            co2e = co2e_gross * _w
            total_non_consolidated += (co2e_gross - co2e)
            _b = per_entity.setdefault(_key, {
                "gross": 0.0, "consolidated": 0.0, "scope1": 0.0, "scope2": 0.0, "n": 0,
                "weight": _w, "basis": _basis, "resolved": _resolved, "entity": _ent})
            _b["gross"] += co2e_gross
            _b["consolidated"] += co2e
            _b["n"] += 1
            if not _resolved:
                _b["resolved"] = False

            dq = line_dq(a.factor, a, a.mapping_basis, _pdate.year if _pdate else None)
            detail = {
                "consolidation": {
                    "entity_key": _key,
                    "entity_id": a.entity_id,
                    "approach": approach,
                    "share_factor": _w,          # UNROUNDED — no rounding rule exists
                    "share_basis": _basis,
                    "resolved": _resolved,
                    "gross_co2e": co2e_gross,
                    "consolidated_co2e": co2e,
                },
                "factor_id": a.factor_id,
                "activity_unit": a.unit,
                "factor_unit": a.factor.unit,
                "quantity": _qty,
                "quantity_as_recorded": a.quantity,
                "temporal_proration": _cov,
                "calc_method": "per_gas" if per_gas else "aggregate",
                # GHG Protocol Scope 3 method hierarchy + LCA system boundary —
                # the lineage an assurer needs to check for double counting and
                # to compute the primary-data share.
                "method_type": a.factor.method_type or "average_data",
                "lca_boundary": a.factor.lca_boundary,
                # ecoinvent pedigree data-quality score + lognormal uncertainty.
                "data_quality": dq,
            }
            if spend_steps is not None:
                detail["spend_normalization"] = spend_steps
            if per_gas:
                gases = factor_gases(a.factor)
                detail["gwp_set_applied"] = gwp_set
                detail["gases_kg_per_unit"] = gases
                detail["gwp_values"] = {g: gwp(g, gwp_set) for g in gases}
            else:
                detail["gwp_set"] = a.factor.gwp_set
                detail["factor_value"] = a.factor.value

            # Biogenic CO2 tracked as its own pool (ISO 14067), never in total_co2e.
            # convert() cannot fail here — the same args already succeeded in
            # compute_activity_co2e above — so no guard is needed.
            if a.factor.kg_co2_biogenic is not None and math.isfinite(a.factor.kg_co2_biogenic):
                qty_fu = convert(_qty, a.unit, a.factor.unit)
                # Weighted too, or the ISO 14067 biogenic pool would sit on a different
                # (gross) basis from the consolidated total reported beside it.
                biogenic_gross = qty_fu * a.factor.kg_co2_biogenic
                biogenic = biogenic_gross * _w
                detail["biogenic_co2e_gross"] = biogenic_gross
                detail["biogenic_co2e"] = biogenic
                total_biogenic += biogenic

            # Scope classification: explicit preset > category rule > flagged default.
            # An unrecognised category defaults to Scope 3 but records scope_source
            # so the assumption is visible in the frozen lineage and the summary.
            _cat = (a.category or "").lower()
            if a.scope:
                scope, scope_source = a.scope, "explicit"
            elif _cat in SCOPE_RULES:
                scope, scope_source = SCOPE_RULES[_cat], "category_rule"
            else:
                scope, scope_source = "3", "assumed_scope3"
            a.scope = scope
            detail["scope_source"] = scope_source
            # IFRS S2 ¶29(a)(iv) per-entity Scope 1 / Scope 2 split. Uses the same
            # WEIGHTED (consolidated) co2e that feeds `total` — the location basis, to
            # match the headline `scope2_location_based`. Accumulated once per activity,
            # BEFORE the market-based line is built, so a Scope 2 activity is never
            # double-counted into scope2 here.
            if scope == "1":
                _b["scope1"] += co2e
            elif scope == "2":
                _b["scope2"] += co2e

            # --- GHGP Scope 3 category (frozen; never written back to the activity) ---
            ghgp_cat, ghgp_src, cands = derive_ghgp_category(scope, a.category, a.ghgp_category)
            # The activity's own category is frozen too, so the Scope 3 breakdown never
            # has to join back to the live ActivityRecord (reproduction contract).
            detail["activity_category"] = a.category
            detail["ghgp_category"] = ghgp_cat
            detail["ghgp_category_source"] = ghgp_src
            detail["ghgp_category_candidates"] = cands
            detail["ghgp_standard_version"] = GHGP_STANDARD_VERSION
            detail["ghgp_map_version"] = CATEGORY_MAP_VERSION
            if ghgp_cat is not None:
                t = taxonomy()[ghgp_cat]
                # Deliberate redundancy: the frozen evidence is human-readable without
                # the software, and a later taxonomy edit can be DETECTED (never
                # silently applied to a filed run).
                detail["ghgp_category_name"] = t["name"]
                detail["ghgp_min_boundary"] = t["min_boundary"]
                # Freeze the VERDICT, not the input: factor.lca_boundary is a live
                # catalog field, and correcting it later must not retroactively change
                # what a filed run claimed.
                _met, _basis, _token = boundary_verdict(ghgp_cat, a.factor.lca_boundary)
                detail["ghgp_min_boundary_met"] = _met
                # Which acceptance vocabulary produced that verdict, and the NORMALISED
                # token it was computed on. Freezing the INPUT beside the verdict is what
                # lets an assurer re-derive the verdict from the run's own record instead
                # of joining back to the live (and possibly since-corrected) factor row.
                detail["ghgp_boundary_policy_version"] = BOUNDARY_POLICY_VERSION
                detail["ghgp_boundary_token"] = _token
                detail["ghgp_boundary_verdict_basis"] = _basis
                detail["ghgp_sale_year_lifetime"] = t["sale_year_lifetime"]
            line_items.append(EmissionLineItem(
                run_id=run.id, activity_id=a.id, scope=scope, method="location", co2e=co2e,
                details=json.dumps(detail),
            ))
            total += co2e
            dq_weighted_sum += co2e * dq["overall"]
            run.mapped += 1

            # GHG Protocol dual Scope 2: every Scope 2 activity ALSO gets a
            # market-based line item. Market instruments are ELECTRICITY
            # contracts, so they only ever apply to electricity — other Scope 2
            # commodities (heat/steam) fall back to the location figure.
            if scope == "2":
                # THE TRAP: the instrument pool prices contractual kWh at the
                # instrument's UNWEIGHTED rate, so a pre-weighted grid_rate would mix
                # bases and yield a plausible-looking but wrong market line. Run the
                # pool ENTIRELY in gross terms, then weight the result once.
                # Policy: the pool is consumed in GROSS kWh — a REC covers physical
                # MWh, not your equity share of them.
                market_co2e_gross, market_detail = co2e_gross, dict(detail)
                # Biogenic belongs to the (single) location line only; don't let it
                # appear twice across the location+market lineage pair.
                market_detail.pop("biogenic_co2e", None)
                market_detail.pop("biogenic_co2e_gross", None)
                is_electricity = (a.category or "").lower() == "electricity"
                # NOT `and instruments`: an org holding ZERO contractual instruments is
                # exactly the population whose ENTIRE market figure was the location
                # figure, and the residual mix has to reach them or the fix is dead code
                # for the majority. With no instruments the pool covers nothing and the
                # exact-passthrough in allocate() keeps their number bit-identical.
                if is_electricity:
                    # Resolved BEFORE the conversion attempt: an electricity line whose
                    # unit cannot be converted still TOUCHED this market, and the statement
                    # artifact is complete-by-construction only if it says so.
                    _adate = _parse_iso_date(_eff_date)
                    _mk = market_key(a.geo)
                    _yr = _adate.year if _adate else None
                    _res = _residual_for(_mk, _yr)
                    _k = (_mk or MARKET_UNKNOWN, _yr or YEAR_UNKNOWN)
                    _b = _rm_rollup.setdefault(_k, {
                        "kwh_contractual": 0.0, "kwh_residual": 0.0, "kwh_grid": 0.0,
                        "grid_num": 0.0, "co2e_residual": 0.0, "co2e_grid": 0.0,
                        "gap": 0.0, "res": _res, "instrument_id": None,
                        "org_kwh": 0.0, "ref_kwh": 0.0, "unpriceable_lines": 0})
                    try:
                        kwh = convert(_qty, a.unit, "kWh")
                        grid_rate = (co2e_gross / kwh) if kwh else 0.0   # GROSS on GROSS
                        # a.geo is the consumption's grid/market — instruments only
                        # cover matching-market load (a US REC can't zero DE consumption).
                        alloc = pool.allocate(kwh, _eff_date, a.geo, grid_rate,
                                              residual=_res, line_co2e_gross=co2e_gross)
                        market_co2e_gross = alloc.pop("co2e")
                        # MERGE, don't replace: a wholesale replace discarded factor_id,
                        # method_type, data_quality, scope_source and every ghgp_* key
                        # from the market line's frozen lineage.
                        market_detail = {**detail, **alloc}
                        market_detail.pop("biogenic_co2e", None)
                        market_detail.pop("biogenic_co2e_gross", None)
                        # The grid rate is frozen for the FIRST time here: it was derived
                        # and thrown away before, which is exactly why the understatement
                        # a legacy run carries cannot be quantified after the fact.
                        market_detail.update({
                            "residual_mix_version": RESIDUAL_MIX_VERSION,
                            "residual_mix_market_key": _mk,
                            "residual_mix_year_key": _yr,
                            "residual_mix_status": _res["status"],
                            "residual_mix_rate_kg_per_kwh": _res.get("rate"),
                            "residual_mix_reference_rate_id": _res.get("reference_rate_id"),
                            "residual_mix_reference_rate_kg_per_kwh":
                                _res.get("reference_rate_kg_per_kwh"),
                            "residual_mix_gwp_match": _res.get("gwp_match"),
                            "grid_rate_kg_per_kwh": grid_rate,
                            "location_factor_geography": a.factor.geography,
                        })
                        # Every leg is bucketed EXACTLY once. Taking only the first org
                        # residual leg (and excluding all residual-typed legs from the
                        # contractual sum) dropped legs 2..n from every bucket: the frozen
                        # ledger stopped summing to consumption, grid_rate_avg inflated,
                        # and RM-B3 fired a FALSE inversion blocker on a correct run.
                        _line_org_kwh = 0.0
                        for _leg in alloc.get("allocations", []):
                            _lk = _leg.get("kwh_covered", 0.0)
                            if _leg.get("instrument_type") != "residual_mix":
                                _b["kwh_contractual"] += _lk
                            elif _leg.get("instrument_id") is not None:
                                _b["org_kwh"] += _lk
                                _line_org_kwh += _lk
                                _b["co2e_org"] = _b.get("co2e_org", 0.0) + _lk * _leg["kg_co2e_per_kwh"]
                                _b["co2e_residual"] += _lk * _leg["kg_co2e_per_kwh"]
                                _b["instrument_id"] = _leg["instrument_id"]
                                _ref = _res.get("reference_rate_kg_per_kwh")
                                if _ref is not None:
                                    # PROVABLE understatement only, and CONSOLIDATED: the
                                    # disclosed market total is share-weighted, so an
                                    # unweighted gap beside it is the like-for-like error.
                                    _b["gap"] += max(
                                        0.0, _ref - _leg["kg_co2e_per_kwh"]) * _lk * _w
                            else:
                                _b["ref_kwh"] += _lk
                                _b["co2e_residual"] += _lk * _leg["kg_co2e_per_kwh"]
                        _b["kwh_residual"] = _b["org_kwh"] + _b["ref_kwh"]
                        _b["kwh_grid"] += alloc.get("kwh_grid_fallback", 0.0)
                        # Weighted over the UNCOVERED remainder only. Averaging over all
                        # electricity (including contractually covered load on a dirtier
                        # factor) produced a "grid average" the residual mix was compared
                        # against but never drawn from — a false RM-B3 inversion on a
                        # correct run.
                        _unc = (alloc.get("kwh_residual_mix", 0.0) + _line_org_kwh
                                + alloc.get("kwh_grid_fallback", 0.0))
                        _b["grid_num"] += grid_rate * _unc
                        _b["grid_den"] = _b.get("grid_den", 0.0) + _unc
                        _b["co2e_grid"] += alloc.get("kwh_grid_fallback", 0.0) * grid_rate
                    except UnitConversionError as exc:
                        market_detail["method_basis"] = "grid_average_fallback"
                        market_detail["fallback_reason"] = str(exc)
                        _b["unpriceable_lines"] += 1
                else:
                    market_detail["method_basis"] = "grid_average_fallback"
                    if not is_electricity:
                        market_detail["fallback_reason"] = \
                            "non-electricity scope 2: electricity instruments not applicable"
                market_co2e = market_co2e_gross * _w
                market_detail["consolidation"] = {
                    **detail["consolidation"],
                    "gross_co2e": market_co2e_gross,
                    "consolidated_co2e": market_co2e,
                }
                line_items.append(EmissionLineItem(
                    run_id=run.id, activity_id=a.id, scope=scope, method="market",
                    co2e=market_co2e, details=json.dumps(market_detail),
                ))
                total_market += market_co2e
            else:
                total_market += co2e          # already weighted

        db.add_all(line_items)
        run.total_co2e = total
        run.total_co2e_market = total_market
        run.total_biogenic_co2e = total_biogenic
        # Emissions-weighted data-quality score (1 best .. 5 worst); 0.0 = no data.
        run.data_quality_score = round(dq_weighted_sum / total, 3) if total else 0.0
        run.notes = json.dumps(errors)
        run.total_co2e_non_consolidated = total_non_consolidated

        # --- Freeze the organisational boundary onto the run ---
        # One row per entity the org holds INCLUDING entities weighted 0.0 and entities
        # with no activities (those rows ARE the "other investees excluded" list the
        # disclosure clauses ask for), plus always exactly one 'self' row. Complete by
        # construction: an assurer sees the whole entity population, not an absence.
        frozen_at_b = _utcnow_iso()
        _rows = [("self", None)] + [(f"e:{e.id}", e) for e in entities]
        for key, e in _rows:
            agg = per_entity.get(key, {})
            if key in per_entity:
                w, basis, resolved = agg["weight"], agg["basis"], agg["resolved"]
            else:
                # Declared but contributed nothing to this run — still freeze its verdict.
                w, basis, resolved = entity_weight(approach, e)
            db.add(RunEntityBoundary(
                run_id=run.id, entity_key=key, entity_id=(e.id if e else None),
                entity_name=(e.name if e else (org.name if org else "reporting organisation")),
                entity_ref=(e.entity_ref if e else None),
                accounting_category=(e.accounting_category if e else "reporting_org"),
                equity_share_pct=(e.equity_share_pct if e else None),
                equity_share_basis=(e.equity_share_basis if e else None),
                financial_control=(e.financial_control if e else None),
                joint_financial_control=(e.joint_financial_control if e else None),
                operational_control=(e.operational_control if e else None),
                control_rationale=(e.control_rationale if e else None),
                in_consolidated_accounting_group=(
                    e.in_consolidated_accounting_group if e else True),
                effective_from=(e.effective_from if e else None),
                effective_to=(e.effective_to if e else None),
                approach=approach, share_factor=w, share_basis=basis, resolved=resolved,
                group_class=group_class(e),
                gross_co2e=agg.get("gross", 0.0),
                consolidated_co2e=agg.get("consolidated", 0.0),
                scope1_consolidated_co2e=agg.get("scope1", 0.0),
                scope2_consolidated_co2e=agg.get("scope2", 0.0),
                line_count=agg.get("n", 0),
                boundary_version=BOUNDARY_VERSION, frozen_at=frozen_at_b,
            ))
        # Activities pointing at an entity that does not exist for this org get their
        # own frozen row too — otherwise the run would carry emissions attributed to a
        # bucket the boundary statement never mentions.
        _covered = {k for k, _ in _rows}
        for key, agg in per_entity.items():
            if key in _covered:
                continue
            db.add(RunEntityBoundary(
                run_id=run.id, entity_key=key, entity_id=None,
                entity_name=f"unknown entity ({key})", accounting_category="unknown",
                approach=approach, share_factor=agg["weight"], share_basis=agg["basis"],
                resolved=False, group_class="unclassified",
                gross_co2e=agg["gross"], consolidated_co2e=agg["consolidated"],
                scope1_consolidated_co2e=agg.get("scope1", 0.0),
                scope2_consolidated_co2e=agg.get("scope2", 0.0),
                line_count=agg["n"],
                boundary_version=BOUNDARY_VERSION, frozen_at=frozen_at_b,
            ))

        # --- Freeze the Scope 2 residual-mix statement onto the run ---
        # ONE ROW PER (market, year) the run's electricity touched, INCLUDING markets fully
        # covered by contractual instruments (status 'fully_contractual', zeros). Complete
        # by construction, the same doctrine as RunEntityBoundary and RunScope3Declaration:
        # an assurer sees the whole market population rather than having to notice an absence.
        _rm_frozen_at = _utcnow_iso()
        for (_mk, _yk), _b in sorted(_rm_rollup.items()):
            _res = _b["res"]
            _kwh_res, _kwh_grid = _b["kwh_residual"], _b["kwh_grid"]
            _kwh_all = _b["kwh_contractual"] + _kwh_res + _kwh_grid
            _rate = (_b["co2e_residual"] / _kwh_res) if _kwh_res > 0 else None
            # Status names the bucket's DOMINANT outcome, but a bucket can be MIXED
            # (part priced at residual, part still at grid). The gate therefore keys its
            # grid-priced rules on kwh_priced_at_grid, never on status equality — keying
            # them on status made every rule unreachable for a market that had any org
            # residual leg at all, however much load still fell through to the grid.
            if _b.get("unpriceable_lines"):
                # An electricity line whose unit will not convert to kWh contributes no
                # quantity, so a zeroed bucket would otherwise read as a clean
                # 'fully_contractual' market — a FALSE completeness claim about load that
                # was in fact priced at the location grid average.
                _status, _rate = "unpriceable", None
            elif _kwh_res <= 0 and _kwh_grid <= 0:
                _status, _rate = "fully_contractual", None
            elif _kwh_grid > 0 and _kwh_res <= 0:
                # Nothing was priced at a residual: name WHY, so the right rule fires.
                _rate = None
                if _mk == MARKET_UNKNOWN:
                    _status = "market_unknown"
                elif _yk == YEAR_UNKNOWN:
                    _status = "year_unknown"
                elif _res["status"] == "not_published":
                    _status = "not_published"
                else:
                    _status = "unresolved_no_reference_data"
            elif _b["org_kwh"] >= _b["ref_kwh"]:
                _status = "org_instrument"
            else:
                _status = "reference_rate"
            db.add(RunResidualMixStatement(
                run_id=run.id, market_key=_mk, year_key=_yk, status=_status,
                rate_kg_co2e_per_kwh=_rate,
                reference_rate_id=_res.get("reference_rate_id"),
                reference_rate_kg_co2e_per_kwh=_res.get("reference_rate_kg_per_kwh"),
                instrument_id=_b["instrument_id"],
                gwp_match=_res.get("gwp_match"), gas_basis=_res.get("gas_basis"),
                publisher=_res.get("publisher"), publication=_res.get("publication"),
                kwh_contractual=_b["kwh_contractual"],
                kwh_priced_at_residual=_kwh_res, kwh_priced_at_grid=_kwh_grid,
                grid_rate_avg_kg_per_kwh=((_b["grid_num"] / _b["grid_den"])
                                          if _b.get("grid_den") else None),
                co2e_at_residual_kg=_b["co2e_residual"], co2e_at_grid_kg=_b["co2e_grid"],
                gap_consolidated_co2e_kg=_b["gap"],
                org_rate_kg_co2e_per_kwh=((_b.get("co2e_org", 0.0) / _b["org_kwh"])
                                          if _b["org_kwh"] > 0 else None),
                gwp_vintage_mismatch=bool(_res.get("gwp_vintage_mismatch")),
                unpriceable_lines=_b.get("unpriceable_lines", 0),
                residual_mix_version=RESIDUAL_MIX_VERSION, frozen_at=_rm_frozen_at,
            ))
        run.scope2_residual_mix_version = RESIDUAL_MIX_VERSION

        # --- Freeze the Scope 3 completeness statement onto the run ---
        # EXACTLY 15 rows, ALWAYS. A category the org never screened is frozen as
        # 'undeclared' — a first-class status, not an absent row — so the run's
        # exclusion statement is complete by construction: an assurer sees fifteen
        # statements rather than an absence they have to notice.
        live_decls = []
        if reporting_period_id is not None:
            live_decls = db.query(Scope3CategoryDeclaration).filter(
                Scope3CategoryDeclaration.organisation_id == organisation_id,
                Scope3CategoryDeclaration.reporting_period_id == reporting_period_id).all()
        by_cat = {d.category: d for d in live_decls}
        frozen_at = _utcnow_iso()
        for c in CATEGORIES:
            d = by_cat.get(c)
            db.add(RunScope3Declaration(
                run_id=run.id, category=c,
                status=d.status if d else "undeclared",
                declaration_id=d.id if d else None,
                justification=d.justification if d else None,
                screening_estimate_tco2e=d.screening_estimate_tco2e if d else None,
                screening_method=d.screening_method if d else None,
                materiality_threshold_pct=d.materiality_threshold_pct if d else None,
                criteria=d.criteria if d else None,
                minimum_boundary_met=d.minimum_boundary_met if d else None,
                temporal_basis=d.temporal_basis if d else None,
                basis_units_sold=d.basis_units_sold if d else None,
                basis_lifetime_years=d.basis_lifetime_years if d else None,
                basis_per_unit_annual_co2e_kg=(
                    d.basis_per_unit_annual_co2e_kg if d else None),
                method_description=d.method_description if d else None,
                calculation_tools=d.calculation_tools if d else None,
                primary_data_pct=d.primary_data_pct if d else None,
                gross_exposure_total=d.gross_exposure_total if d else None,
                gross_exposure_currency=d.gross_exposure_currency if d else None,
                screened_at=d.screened_at if d else None,
                ghgp_standard_version=GHGP_STANDARD_VERSION,
                frozen_at=frozen_at,
            ))
        run.ghgp_standard_version = GHGP_STANDARD_VERSION
        run.ghgp_map_version = CATEGORY_MAP_VERSION
        # Stated at run level too: a run with zero Scope 3 lines must still say which
        # acceptance vocabulary it was computed under.
        run.ghgp_boundary_policy_version = BOUNDARY_POLICY_VERSION
        # Stamps this run as computed UNDER the temporal-basis requirement. Runs frozen
        # before this keep NULL and are only warned, never blocked (the anti-cliff rule).
        run.scope3_temporal_basis_version = TEMPORAL_BASIS_VERSION
        # Detects the screen being EDITED after the run that filed it.
        run.scope3_declaration_fingerprint = declarations_fingerprint(live_decls)

        # --- Scope 3 Category 15 = PCAF financed emissions, frozen onto the run ---
        # Cat 15 IS gross Scope 3, so it must be an attribute of the immutable run
        # (the assurance unit), not of a render-time request. total_co2e and the
        # pedigree data-quality score are NEVER touched — positions are a live
        # ledger on a different (PCAF) data-quality scale; the DISCLOSED total in the
        # renderers is what adds financed emissions.
        from .pcaf import portfolio_financed
        from ..models import FinancedPosition, RunFinancedLine
        all_positions = db.query(FinancedPosition).filter(
            FinancedPosition.organisation_id == organisation_id).all()
        if include_financed is None:
            include_financed = len(all_positions) > 0
        if include_financed:
            period_end = period.end_date if period is not None else None
            as_of = financed_as_of or period_end
            pf = portfolio_financed(db, organisation_id,
                                    include_scope3=financed_include_scope3, as_of=as_of)
            if pf.get("as_of_filtered_empty"):
                # The as_of cutoff excluded EVERY position although the org holds some.
                # Do NOT freeze a false zero: leave financed_co2e None (not evaluated)
                # so the gate blocks the filing, and record the as_of so the message is
                # accurate ("as_of excluded every position") rather than "never tried".
                run.financed_co2e = None
                run.financed_as_of = as_of
                run.financed_include_scope3 = financed_include_scope3
                run.financed_fingerprint = None
            else:
                by_id = {p.id: p for p in all_positions}
                for ln in pf["lines"]:
                    p = by_id.get(ln["position_id"])
                    # Freeze the FULL attribution lineage: an assurer must be able to
                    # walk outstanding / denominator x investee emissions = financed,
                    # and the exposure is needed for IFRS S2 B58-B63's % covered.
                    db.add(RunFinancedLine(
                        run_id=run.id, position_id=ln["position_id"], ghgp_category=15,
                        co2e=ln["financed_total_tco2e"] * 1000.0,   # tCO2e -> kg (explicit)
                        details=json.dumps({
                            **ln,
                            "outstanding_amount": p.outstanding_amount if p else None,
                            "attribution_denominator": p.attribution_denominator if p else None,
                            "currency": p.currency if p else None,
                            "investee_scope1_tco2e": p.investee_scope1_tco2e if p else None,
                            "investee_scope2_tco2e": p.investee_scope2_tco2e if p else None,
                            "investee_scope3_tco2e": p.investee_scope3_tco2e if p else None,
                            "position_as_of_date": p.as_of_date if p else None,
                            "unit_note": "kg (PCAF tCO2e x1000)",
                            "pcaf_standard": "PCAF Part A Financed Emissions (Dec 2022)"})))
                run.financed_co2e = pf["financed_emissions_tco2e"]["total"] * 1000.0
                run.financed_as_of = as_of
                run.financed_include_scope3 = financed_include_scope3
                # Fingerprint only the positions that FED the figure (the as_of-included
                # set), so a position dated AFTER the cutoff — not in this figure — can be
                # added/edited without false-flagging a correctly-filed run as stale.
                included = [p for p in all_positions
                            if p.id in {ln["position_id"] for ln in pf["lines"]}]
                run.financed_fingerprint = _financed_fingerprint(included)

        # --- Inventory REMOVALS (GHG Protocol Land Sector & Removals) ---
        # A fourth frozen pool on the financed template, but arithmetic INVERTED:
        # removals SUBTRACT into a render-time net, never touching total_co2e. Removals
        # occur WITHIN the boundary, so each is weighted by the SAME entity share the
        # emissions use (else net = consolidated emissions - gross removals mixes bases).
        from ..models import RemovalRecord, RunRemovalLine
        rq = db.query(RemovalRecord).filter(
            RemovalRecord.organisation_id == organisation_id)
        if reporting_period_id is not None:
            rq = rq.filter(RemovalRecord.reporting_period_id == reporting_period_id)
        rem = rq.order_by(RemovalRecord.id).all()
        if include_removals is None:
            include_removals = len(rem) > 0
        if include_removals:
            run.removals_lsrg_version = LSRG_VERSION      # the dimension WAS evaluated
            rem_as_of = removals_as_of or (period.end_date if period is not None else None)
            included_rem = [r for r in rem
                            if rem_as_of is None or (r.as_of_date or "") <= rem_as_of]
            if rem and not included_rem:
                # FALSE-ZERO GUARD: an as_of that excludes every record must NOT freeze
                # a fabricated clean zero. Leave None so the gate blocks (R2).
                run.total_removals_co2e = None
                run.removals_reversed_co2e = None
                run.removals_as_of = rem_as_of
                run.removals_fingerprint = None
            else:
                gross_rem = 0.0
                reversed_rem = 0.0
                for r in included_rem:
                    _rent = ent_by_id.get(r.entity_id) if r.entity_id is not None else None
                    if r.entity_id is not None and _rent is None:
                        _rw, _rbasis, _rres = 1.0, "unresolved_entity_not_found", False
                    else:
                        _rw, _rbasis, _rres = entity_weight(approach, _rent)
                    gross_kg = r.quantity_tco2e * 1000.0
                    cons_kg = gross_kg * _rw
                    permanence_class = "high" if r.removal_category == "technological" else "low"
                    db.add(RunRemovalLine(
                        run_id=run.id, removal_record_id=r.id,
                        removal_category=r.removal_category, scope=r.scope,
                        record_kind=r.record_kind, co2e=cons_kg,
                        details=json.dumps({
                            "consolidation": {"entity_id": r.entity_id, "approach": approach,
                                              "share_factor": _rw, "share_basis": _rbasis,
                                              "resolved": _rres, "gross_co2e": gross_kg,
                                              "consolidated_co2e": cons_kg},
                            "method": r.method,
                            "quantification_method": r.quantification_method,
                            "storage_medium": r.storage_medium,
                            "expected_durability_years": r.expected_durability_years,
                            "monitoring_method": r.monitoring_method,
                            "monitoring_period_years": r.monitoring_period_years,
                            "reversal_accounting": r.reversal_accounting,
                            "attribute_retained": r.attribute_retained,
                            "credit_registry": r.credit_registry,
                            "credit_serial_if_sold": r.credit_serial_if_sold,
                            "reverses_record_id": r.reverses_record_id,
                            "uncertainty_pct": r.uncertainty_pct, "buffer_pct": r.buffer_pct,
                            "vintage_year": r.vintage_year, "as_of_date": r.as_of_date,
                            "gwp_set": gwp_set, "permanence_class": permanence_class,
                            "unit_note": "kg (tCO2e x1000); positive removal quantity",
                            "lsrg_note": "GHG Protocol Land Sector & Removals: reported "
                                         "separately, never netted into gross total_co2e"})))
                    if r.record_kind == "reversal":
                        reversed_rem += cons_kg
                    else:
                        gross_rem += cons_kg
                run.total_removals_co2e = gross_rem
                run.removals_reversed_co2e = reversed_rem
                run.removals_as_of = rem_as_of
                run.removals_fingerprint = _removals_fingerprint(included_rem)

        run.status = "complete"
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(run)
    return run
