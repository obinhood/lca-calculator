"""Reading frozen run state without letting one bad row take down a report.

Every renderer in this platform reads the JSON blobs frozen onto a run — line
`details`, `run.notes`, declaration `criteria`. Until now each one called
`json.loads` bare, so a SINGLE malformed blob raised, and the exception took out
`/results/summary` and every framework renderer built on it. Not the affected
line: the whole organisation's reporting, for one corrupt row.

The parsers here fail soft so nothing dies, and `corrupt_details` exists so the
corruption is still VISIBLE. That pairing is the point. A parser that quietly
returned {} would trade a loud failure for a silent one — a line whose factor id
could not be read would simply stop appearing in the factor register, and no
reader would be told. Failing soft is only defensible when something else is
counting.

These are for reading state the engine itself wrote. They are NOT an input
boundary: user-supplied JSON should still be validated and rejected, not coerced.
"""
import json
from typing import Optional

from sqlalchemy.orm import Session


def parse_detail(raw) -> dict:
    """A frozen detail blob as a dict; anything unusable becomes {}.

    Also coerces non-object JSON: `[]` and `null` are valid JSON but not a detail
    record, and returning them hands a list to callers doing `.get()`.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_list(raw) -> list:
    """A frozen list blob (``run.notes``) as a list; anything unusable becomes []."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def parse_optional(raw) -> Optional[dict]:
    """A frozen blob that is legitimately absent, e.g. a readiness snapshot.

    None means "not present"; {} would mean "present and empty", and the two carry
    different meanings to every caller that tests them.
    """
    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def corrupt_details(db: Session, run_id: int) -> dict:
    """How many of a run's frozen line details cannot be parsed.

    The counterpart to failing soft. Reports keep rendering, and this says how much
    of the underlying record could not be read — so "the pack rendered" never gets
    mistaken for "every line was legible".
    """
    from ..models import EmissionLineItem
    rows = db.query(EmissionLineItem.id, EmissionLineItem.details).filter(
        EmissionLineItem.run_id == run_id).all()
    bad = [lid for lid, raw in rows if raw and not parse_detail(raw)]
    return {
        "lines_total": len(rows),
        "lines_unreadable": len(bad),
        "line_ids": sorted(bad)[:100],
        "clean": not bad,
        "note": None if not bad else (
            f"{len(bad)} of {len(rows)} line detail blobs could not be parsed. Those "
            f"lines still carry their co2e — the totals are unaffected — but their "
            f"factor lineage, GWP set and data quality cannot be read, so they are "
            f"absent from the factor register, the pedigree score and the "
            f"uncertainty propagation."),
    }
