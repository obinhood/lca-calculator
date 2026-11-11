"""Prior-period comparatives: the ONE place a disclosure subtracts two runs.

Every primary annual framework this platform renders — ESRS E1, SECR, ISSB S2, GRI,
SB 253 — requires the prior period's figures beside the current ones. Until this module
existed each renderer published exactly one run, so a second-year filer could not produce
a compliant statement at all.

THE POINT OF THIS MODULE IS THAT THERE IS ONLY ONE OF IT, for the same reason
`services.comparability.cross_run_gates` is one function: the last time renderers each
built their own two-run logic, ISO 14064-2 re-implemented the boundary test as a bare
inequality on the consolidation approach alone — which a divestment leaves untouched —
and so published an entity leaving the group as a substantiated project reduction, with
an empty blocker list. A comparative is the shape that defect takes, so the comparative
is built once.

(That sentence is deliberately paraphrased. `tests/test_cross_run_gates.py` scans this
package for the re-derivation as a literal substring, so quoting the forbidden
comparison verbatim here would trip the guard — and relaxing the guard to ignore
comments would be the same fail-open defect it exists to catch.)

Three properties are load-bearing:

1. **The gate question is asked, never assembled.** `cross_run_gates` returns EVERY reason
   two runs cannot be subtracted — period length, boundary, GWP vintage, pedigree
   compilation, residual mix — and all of them are surfaced. This module never reaches
   past it to a component gate, and never adds a sixth of its own invention.

2. **The same extractor runs over both runs.** A comparative built by reading the current
   figures from one code path and the prior figures from another is a difference between
   two code paths as much as between two periods. Callers pass ONE `extract` function and
   it is applied to each run, so a figure can only be compared with itself.

3. **A comparative that cannot be computed is `cannot_determine` with its reasons named.**
   Never a blank, never a zero — a zero delta reads as "no change", which is a published
   claim, and an absent block reads as "nothing to report". And the three ways it can fail
   are DIFFERENT FACTS that must read differently:
     * `no_prior_run_requested` — nobody asked for a comparative. Whether a prior period
       is nonetheless on file is stated, because a second-year filer who omitted one is
       the exact customer this module exists for.
     * `prior_run_not_found` — an id was supplied that is not this organisation's.
     * `not_comparable` — a real prior run was supplied and the shared gates refused it.
   Collapsing these into one "unavailable" would hide the only one that is a filer error.
"""
from typing import Callable, Optional

from sqlalchemy.orm import Session

from . import derivation as D
from ..models import CalculationRun
from ..services.calc import _parse_iso_date
from ..services.comparability import (cross_run_gates, period_days, period_info,
                                      period_payload, run_period)

# How a comparative reports a figure it cannot compute. Mirrors the engine's convention
# that a missing input yields a cannot-determine and never a silent zero.
CANNOT_DETERMINE = "cannot_determine"


# The four buckets `compute_co2e` drops an activity into when it could not be computed.
# Every one of them means the run's total covers less than the run's own period.
_EXCLUSION_BUCKETS = ("unmapped", "unit_errors", "data_errors", "gwp_mismatch")


def _excluded_lines(run: CalculationRun) -> int:
    """How many of a run's activities landed in an exclusion bucket."""
    return sum(int(getattr(run, b, 0) or 0) for b in _EXCLUSION_BUCKETS)


def _exclusion_summary(run: CalculationRun) -> str:
    """Which buckets, with counts — so a refusal says what to go and fix."""
    return ", ".join(f"{b}={int(getattr(run, b, 0) or 0)}" for b in _EXCLUSION_BUCKETS
                     if int(getattr(run, b, 0) or 0))


def _run_descriptor(db: Session, run: CalculationRun) -> dict:
    """The identity of a run in a comparative — enough for a reader to check the pair.

    Carries the dimensions the gates test on (period, GWP vintage, consolidation
    approach, pedigree compilation) so a reader can see WHY a pair was comparable or
    refused, rather than taking the verdict on trust.
    """
    return {
        "run_id": run.id,
        "created_at": run.created_at,
        "status": run.status,
        "gwp_set": run.gwp_set,
        "consolidation_approach": run.consolidation_approach,
        "pedigree_method": run.pedigree_method,
        "reporting_period": period_info(db, run),
    }


def prior_period_candidates(db: Session, organisation_id: int,
                            run: CalculationRun) -> list:
    """Completed runs of this organisation scoped to a DIFFERENT reporting period.

    "A different period" is the whole test, and it is deliberately not "an earlier run":
    a preparer re-runs the same period many times while resolving mappings, and offering
    those as comparatives would invite subtracting a period from itself — a zero that
    reads as "no change" (see `_same_period_as_current`).

    An unscoped run is never a candidate: it has no period to be prior TO, and
    `period_comparable` would refuse it anyway. Reported so a renderer can say "a prior
    period is on file and you did not compare against it" instead of staying silent.

    PRIOR means prior: a period that ends before this one starts. A later period is a
    pair `_pairing_problem` refuses, so offering one here would name as a remedy
    something the builder goes on to reject.
    """
    if run is None or not run.reporting_period_id:
        # Nothing can be "prior" to a run that covers no stated window. The absence of
        # candidates here is a consequence of the current run being unscoped, which is
        # itself gated; it is not a claim that the organisation has no history.
        return []
    rows = db.query(CalculationRun).filter(
        CalculationRun.organisation_id == organisation_id,
        CalculationRun.id != run.id,
        CalculationRun.status == "complete",
        CalculationRun.reporting_period_id.isnot(None),
        CalculationRun.reporting_period_id != run.reporting_period_id,
    ).order_by(CalculationRun.id.desc()).all()
    # The same standard the builder applies below, for the same reason: a candidate it
    # would go on to refuse is not a remedy, it is a round trip.
    #
    # The date check is explicit rather than inherited from `_pairing_problem`, which
    # returns None — "no problem" — when it cannot PARSE the two periods, because
    # ordering is then `period_comparable`'s answer to give. That is correct inside the
    # builder, where `cross_run_gates` runs immediately afterwards and catches it. Here
    # there is no afterwards: a run whose period dates are not ISO would be published as
    # an available comparative on the strength of a check that could not read it, which
    # is the same thing as a figure's absence passing a gate.
    return [_run_descriptor(db, r) for r in rows
            if _pairing_problem(db, r, run) is None and not _excluded_lines(r)
            and period_days(run_period(db, r)) is not None]


def _pairing_problem(db: Session, prior: CalculationRun,
                     run: CalculationRun) -> Optional[str]:
    """A reason this pair is not a (current, prior) pair at all, or None.

    NOT a re-derivation of any gate in `cross_run_gates`, and deliberately kept here
    rather than pushed into it: that function answers "can these two totals be
    subtracted?", which is SYMMETRIC — swap the arguments and every one of its five
    answers is unchanged. A prior-period comparative is DIRECTIONAL, because it
    publishes `current - prior` and calls the result a year-on-year change. Three things
    can therefore be wrong about a pair that every symmetric gate accepts:

    * The two runs are the same run. The delta is zero, and a zero delta is not "no
      comparative available" — it is a published claim that emissions did not change.
    * The two runs share one reporting period. Same zero, same false claim; their
      difference measures recomputation, not a year.
    * The "comparative" is the LATER period. Both totals are right, both windows are
      the same length, every gate passes — and the sign is backwards, so a rise is
      published as a fall. This is the one that survives every other check.
    """
    if prior.id == run.id:
        return ("the comparative run and the reporting run are the same run "
                f"(#{run.id}) — subtracting a run from itself yields a zero that would "
                f"be published as 'no change year on year'")
    if (prior.reporting_period_id is not None
            and prior.reporting_period_id == run.reporting_period_id):
        return (f"the comparative run (#{prior.id}) and the reporting run (#{run.id}) are "
                f"scoped to the SAME reporting period "
                f"(id {run.reporting_period_id}) — the two cover one window, not a "
                f"current and a prior one, so their difference measures recomputation "
                f"rather than a year-on-year change")
    p_prior, p_run = run_period(db, prior), run_period(db, run)
    if p_prior is None or p_run is None:
        return None            # an unscoped run is `period_comparable`'s answer to give
    a_end = _parse_iso_date(p_prior.end_date or "")
    b_start = _parse_iso_date(p_run.start_date or "")
    if a_end is None or b_start is None:
        return None            # non-ISO dates are `period_comparable`'s answer to give
    if a_end >= b_start:
        return (f"the comparative period {p_prior.label!r} "
                f"({p_prior.start_date}..{p_prior.end_date}) does not end before the "
                f"reporting period {p_run.label!r} "
                f"({p_run.start_date}..{p_run.end_date}) begins — it is not a PRIOR "
                f"period. The change is published as current minus comparative, so a "
                f"later or overlapping period reports a rise as a fall, with both "
                f"totals correct and every comparability gate passed")
    return None


def _delta(current: Optional[float], prior: Optional[float]) -> dict:
    """One figure's current, prior and change — or a cannot-determine for each part.

    A None input is never coerced to 0.0: a figure the payload declines to state (a
    refused Cat 15 total, an unassessable energy carrier) has no delta, and publishing
    one computed from a substituted zero would report the refusal as a movement.
    """
    out = {"current": current, "prior": prior}
    if current is None or prior is None:
        out["change"] = None
        out["change_pct"] = None
        out["status"] = CANNOT_DETERMINE
        _missing = [n for n, v in (("current", current), ("prior", prior)) if v is None]
        out["reason"] = (f"not stated for the {' and '.join(_missing)} run — a change "
                         f"computed against a substituted zero would report a figure the "
                         f"report declines to state as a movement")
        return out
    out["change"] = current - prior
    # A percentage change off a zero base is undefined, not infinite and not 100%.
    # Both halves are stated above, so the reader loses nothing by its absence.
    if prior == 0:
        out["change_pct"] = None
        out["pct_reason"] = ("prior figure is zero — a percentage change off a zero base "
                             "is undefined; the absolute change is stated instead")
    else:
        out["change_pct"] = round(100.0 * (current - prior) / prior, 4)
    out["status"] = "computed"
    return out


def prior_period_comparative(db: Session, organisation_id: int, run: CalculationRun,
                             comparative_run_id: Optional[int], *,
                             extract: Callable[[Session, CalculationRun], dict],
                             clause: str, quantity: str, measures: str,
                             prefix: str = "") -> dict:
    """The prior-period comparative block for one disclosure, and its blockers.

    `extract(db, run) -> {figure_name: value|None}` is applied to BOTH runs, so each
    figure is only ever compared with itself (property 2 in the module docstring).

    `clause` names the requirement being met ("ESRS E1-6 comparative"); `quantity` and
    `measures` are passed through to the shared gates so a blocker reads as this
    renderer's own sentence — "the ESRS E1-6 comparative ... measure a year-on-year
    change" — rather than a generic one.

    Returns ``{"comparative": <block>, "blockers": [...]}``. The block is ALWAYS present
    and always says what it knows; `blockers` is what the renderer must fold into its own
    fail-closed gate list.
    """
    candidates = prior_period_candidates(db, organisation_id, run)

    def _cannot(reason_code: str, reason: str, *, blockers: list,
                prior: Optional[dict] = None) -> dict:
        return {
            "comparative": {
                "clause": clause,
                "status": CANNOT_DETERMINE,
                "reason_code": reason_code,
                "reason": reason,
                # Exactly what this comparative contributes to the renderer's own
                # fail-closed list — so a reader of the block and a reader of
                # `blockers` at the top of the payload never see different sets.
                # `status` and `reason_code` carry the meaning; this list does not
                # also encode it.
                "blockers": list(blockers),
                "reporting_run": _run_descriptor(db, run),
                "comparative_run": prior,
                "figures": None,
                "prior_periods_on_file": candidates,
                "note": "A comparative that cannot be computed is reported as "
                        "cannot_determine with its reasons named — never as a blank or a "
                        "zero, because a zero delta is a published claim that nothing "
                        "changed.",
            },
            "blockers": blockers,
        }

    if comparative_run_id is None:
        # Not an error in itself: a first-year filing legitimately has no prior period.
        # But if one IS on file, say so plainly and name it — that is the second-year
        # filer whose statement would otherwise omit a mandatory comparative in silence.
        if candidates:
            _ids = ", ".join(f"#{c['run_id']}" for c in candidates[:5])
            return _cannot(
                "no_prior_run_requested",
                f"no comparative_run_id was supplied, but this organisation has "
                f"{len(candidates)} completed run(s) scoped to a different reporting "
                f"period ({_ids}) — {clause} requires the prior period's figures, so "
                f"supply one of these as comparative_run_id. Omitting it publishes a "
                f"single-period statement where a comparative is mandatory.",
                blockers=[f"{prefix}{clause}: a prior reporting period is on file "
                          f"({_ids}) but no comparative_run_id was supplied — the "
                          f"comparative this framework requires is absent, not zero"])
        return _cannot(
            "no_prior_period_on_file",
            f"no comparative_run_id was supplied and this organisation has no completed "
            f"run scoped to a different reporting period — there is no prior period to "
            f"compare against. A first reporting period legitimately has no comparative; "
            f"{clause} is satisfied by stating that, not by a zero.",
            blockers=[])

    prior = db.query(CalculationRun).filter(
        CalculationRun.id == comparative_run_id,
        CalculationRun.organisation_id == organisation_id).first()
    if prior is None:
        return _cannot(
            "prior_run_not_found",
            f"comparative_run_id {comparative_run_id} is not a run of this organisation",
            blockers=[f"{prefix}{clause}: comparative_run_id {comparative_run_id} not "
                      f"found for this organisation"])

    _excluded = _excluded_lines(prior)
    if prior.status != "complete" or _excluded:
        # TWO conditions, because either alone fails open.
        #
        # The status alone is not enough: a run that excluded EVERY line — the real
        # case is an AR5 factor file computed on the AR6 default, where each line is
        # dropped on vintage — reports total_co2e 0.0, and has historically called
        # itself "complete". Subtracting that publishes the whole of the current year
        # as an increase from nothing, with every comparability gate passing, because
        # 0.0 is a number like any other.
        #
        # The counters alone are not enough either: a 'pending' run has no exclusions
        # yet because it has not finished computing.
        _why = (f"has status {prior.status!r}" if prior.status != "complete"
                else "reports status 'complete'")
        _detail = (f", and {_excluded} of its {prior.total_activities} activities were "
                   f"EXCLUDED ({_exclusion_summary(prior)}), so its total is a partial "
                   f"figure" if _excluded else "")
        return _cannot(
            "prior_run_incomplete",
            f"comparative run #{prior.id} {_why}{_detail} — a total that does not cover "
            f"the whole of its own period is not a figure this year's can be subtracted "
            f"from: the difference would report the missing lines as a change",
            blockers=[f"{prefix}{clause}: comparative run #{prior.id} is not a complete "
                      f"inventory ({_why}{_detail})"],
            prior=_run_descriptor(db, prior))

    _pairing = _pairing_problem(db, prior, run)
    if _pairing:
        return _cannot("not_a_prior_period", _pairing,
                       blockers=[f"{prefix}{clause}: {_pairing}"],
                       prior=_run_descriptor(db, prior))

    # THE one question. Every reason the two runs cannot be subtracted, in one call —
    # never a set assembled here. `label_a`/`label_b` make each blocker read in this
    # renderer's voice.
    gate_reasons = cross_run_gates(
        db, prior, run, label_a="comparative", label_b="reporting",
        quantity=quantity, measures=measures, prefix=f"{prefix}{clause}: ")

    # The period block is published whether or not the gates passed: a reader of a
    # COMPARABLE delta needs both windows just as much as a reader of a refused one.
    periods = period_payload(
        db, prior, run, key_a="comparative", key_b="reporting",
        note=f"{clause} states the prior period beside the current one. The difference "
             f"is only a year-on-year change if the two runs cover comparable lengths of "
             f"time; a 12-month prior period minus a 3-month current one reports the "
             f"missing nine months as a 75% fall, with both totals correct for what they "
             f"cover. Both windows are stated so the comparison can be checked.")

    if gate_reasons:
        out = _cannot(
            "not_comparable",
            f"a prior run (#{prior.id}) was supplied but the two runs cannot be "
            f"subtracted — {len(gate_reasons)} comparability gate(s) refused it. The "
            f"figures are NOT differenced; each reason is named so the preparer can "
            f"re-scope or re-run rather than publish an artefact as a change.",
            blockers=list(gate_reasons), prior=_run_descriptor(db, prior))
        out["comparative"]["period_comparability"] = periods
        return out

    cur_figs = extract(db, run)
    prior_figs = extract(db, prior)
    # The extractor is one function applied twice, so the two key sets agree by
    # construction. Asserting it anyway: a caller that made `extract` depend on the run
    # (a per-run early return, say) would otherwise produce a comparative whose figures
    # silently differ in which quantities they cover.
    if set(cur_figs) != set(prior_figs):
        _diff = sorted(set(cur_figs) ^ set(prior_figs))
        return _cannot(
            "extractor_disagreed",
            f"the figure extractor returned different quantities for the two runs "
            f"({_diff}) — a comparative must compare like with like, so no difference is "
            f"published",
            blockers=[f"{prefix}{clause}: comparative figure sets disagree ({_diff})"],
            prior=_run_descriptor(db, prior))

    figures = {name: _delta(cur_figs[name], prior_figs[name]) for name in sorted(cur_figs)}
    return {
        "comparative": {
            "clause": clause,
            "status": "computed",
            "reason_code": None,
            # The gates RAN and returned nothing. Distinct from the None above, which
            # means there was no pair to ask about.
            "blockers": [],
            "reporting_run": _run_descriptor(db, run),
            "comparative_run": _run_descriptor(db, prior),
            "period_comparability": periods,
            "figures": figures,
            "prior_periods_on_file": candidates,
            "note": "Exact difference between two immutable runs; `change` is current "
                    "minus prior, so a NEGATIVE change is a fall in emissions. Published "
                    "only after services.comparability.cross_run_gates returned no "
                    "reason the two runs cannot be subtracted — read `period_comparability` "
                    "to see the two windows the difference spans.",
        },
        "blockers": [],
    }


def comparative_derivations(block: dict, *, unit: str, figures: tuple) -> list:
    """Worked differences for the comparative figures a report leads with.

    A refused comparative gets `D.blocked` rather than no derivation at all: the reason
    the arithmetic is not shown belongs beside the figures, where a reader deciding
    whether to rely on them will see it.
    """
    if block.get("status") != "computed":
        return [D.blocked(f"Comparative — {block.get('clause')}",
                          str(block.get("reason") or "comparative not available"),
                          unit=unit)]
    out = []
    for name, label in figures:
        f = (block.get("figures") or {}).get(name)
        if not f:
            continue
        if f.get("status") != "computed":
            out.append(D.blocked(f"Change in {label}", str(f.get("reason")), unit=unit))
            continue
        out.append(D.difference(
            f"Change in {label}",
            [("Reporting period", f["current"]), ("Comparative period", f["prior"])],
            unit=unit, stated_value=f["change"],
            note="Current minus prior, between two immutable runs the shared "
                 "comparability gates accepted; a negative value is a fall."))
    return out
