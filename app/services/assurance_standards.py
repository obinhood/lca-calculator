"""Which assurance standard applies to a reporting period, and from when.

ISAE 3410 (Assurance Engagements on Greenhouse Gas Statements) has been withdrawn
by the IAASB with effect from 15 December 2026, superseded by ISSA 5000 (General
Requirements for Sustainability Assurance Engagements). ISSA 5000 is effective for
engagements on sustainability information reported for PERIODS BEGINNING ON OR
AFTER that date — or as at a specific date on or after it — with early application
permitted.

THE RULE THAT MATTERS: applicability turns on the PERIOD BEING ASSURED, never on
today's date. An FY2025 inventory assured in 2028 was still an ISAE 3410
engagement; the standard in force for a period is the one that governs it. Keying
this off `date.today()` would silently restate the applicable standard of every
historical engagement the moment the clock passed 15 December 2026 — the same
live-vs-frozen defect the report renderers exist to prevent.

Consequently there is no "current standard" function here, and deliberately so.
Without a period the answer is `cannot_determine`, and both standards are returned
with their conditions attached rather than one being guessed.

ISO 14064-3 is a separate ISO standard for validation and verification of GHG
statements. It is unaffected by the IAASB withdrawal and carries no sunset.
"""
from typing import Optional

# IAASB: ISSA 5000 effective for periods beginning on or after this date; ISAE 3410
# withdrawn with effect from the same date.
ISSA_5000_EFFECTIVE_FROM = "2026-12-15"

ASSURANCE_STANDARDS = {
    "ISAE_3410": {
        "name": "ISAE 3410 (Assurance Engagements on Greenhouse Gas Statements)",
        "authority": "IAASB",
        "withdrawn_from": ISSA_5000_EFFECTIVE_FROM,
        "effective_from": None,
        "superseded_by": "ISSA_5000",
        "early_application": False,
    },
    "ISSA_5000": {
        "name": "ISSA 5000 (General Requirements for Sustainability Assurance Engagements)",
        "authority": "IAASB",
        "withdrawn_from": None,
        "effective_from": ISSA_5000_EFFECTIVE_FROM,
        "supersedes": "ISAE_3410",
        # The IAASB permits applying ISSA 5000 to earlier periods, so an early
        # period is never a reason to REFUSE it — only ISAE 3410 has a hard stop.
        "early_application": True,
    },
    "ISO_14064_3": {
        "name": "ISO 14064-3 (validation and verification of GHG statements)",
        "authority": "ISO",
        "withdrawn_from": None,
        "effective_from": None,
        "early_application": False,
    },
}

VALID_STANDARDS = tuple(ASSURANCE_STANDARDS)


def _is_on_or_after(date_str: Optional[str], boundary: str) -> Optional[bool]:
    """ISO date comparison; None when the input is absent or unparseable.

    Plain string comparison is correct and sufficient for zero-padded ISO dates,
    which is what ReportingPeriod stores, and it avoids importing a date parser
    that would accept ambiguous formats this module must not guess at.
    """
    if not date_str or len(date_str) < 10:
        return None
    head = date_str[:10]
    if head[4] != "-" or head[7] != "-" or not head.replace("-", "").isdigit():
        return None
    return head >= boundary


def applicable_standards(period_start: Optional[str]) -> dict:
    """Which IAASB standard governs an engagement over a period starting when.

    `determinable` is False when no usable period start was supplied. That is a
    real answer, not a failure: both standards are still returned with the
    condition that selects them, so a reader can resolve it once the period is
    known — and nothing is guessed on their behalf.
    """
    on_or_after = _is_on_or_after(period_start, ISSA_5000_EFFECTIVE_FROM)

    if on_or_after is None:
        return {
            "determinable": False,
            "period_start": period_start,
            "issa_5000_effective_from": ISSA_5000_EFFECTIVE_FROM,
            "reason": "no usable reporting-period start date; the applicable IAASB "
                      "standard is determined by the period being assured, never by "
                      "today's date, so it cannot be resolved without one.",
            "conditional": {
                "ISAE_3410": f"periods beginning before {ISSA_5000_EFFECTIVE_FROM}",
                "ISSA_5000": f"periods beginning on or after {ISSA_5000_EFFECTIVE_FROM}"
                             " (early application permitted for earlier periods)",
            },
            # ISO 14064-3 has no sunset, so it is applicable whatever the period.
            "applicable": ["ISO_14064_3"],
            "withdrawn": [],
        }

    if on_or_after:
        return {
            "determinable": True,
            "period_start": period_start,
            "issa_5000_effective_from": ISSA_5000_EFFECTIVE_FROM,
            "reason": f"period begins on or after {ISSA_5000_EFFECTIVE_FROM}: ISSA 5000 "
                      f"is effective and ISAE 3410 is withdrawn.",
            "applicable": ["ISSA_5000", "ISO_14064_3"],
            "withdrawn": ["ISAE_3410"],
        }

    return {
        "determinable": True,
        "period_start": period_start,
        "issa_5000_effective_from": ISSA_5000_EFFECTIVE_FROM,
        "reason": f"period begins before {ISSA_5000_EFFECTIVE_FROM}: ISAE 3410 was the "
                  f"standard in force for it. ISSA 5000 may be applied early.",
        "applicable": ["ISAE_3410", "ISO_14064_3", "ISSA_5000"],
        "withdrawn": [],
    }


def standard_permitted(standard: str, period_start: Optional[str]) -> dict:
    """May `standard` be used for an engagement over a period starting when?

    Refuses only what is actually impossible: ISAE 3410 over a period beginning on
    or after the withdrawal date. An unknown period is NOT a refusal — it is
    permitted with a warning, because refusing every engagement whose run carries
    no reporting period would break the far more common legitimate case.
    """
    if standard not in ASSURANCE_STANDARDS:
        return {"permitted": False, "reason": f"unknown standard {standard!r}; "
                                              f"expected one of {list(VALID_STANDARDS)}",
                "warning": None}

    spec = ASSURANCE_STANDARDS[standard]
    withdrawn_from = spec.get("withdrawn_from")
    if not withdrawn_from:
        return {"permitted": True, "reason": None, "warning": None}

    on_or_after = _is_on_or_after(period_start, withdrawn_from)
    if on_or_after is None:
        return {
            "permitted": True, "reason": None,
            "warning": f"{standard} was withdrawn with effect from {withdrawn_from} "
                       f"(superseded by {spec.get('superseded_by')}). This run carries no "
                       f"usable reporting-period start date, so applicability could not "
                       f"be checked — confirm the period begins before {withdrawn_from}.",
        }
    if on_or_after:
        return {
            "permitted": False,
            "reason": f"{standard} was withdrawn with effect from {withdrawn_from} and "
                      f"cannot govern a period beginning on or after it "
                      f"(period starts {period_start}). Use "
                      f"{spec.get('superseded_by')} instead.",
            "warning": None,
        }
    return {"permitted": True, "reason": None, "warning": None}


def run_period_start(db, run) -> Optional[str]:
    """The assured period's start date, read from the run's frozen period link.

    Returns None when the run is not period-scoped — which callers must treat as
    "unknown", never as "before the cutoff".
    """
    from ..models import ReportingPeriod
    if run is None or getattr(run, "reporting_period_id", None) is None:
        return None
    period = db.query(ReportingPeriod).filter(
        ReportingPeriod.id == run.reporting_period_id).first()
    return getattr(period, "start_date", None) if period else None
