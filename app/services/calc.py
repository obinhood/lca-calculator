import json
import math
import hashlib
from datetime import date as date_cls, datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from ..models import (
    ActivityRecord, EmissionFactor, CalculationRun, EmissionLineItem, ReportingPeriod,
    MarketInstrument, Scope3CategoryDeclaration, RunScope3Declaration,
)
from .ghgp import (
    GHGP_STANDARD_VERSION, CATEGORY_MAP_VERSION, CATEGORIES, taxonomy,
    derive_ghgp_category, boundary_meets_minimum, declarations_fingerprint,
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
FINGERPRINT_VERSION = "v3"


def activities_fingerprint(acts: List[ActivityRecord]) -> str:
    """Stable hash of the activity set (id/factor/quantity/unit/date/category/ghgp_category).

    Changes if any activity is added, removed, re-mapped, or edited — even when the
    activity count is unchanged — so a run computed against this set can be detected
    as stale by content, not just by count. date and category are included because
    both change the RESULT (period attribution and scope classification); ghgp_category
    because it changes the DISCLOSURE (v3).
    """
    parts = sorted(
        f"{a.id}:{a.factor_id}:{a.quantity}:{a.unit}:{a.date}:{a.category}:{a.ghgp_category}"
        for a in acts)
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
                 activity_market: Optional[str], grid_rate_per_kwh: float) -> dict:
        """Cover ``kwh`` from the pool; residual is priced at the grid rate."""
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
        if needed > 0:
            co2e += needed * grid_rate_per_kwh
        covered = kwh - needed
        basis = ("contractual_instrument" if needed <= 0 and allocations
                 else "partial_contractual" if allocations
                 else "grid_average_fallback")
        return {"co2e": co2e, "kwh": kwh, "kwh_contractual": covered,
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
                 financed_include_scope3: bool = True) -> CalculationRun:
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
        for a in acts:
            if a.id in undatable:
                run.data_errors += 1
                errors.append({"activity_id": a.id,
                               "error": "missing/malformed date in a period-scoped run"})
                continue
            if not a.factor:
                run.unmapped += 1
                continue
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

            _pdate = _parse_iso_date(a.date)
            spend_steps = None
            if (a.factor.method_type or "") == "spend_based":
                # Spend-based EEIO: normalize the amount to the factor's currency
                # and base year (inflation + base-year FX), fail-closed on missing
                # reference data, then apply the per-currency factor.
                try:
                    amt = float(a.quantity)
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
                    co2e = compute_activity_co2e(a.quantity, a.unit, a.factor, gwp_set=gwp_set)
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

            dq = line_dq(a.factor, a, a.mapping_basis, _pdate.year if _pdate else None)
            detail = {
                "factor_id": a.factor_id,
                "activity_unit": a.unit,
                "factor_unit": a.factor.unit,
                "quantity": a.quantity,
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
                qty_fu = convert(a.quantity, a.unit, a.factor.unit)
                biogenic = qty_fu * a.factor.kg_co2_biogenic
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
                detail["ghgp_min_boundary_met"] = boundary_meets_minimum(
                    ghgp_cat, a.factor.lca_boundary)
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
                market_co2e, market_detail = co2e, dict(detail)
                # Biogenic belongs to the (single) location line only; don't let it
                # appear twice across the location+market lineage pair.
                market_detail.pop("biogenic_co2e", None)
                is_electricity = (a.category or "").lower() == "electricity"
                if is_electricity and instruments:
                    try:
                        kwh = convert(a.quantity, a.unit, "kWh")
                        grid_rate = (co2e / kwh) if kwh else 0.0
                        # a.geo is the consumption's grid/market — instruments only
                        # cover matching-market load (a US REC can't zero DE consumption).
                        alloc = pool.allocate(kwh, a.date, a.geo, grid_rate)
                        market_co2e = alloc.pop("co2e")
                        # MERGE, don't replace: a wholesale replace discarded factor_id,
                        # method_type, data_quality, scope_source and every ghgp_* key
                        # from the market line's frozen lineage.
                        market_detail = {**detail, **alloc}
                        market_detail.pop("biogenic_co2e", None)
                    except UnitConversionError as exc:
                        market_detail["method_basis"] = "grid_average_fallback"
                        market_detail["fallback_reason"] = str(exc)
                else:
                    market_detail["method_basis"] = "grid_average_fallback"
                    if not is_electricity:
                        market_detail["fallback_reason"] = \
                            "non-electricity scope 2: electricity instruments not applicable"
                line_items.append(EmissionLineItem(
                    run_id=run.id, activity_id=a.id, scope=scope, method="market",
                    co2e=market_co2e, details=json.dumps(market_detail),
                ))
                total_market += market_co2e
            else:
                total_market += co2e

        db.add_all(line_items)
        run.total_co2e = total
        run.total_co2e_market = total_market
        run.total_biogenic_co2e = total_biogenic
        # Emissions-weighted data-quality score (1 best .. 5 worst); 0.0 = no data.
        run.data_quality_score = round(dq_weighted_sum / total, 3) if total else 0.0
        run.notes = json.dumps(errors)

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
                method_description=d.method_description if d else None,
                calculation_tools=d.calculation_tools if d else None,
                primary_data_pct=d.primary_data_pct if d else None,
                screened_at=d.screened_at if d else None,
                ghgp_standard_version=GHGP_STANDARD_VERSION,
                frozen_at=frozen_at,
            ))
        run.ghgp_standard_version = GHGP_STANDARD_VERSION
        run.ghgp_map_version = CATEGORY_MAP_VERSION
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
                for ln in pf["lines"]:
                    db.add(RunFinancedLine(
                        run_id=run.id, position_id=ln["position_id"], ghgp_category=15,
                        co2e=ln["financed_total_tco2e"] * 1000.0,   # tCO2e -> kg (explicit)
                        details=json.dumps({**ln, "unit_note": "kg (PCAF tCO2e x1000)",
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

        run.status = "complete"
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(run)
    return run
