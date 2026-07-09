import json
import hashlib
from datetime import date as date_cls, datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from ..models import (
    ActivityRecord, EmissionFactor, CalculationRun, EmissionLineItem, ReportingPeriod,
    MarketInstrument,
)
from .units import convert, UnitConversionError, QuantityError
from .gwp import co2e_from_gases, gwp

SCOPE_RULES = {
    "electricity":"2",
    "gas":"1",
    "diesel":"1",
    "flight":"3",
    "train":"3",
    "car":"3",
    "waste":"3",
    "spend":"3"
}

class ReportingPeriodError(ValueError):
    """Invalid reporting period for a calculation (wrong org, frozen, or missing)."""


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


def activities_fingerprint(acts: List[ActivityRecord]) -> str:
    """Stable hash of the activity set (id/factor/quantity/unit).

    Changes if any activity is added, removed, re-mapped, or edited — even when the
    activity count is unchanged — so a run computed against this set can be detected
    as stale by content, not just by count.
    """
    parts = sorted(f"{a.id}:{a.factor_id}:{a.quantity}:{a.unit}" for a in acts)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


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
        return qty_in_factor_unit * co2e_from_gases(factor_gases(factor), gwp_set)
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

    def _applies(self, inst: MarketInstrument, activity_date: Optional[str]) -> bool:
        if inst.gwp_set and self.run_gwp_set and inst.gwp_set != self.run_gwp_set:
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
                 grid_rate_per_kwh: float) -> dict:
        """Cover ``kwh`` from the pool; residual is priced at the grid rate."""
        allocations = []
        needed = kwh
        co2e = 0.0
        for inst in self.instruments:
            if needed <= 0:
                break
            if not self._applies(inst, activity_date):
                continue
            rem = self.remaining[inst.id]
            take = needed if rem is None else min(rem, needed)
            if take <= 0:
                continue
            co2e += take * inst.kg_co2e_per_kwh
            if rem is not None:
                self.remaining[inst.id] = rem - take
            needed -= take
            allocations.append({
                "instrument_id": inst.id,
                "instrument_type": inst.instrument_type,
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
                "allocations": allocations,
                "instruments_skipped_gwp_vintage": self.skipped_vintage}


def compute_co2e(db: Session, organisation_id: int, gwp_set: str = "AR6",
                 reporting_period_id: Optional[int] = None) -> CalculationRun:
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

    acts = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == organisation_id).all()

    undatable = set()
    if period is not None:
        p_start = _parse_iso_date(period.start_date)
        p_end = _parse_iso_date(period.end_date)
        in_period = []
        for a in acts:
            adate = _parse_iso_date(a.date)
            if adate is None:
                undatable.add(a.id)   # keep in run; becomes a data_error below
                in_period.append(a)
                continue
            if p_start and adate < p_start:
                continue
            if p_end and adate > p_end:
                continue
            in_period.append(a)
        acts = in_period

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
            if not per_gas and a.factor.gwp_set and gwp_set and a.factor.gwp_set != gwp_set:
                run.gwp_mismatch += 1
                errors.append({"activity_id": a.id,
                               "error": f"factor GWP set {a.factor.gwp_set} != requested {gwp_set}"})
                continue
            if a.quantity is not None and a.quantity < 0:
                run.data_errors += 1
                errors.append({"activity_id": a.id, "error": "negative quantity"})
                continue
            try:
                co2e = compute_activity_co2e(a.quantity, a.unit, a.factor, gwp_set=gwp_set)
            except QuantityError as exc:          # None / non-finite / non-numeric
                run.data_errors += 1
                errors.append({"activity_id": a.id, "error": str(exc)})
                continue
            except UnitConversionError as exc:    # incompatible / ambiguous / malformed
                run.unit_errors += 1
                errors.append({"activity_id": a.id, "error": str(exc)})
                continue

            detail = {
                "factor_id": a.factor_id,
                "activity_unit": a.unit,
                "factor_unit": a.factor.unit,
                "quantity": a.quantity,
                "calc_method": "per_gas" if per_gas else "aggregate",
            }
            if per_gas:
                gases = factor_gases(a.factor)
                detail["gwp_set_applied"] = gwp_set
                detail["gases_kg_per_unit"] = gases
                detail["gwp_values"] = {g: gwp(g, gwp_set) for g in gases}
            else:
                detail["gwp_set"] = a.factor.gwp_set
                detail["factor_value"] = a.factor.value

            scope = a.scope or SCOPE_RULES.get((a.category or "").lower(), "3")
            a.scope = scope
            line_items.append(EmissionLineItem(
                run_id=run.id, activity_id=a.id, scope=scope, method="location", co2e=co2e,
                details=json.dumps(detail),
            ))
            total += co2e
            run.mapped += 1

            # GHG Protocol dual Scope 2: every Scope 2 activity ALSO gets a
            # market-based line item. Market instruments are ELECTRICITY
            # contracts, so they only ever apply to electricity — other Scope 2
            # commodities (heat/steam) fall back to the location figure.
            if scope == "2":
                market_co2e, market_detail = co2e, dict(detail)
                is_electricity = (a.category or "").lower() == "electricity"
                if is_electricity and instruments:
                    try:
                        kwh = convert(a.quantity, a.unit, "kWh")
                        grid_rate = (co2e / kwh) if kwh else 0.0
                        alloc = pool.allocate(kwh, a.date, grid_rate)
                        market_co2e = alloc.pop("co2e")
                        market_detail = alloc
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
        run.notes = json.dumps(errors)
        run.status = "complete"
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(run)
    return run
