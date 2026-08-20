"""Monte Carlo propagation of per-line pedigree uncertainty to an inventory interval.

``services/dq.py`` already derives a lognormal geometric standard deviation for
every emission line from the ecoinvent pedigree matrix. Until now that sigma was
computed, frozen into the line's ``details``, and then DISCARDED at aggregation:
the run reported a single total with no interval, and the only surviving summary
was an emissions-weighted 1-5 score. This module propagates the distributions
that were already there.

REPRODUCTION CONTRACT (same as every report module): this reads ONLY what the run
froze — ``calculation_runs``, ``emission_line_items.co2e/.details``, and the row
counts of ``run_financed_lines`` / ``run_removal_lines`` for coverage disclosure.
It never joins ActivityRecord or EmissionFactor, so re-running the propagation on
a filed run years later returns a BIT-IDENTICAL interval even after activities are
re-mapped or factors are corrected.

Three decisions carry the methodology, and each is reported in the payload rather
than buried here:

1. CORRELATION IS THE WHOLE BALLGAME. Sampling every line independently is the
   flattering assumption and it is wrong: n independent lines shrink the relative
   interval by ~sqrt(n), so a 4,000-line inventory of individually +/-40% lines
   reports a +/-1% total. Lines resolved to the SAME emission factor share that
   factor's error exactly — it is common-mode, not independent. The default
   (``by_factor``) draws one standard normal per distinct factor and applies it to
   every line resolved to that factor. We cannot decompose the stored sigma into
   its factor-borne and line-borne parts (dq.py combines five indicators into one
   number), so instead of guessing we BOUND it: every response also carries the
   ``independent`` (narrowest) and ``perfect`` (widest) intervals, and the reader
   sees how much the answer depends on the assumption.

2. THE POINT ESTIMATE IS THE MEDIAN, NOT THE MEAN. A pedigree GSD describes a
   lognormal whose MEDIAN is the reported value; its arithmetic mean is higher by
   exp(sigma^2/2). So ``simulated_mean > deterministic_total`` is the correct
   behaviour of a right-skewed distribution, not a bug, and both are reported side
   by side with the skew named. Never "correct" the total toward the simulated
   mean: the total is the inventory, the simulation is a statement about it.

3. AN INTERVAL THAT COVERS PART OF THE INVENTORY MUST SAY SO. Financed emissions
   (RunFinancedLine) and removals (RunRemovalLine) are deliberately NOT
   EmissionLineItems and carry no pedigree sigma, so they cannot be propagated.
   Rather than quietly reporting an interval on a subset as though it covered the
   whole, ``coverage`` states the propagated amount, the excluded pools by name and
   amount, and ``covers_full_inventory``.

Determinism: the seed is derived by SHA-256 from the frozen inputs (run, method,
mode, iterations, and the sorted per-line median/sigma/group triples) — never from
the clock and never from dict ordering. Re-running returns identical numbers, and
``input_fingerprint`` proves which inputs produced them.
"""
import hashlib
import json
import math
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from .frozen import parse_detail
from ..models import (
    CalculationRun, EmissionLineItem, RunFinancedLine, RunRemovalLine,
)

# Bumped whenever the sampling model changes in a way that alters results for
# unchanged inputs. Frozen into every payload so an old figure stays attributable.
PROPAGATION_VERSION = "mc-v1"

CORRELATION_MODES = ("independent", "by_factor", "perfect")
DEFAULT_CORRELATION = "by_factor"
DEFAULT_ITERATIONS = 10_000
MIN_ITERATIONS = 1_000
MAX_ITERATIONS = 200_000

# Sigma for a line whose frozen details carry no pedigree score at all (a run
# computed before dq.py existed, or a line whose factor was never scored). The
# module's own conservative-by-default rule applies: an unscorable line takes the
# ALL-INDICATORS-POOR sigma, never a flattering mid value. Derived from dq's
# published tables rather than hard-coded, so the two can never drift apart.
def _all_poor_sigma() -> float:
    from .dq import _UF, _BASIC_UNCERTAINTY_DEFAULT
    logvar = sum(math.log(_UF[k][4]) ** 2 for k in _UF) \
        + math.log(_BASIC_UNCERTAINTY_DEFAULT) ** 2
    return math.sqrt(logvar)


SIGMA_UNSCORED = _all_poor_sigma()

# Hard ceiling on any sigma read out of a frozen details blob. The pedigree matrix
# cannot produce more than SIGMA_UNSCORED (~0.92), so anything approaching this is
# corrupt rather than merely poor — and exp(sigma * z) overflows to inf around
# sigma ~ 140, which would silently turn the whole interval into nan. Clamping and
# COUNTING is the fail-closed answer: the number stays finite and the payload says
# the value was not trusted as given.
SIGMA_CEILING = 5.0

# Sampling is chunked over lines so peak memory stays O(chunk x iterations)
# instead of O(lines x iterations) — a 50k-line inventory at 10k iterations would
# otherwise materialise a 4 GB array.
_CHUNK = 256


def _percentile_pair(confidence: float) -> tuple:
    """Two-sided percentile bounds for a confidence level, e.g. 0.95 -> (2.5, 97.5).

    Rounded because the raw arithmetic yields 2.500000000000002 for the commonest
    input, and a disclosure that prints its own confidence level as fifteen
    significant figures reads as a defect to the reader who has to sign it.
    """
    tail = round((1.0 - confidence) / 2.0 * 100.0, 6)
    return (tail, round(100.0 - tail, 6))


def _basis_rows(db: Session, run_id: int, method: str) -> list:
    """The rows that constitute one reporting basis.

    Location basis is exactly the location lines — the calc engine's invariant is
    ``sum(location lines) == run.total_co2e``.

    Market basis is NOT simply the market lines. The engine writes a market line
    only for Scope 2 (market instruments are electricity contracts); every other
    activity carries its LOCATION figure into ``total_co2e_market``. Selecting on
    ``method == 'market'`` therefore returns Scope 2 alone: on a mixed inventory
    that propagated 200 kg of a 10,200 kg market total and reported the missing
    98% as reconciliation drift. The market basis is the market line where one
    exists, the location line everywhere else.
    """
    rows = (db.query(EmissionLineItem)
            .filter(EmissionLineItem.run_id == run_id)
            .order_by(EmissionLineItem.id)
            .all())
    if method == "location":
        return [(r, "location") for r in rows if r.method == "location"]
    market_activities = {r.activity_id for r in rows if r.method == "market"}
    return [(r, r.method) for r in rows
            if r.method == "market"
            or (r.method == "location" and r.activity_id not in market_activities)]


def _load_lines(db: Session, run_id: int, method: str,
                include_crosswalk: bool = False) -> list:
    """Frozen (median, sigma, group_key, unscored, basis) per line of one basis.

    ``median`` keeps its sign. Multiplying by a strictly positive lognormal
    multiplier preserves it, so a negative line is sampled on its magnitude with
    the sign carried through, and the lognormal is never asked to describe a
    negative quantity.
    """
    out = []
    for r, basis in _basis_rows(db, run_id, method):
        detail = parse_detail(r.details)
        dq = detail.get("data_quality") or {}
        sigma = dq.get("sigma_log")
        unscored = not isinstance(sigma, (int, float)) or not math.isfinite(sigma) \
            or sigma < 0
        if unscored:
            sigma = SIGMA_UNSCORED

        # The declared classification chain's own contribution, frozen at compute
        # time. Variances add on the log scale, so the chain widens the line's
        # band rather than replacing it. A line with no declared chain adds
        # nothing — which is NOT the same as carrying no crosswalk error, and the
        # payload reports the unquantified share separately.
        xw = detail.get("crosswalk") or {}
        xw_declared = bool(xw.get("declared"))
        xw_var = xw.get("total_variance") if xw.get("quantifiable") else None
        xw_applied = isinstance(xw_var, (int, float)) and math.isfinite(xw_var) \
            and xw_var > 0
        if include_crosswalk and xw_applied:
            sigma = math.sqrt(sigma ** 2 + xw_var)

        clamped = sigma > SIGMA_CEILING
        if clamped:
            sigma = SIGMA_CEILING
        # Group by the factor the line resolved to. A line with no factor_id cannot
        # share common-mode error with anything, so it groups alone under its own id.
        fid = detail.get("factor_id")
        group = f"factor:{fid}" if fid is not None else f"line:{r.id}"
        co2e = r.co2e if isinstance(r.co2e, (int, float)) and math.isfinite(r.co2e) else 0.0
        out.append({
            "line_id": r.id, "activity_id": r.activity_id, "scope": r.scope,
            "median": float(co2e), "sigma": float(sigma),
            "group": group, "unscored": unscored, "clamped": clamped, "basis": basis,
            "crosswalk_declared": xw_declared,
            "crosswalk_variance": xw_var if xw_applied else None,
            "crosswalk_unquantifiable": xw_declared and not xw_applied,
        })
    return out


def _fingerprint(run_id: int, method: str, correlation: str, iterations: int,
                 confidence: float, lines: list,
                 include_crosswalk: bool = False) -> str:
    """SHA-256 over the exact inputs the simulation consumes.

    Sorted and rounded so neither row order nor float repr can move the seed;
    identical inputs therefore produce an identical stream on any machine.
    """
    payload = {
        "v": PROPAGATION_VERSION,
        "run": run_id, "method": method, "correlation": correlation,
        "iterations": iterations, "confidence": round(confidence, 6),
        "include_crosswalk": bool(include_crosswalk),
        "lines": sorted(
            [f"{ln['group']}|{ln['median']:.10g}|{ln['sigma']:.10g}" for ln in lines]
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _seed_from(fingerprint: str) -> int:
    """First 8 bytes of the fingerprint as the PRNG seed."""
    return int(fingerprint[:16], 16)


def _simulate(lines: list, correlation: str, iterations: int, seed: int) -> tuple:
    """Sample the inventory total; return (totals, per-group variance, per-group mean).

    One standard normal is drawn per INDEPENDENT SOURCE — a line, a factor group, or
    the whole inventory, per ``correlation`` — and every line scales it by its own
    sigma: value_i = median_i * exp(sigma_i * Z_source(i)). Heterogeneous sigmas
    inside one factor group are therefore handled exactly, without needing the group
    to share a single sigma.
    """
    rng = np.random.default_rng(seed)
    totals = np.zeros(iterations, dtype=np.float64)
    group_var, group_mean = {}, {}

    if correlation == "perfect":
        # One common-mode draw for the entire inventory.
        z = rng.standard_normal(iterations)
        for ln in lines:
            contrib = ln["median"] * np.exp(ln["sigma"] * z)
            totals += contrib
            g = ln["group"]
            group_mean[g] = group_mean.get(g, 0.0) + float(contrib.mean())
        # Under perfect correlation every line moves together, so a per-group
        # variance share is not a decomposition of independent contributions.
        # Reported as None rather than as a misleading number.
        return totals, None, group_mean

    if correlation == "by_factor":
        by_group = {}
        for ln in lines:
            by_group.setdefault(ln["group"], []).append(ln)
        for g in sorted(by_group):
            z = rng.standard_normal(iterations)
            acc = np.zeros(iterations, dtype=np.float64)
            for ln in by_group[g]:
                acc += ln["median"] * np.exp(ln["sigma"] * z)
            totals += acc
            group_var[g] = float(acc.var())
            group_mean[g] = float(acc.mean())
        return totals, group_var, group_mean

    # independent: every line is its own source. Chunked to bound peak memory.
    for start in range(0, len(lines), _CHUNK):
        block = lines[start:start + _CHUNK]
        med = np.array([ln["median"] for ln in block], dtype=np.float64)[:, None]
        sig = np.array([ln["sigma"] for ln in block], dtype=np.float64)[:, None]
        z = rng.standard_normal((len(block), iterations))
        contrib = med * np.exp(sig * z)
        totals += contrib.sum(axis=0)
        for i, ln in enumerate(block):
            g = ln["group"]
            # Independent lines: variances add, so a factor group's variance is the
            # sum of its lines' variances.
            group_var[g] = group_var.get(g, 0.0) + float(contrib[i].var())
            group_mean[g] = group_mean.get(g, 0.0) + float(contrib[i].mean())
    return totals, group_var, group_mean


def _interval(totals: np.ndarray, deterministic: float, confidence: float) -> dict:
    lo_p, hi_p = _percentile_pair(confidence)
    lo, hi = (float(x) for x in np.percentile(totals, [lo_p, hi_p]))
    mean = float(totals.mean())
    median = float(np.median(totals))
    out = {
        "low": round(lo, 3), "high": round(hi, 3),
        "simulated_mean": round(mean, 3),
        "simulated_median": round(median, 3),
        "percentiles": {"low": lo_p, "high": hi_p},
    }
    if deterministic:
        out["low_pct_of_total"] = round(100.0 * lo / deterministic, 2)
        out["high_pct_of_total"] = round(100.0 * hi / deterministic, 2)
        # The conventional one-number summary: half-width as a percentage of the
        # reported total. Only meaningful for a symmetric-ish band, so the two
        # one-sided figures above are reported too and should be preferred.
        out["relative_half_width_pct"] = round(
            100.0 * (hi - lo) / (2.0 * abs(deterministic)), 2)
    else:
        out["low_pct_of_total"] = None
        out["high_pct_of_total"] = None
        out["relative_half_width_pct"] = None
    return out


def _coverage(db: Session, run: CalculationRun, method: str, lines: list) -> dict:
    """What the interval covers, and — by name and amount — what it does not."""
    propagated = sum(ln["median"] for ln in lines)
    headline = (run.total_co2e_market if method == "market" else run.total_co2e) or 0.0

    financed = sum(
        r.co2e or 0.0
        for r in db.query(RunFinancedLine).filter(RunFinancedLine.run_id == run.id).all())
    removals = sum(
        r.co2e or 0.0
        for r in db.query(RunRemovalLine).filter(RunRemovalLine.run_id == run.id).all())
    biogenic = run.total_biogenic_co2e or 0.0

    excluded = []
    if financed:
        excluded.append({
            "pool": "financed_emissions_cat15", "co2e_kg": round(financed, 3),
            "reason": "PCAF financed lines are frozen as RunFinancedLine, not "
                      "EmissionLineItem, and carry a PCAF 1-5 data-quality score "
                      "rather than a pedigree sigma — there is no distribution to "
                      "sample.",
        })
    if removals:
        excluded.append({
            "pool": "removals", "co2e_kg": round(removals, 3),
            "reason": "Removals are frozen as RunRemovalLine in their own "
                      "positive-signed pool and carry no pedigree sigma.",
        })
    if biogenic:
        excluded.append({
            "pool": "biogenic_co2", "co2e_kg": round(biogenic, 3),
            "reason": "ISO 14067 biogenic CO2 is reported separately and is never "
                      "netted into the total this interval describes.",
        })

    # The calc engine's invariant is sum(location line items) == run.total_co2e.
    # A drift here means the run was written by a different engine version or the
    # rows were touched — surface it, never silently reconcile.
    drift = propagated - headline
    reconciles = abs(drift) <= max(1e-6, abs(headline) * 1e-9)

    out = {
        "basis": method,
        "propagated_co2e_kg": round(propagated, 3),
        "run_total_co2e_kg": round(headline, 3),
        "reconciles_to_run_total": reconciles,
        "reconciliation_drift_kg": None if reconciles else round(drift, 6),
        "covers_full_inventory": reconciles and not excluded,
        "excluded_pools": excluded,
        "note": "The interval describes ONLY the propagated amount above."
                if excluded else None,
    }
    if method == "market":
        # The market basis is assembled from two line kinds; say which, so a reader
        # can see that the non-Scope-2 part is the location figure by design rather
        # than assuming the market total was fully re-priced.
        from_market = sum(1 for ln in lines if ln["basis"] == "market")
        out["basis_composition"] = {
            "market_priced_lines": from_market,
            "location_carried_lines": len(lines) - from_market,
            "note": "Market instruments are electricity contracts, so the engine "
                    "prices only Scope 2 on a market basis; every other activity "
                    "carries its location figure into the market total unchanged.",
        }
    return out


def _sensitivity(group_var: Optional[dict], group_mean: Optional[dict],
                 lines: list, top_n: int) -> dict:
    """Which factor groups drive the interval width.

    Variance shares are exact under the sampled model when groups are independent
    of one another — true for ``independent`` and ``by_factor``. Under ``perfect``
    every group moves together and no such decomposition exists, so the share is
    withheld rather than fabricated.
    """
    lines_per_group, unscored_per_group = {}, {}
    for ln in lines:
        g = ln["group"]
        lines_per_group[g] = lines_per_group.get(g, 0) + 1
        if ln["unscored"]:
            unscored_per_group[g] = unscored_per_group.get(g, 0) + 1

    if group_var is None:
        return {
            "available": False,
            "reason": "Variance shares require between-group independence; under "
                      "'perfect' correlation the whole inventory is one common-mode "
                      "source and no decomposition exists.",
            "contributors": [],
        }

    total_var = sum(group_var.values())
    ranked = sorted(group_var.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    contributors = [{
        "group": g,
        "variance_share_pct": round(100.0 * v / total_var, 2) if total_var else None,
        "mean_contribution_kg": round(group_mean.get(g, 0.0), 3),
        "lines": lines_per_group.get(g, 0),
        "unscored_lines": unscored_per_group.get(g, 0),
    } for g, v in ranked]
    return {
        "available": True,
        "basis": "share of total sampled variance, groups independent",
        "contributors": contributors,
    }


def propagate(db: Session, run_id: int, *, method: str = "location",
              correlation: str = DEFAULT_CORRELATION,
              iterations: int = DEFAULT_ITERATIONS,
              confidence: float = 0.95, top_n: int = 10,
              include_crosswalk: bool = False) -> dict:
    """Propagate frozen per-line pedigree sigmas to an inventory-level interval.

    Returns a payload carrying the interval under the requested correlation mode,
    the same interval under both bounding assumptions, a coverage statement naming
    every pool the interval does NOT describe, a variance-share ranking, and the
    fingerprint + seed that make the result reproducible.

    Refuses (``available: False`` with a reason) rather than returning a number it
    cannot stand behind: unknown run, unknown mode, no lines, or a zero-variance
    inventory.
    """
    if correlation not in CORRELATION_MODES:
        return {"available": False,
                "reason": f"correlation must be one of {list(CORRELATION_MODES)}"}
    if method not in ("location", "market"):
        return {"available": False, "reason": "method must be 'location' or 'market'"}
    if not (MIN_ITERATIONS <= iterations <= MAX_ITERATIONS):
        return {"available": False,
                "reason": f"iterations must be {MIN_ITERATIONS}..{MAX_ITERATIONS}"}
    if not (0.5 <= confidence < 1.0):
        return {"available": False, "reason": "confidence must be in [0.5, 1.0)"}

    run = db.query(CalculationRun).filter(CalculationRun.id == run_id).first()
    if run is None:
        return {"available": False, "reason": f"run {run_id} not found"}

    lines = _load_lines(db, run_id, method, include_crosswalk)
    if not lines:
        return {"available": False, "run_id": run_id, "method": method,
                "reason": f"run {run_id} has no emission lines on the {method} "
                          "basis to propagate"}

    deterministic = sum(ln["median"] for ln in lines)
    unscored = sum(1 for ln in lines if ln["unscored"])
    clamped = sum(1 for ln in lines if ln["clamped"])
    zero_sigma = all(ln["sigma"] == 0.0 for ln in lines)

    fingerprint = _fingerprint(run_id, method, correlation, iterations,
                               confidence, lines, include_crosswalk)
    seed = _seed_from(fingerprint)

    if zero_sigma:
        return {
            "available": False, "run_id": run_id, "method": method,
            "reason": "every line has sigma 0 — the inventory carries no quantified "
                      "uncertainty to propagate, so an interval would be the point "
                      "estimate restated as a range.",
            "deterministic_total_co2e_kg": round(deterministic, 3),
            "input_fingerprint": fingerprint,
        }

    totals, group_var, group_mean = _simulate(lines, correlation, iterations, seed)
    headline = _interval(totals, deterministic, confidence)

    # ALL three modes are always reported, whichever was requested: the reader needs
    # the narrowest and widest defensible bounds AND the by_factor default to judge
    # how much the answer rests on the correlation assumption. Each re-seeds from its
    # own fingerprint, so a bound is reproducible on its own terms and never depends
    # on call order.
    bounds = {}
    for mode in CORRELATION_MODES:
        if mode == correlation:
            bounds[mode] = headline
            continue
        fp_m = _fingerprint(run_id, method, mode, iterations, confidence, lines,
                            include_crosswalk)
        t_m, _, _ = _simulate(lines, mode, iterations, _seed_from(fp_m))
        bounds[mode] = _interval(t_m, deterministic, confidence)

    return {
        "available": True,
        "run_id": run_id,
        "method": method,
        "propagation_version": PROPAGATION_VERSION,
        "deterministic_total_co2e_kg": round(deterministic, 3),
        "confidence": confidence,
        "correlation": correlation,
        "correlation_note": (
            "Lines resolved to the same emission factor share that factor's error "
            "exactly; sampling them independently would shrink the interval by "
            "roughly sqrt(n) and overstate precision. The stored sigma cannot be "
            "decomposed into factor-borne and line-borne parts, so 'independent' "
            "and 'perfect' are reported as the narrowest and widest defensible "
            "bounds on the same inventory."),
        "interval": headline,
        "correlation_bounds": bounds,
        "reconciles_with": {
            "summary_data_quality_band": "reports/summary.py -> data_quality."
                                         "approx_ci95_low/high",
            "relationship": "That closed-form band assumes fully correlated line "
                            "errors, and under one shared draw the total is monotone "
                            "in it — so it IS this payload's 'perfect' bound, and the "
                            "two agree to Monte Carlo sampling noise. Disclose the "
                            "'by_factor' interval; the summary band is the "
                            "conservative outer limit, not a competing estimate.",
        },
        "skew_note": (
            "A pedigree GSD describes a lognormal whose MEDIAN is the reported "
            "total; its arithmetic mean is higher by exp(sigma^2/2). "
            "simulated_mean exceeding deterministic_total_co2e_kg is the correct "
            "behaviour of a right-skewed distribution, not a discrepancy — the "
            "total is the inventory, the simulation is a statement about it."),
        "crosswalk": {
            "included": include_crosswalk,
            "lines_with_declared_chain": sum(1 for l in lines if l["crosswalk_declared"]),
            "lines_with_quantified_chain": sum(
                1 for l in lines if l["crosswalk_variance"]),
            "lines_declared_but_unquantifiable": sum(
                1 for l in lines if l["crosswalk_unquantifiable"]),
            "lines_without_a_declared_chain": sum(
                1 for l in lines if not l["crosswalk_declared"]),
            "note": (
                "Classification-chain variance is added to each line's pedigree "
                "variance on the log scale, widening the band rather than replacing "
                "it. The registry has long recorded that a chart-of-accounts -> "
                "UNSPSC -> NAICS mapping often carries more error than the factor "
                "itself; this is where that shows up in the number."
                if include_crosswalk else
                "NOT INCLUDED. The interval above reflects factor-and-activity "
                "pedigree only. Pass include_crosswalk=true to add the declared "
                "classification chain's measured contribution."),
            "undeclared_note": (
                "A line with no declared chain adds nothing — which is NOT the same "
                "as carrying no crosswalk error. It is unquantified, not absent, and "
                "the count above says how many lines are in that position."),
        },
        "coverage": _coverage(db, run, method, lines),
        "sensitivity": _sensitivity(group_var, group_mean, lines, top_n),
        "lines": {
            "count": len(lines),
            "distinct_groups": len({ln["group"] for ln in lines}),
            "unscored": unscored,
            "unscored_sigma_applied": round(SIGMA_UNSCORED, 4) if unscored else None,
            "unscored_note": (
                f"{unscored} line(s) carried no pedigree score and were sampled at "
                f"the all-indicators-poor sigma ({SIGMA_UNSCORED:.4f}), per the "
                f"conservative-by-default rule in services/dq.py — a missing score "
                f"widens the band, never narrows it.") if unscored else None,
            "sigma_clamped": clamped,
            "sigma_clamped_note": (
                f"{clamped} line(s) carried a sigma above the {SIGMA_CEILING} "
                f"ceiling and were clamped to it. The pedigree matrix cannot "
                f"produce more than {SIGMA_UNSCORED:.4f}, so such a value is "
                f"corrupt rather than merely poor; it is clamped and counted "
                f"instead of being sampled into an infinite interval."
            ) if clamped else None,
        },
        "reproducibility": {
            "iterations": iterations,
            "seed": seed,
            "input_fingerprint": fingerprint,
            "contract": "Seed is SHA-256 over the frozen inputs (run, method, mode, "
                        "iterations, confidence, and every line's median/sigma/group). "
                        "Identical inputs return bit-identical numbers; a changed "
                        "fingerprint proves the inputs moved, not the sampler.",
        },
    }
