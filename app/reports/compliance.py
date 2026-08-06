"""Evaluate a report against its framework's required-data checklist.

Answers two questions a preparer actually has:

  1. DOES THIS REPORT CARRY EVERY REQUIRED FIELD?  Each requirement in the registry is
     resolved against the real payload, so "present" is verified, not asserted. Requirements
     the engine cannot produce (narrative, governance, assurance) are listed as outstanding
     rather than quietly dropped.

  2. WHERE A REQUIREMENT HAS A NUMBER, HOW FAR OFF ARE WE?  Thresholds carry the value the
     standard expects, the value achieved, and the GAP between them — "coverage 87.0%,
     needs 100%, short by 13.0 points" — so a shortfall is measurable rather than a yes/no.

`data_complete` means every required DATA field the platform owns is present. That is a
completeness statement about data, NOT a compliance opinion: filing also needs the preparer
narrative and, for most frameworks, independent assurance. Both are reported alongside.
"""
from typing import Any, Dict, List, Optional

from .compliance_requirements import REQUIREMENTS

# ---------------------------------------------------------------------------------------
# Numeric / categorical thresholds: what the standard expects vs what the run achieved.
#
# comparator: ">=" (actual must reach required) | "<=" (must not exceed) | "==" (must equal).
# `path` is resolved against the report payload; when a value is missing the threshold is
# reported as "not assessable" rather than silently passing.

_COVERAGE = {"metric": "Activity-data coverage", "path": "coverage.coverage_pct",
             "required": 100.0, "comparator": ">=", "unit": "%",
             "note": "every uploaded activity must be mapped and computed"}
_DQ = {"metric": "Data-quality score", "path": "data_quality.emissions_weighted_score",
       "required": 3.0, "comparator": "<=", "unit": "(1 best – 5 worst)",
       "note": "scores worse than 3 indicate proxy-heavy data an assurer will challenge"}
_AR6 = {"metric": "GWP vintage", "path": "run.gwp_set", "required": "AR6", "comparator": "==",
        "unit": "", "note": "the standard expects the latest IPCC GWP values"}

THRESHOLDS: Dict[str, List[dict]] = {
    "secr": [_COVERAGE],
    "esrs_e1": [_COVERAGE, _AR6, _DQ,
                {"metric": "Scope 3 categories screened", "compute": "scope3_screened",
                 "required": 15, "comparator": ">=", "unit": "of 15 categories",
                 "note": "ESRS AR 46(i): all 15 GHG Protocol categories must be screened"}],
    "issb_s2": [_COVERAGE, _AR6, _DQ],
    "gri": [_COVERAGE],
    "cdp": [_COVERAGE],
    "sb253": [_COVERAGE, _AR6],
    "tcfd": [_COVERAGE],
    "scope3_inventory": [
        {"metric": "Scope 3 categories declared", "compute": "scope3_screened",
         "required": 15, "comparator": ">=", "unit": "of 15 categories",
         "note": "every category must be quantified or excluded with a justification"}],
    "esos": [_COVERAGE,
             {"metric": "Significant-consumption coverage", "path": "significant_energy_use_pct",
              "required": 95.0, "comparator": ">=", "unit": "%",
              "note": "ESOS requires audits covering 95% of total energy consumption"}],
    "neutrality": [_COVERAGE],
    "assurance_readiness": [_COVERAGE, _DQ],
    "ets_mrv": [_COVERAGE],
    "pcaf": [{"metric": "Weighted data-quality score", "path": "weighted_data_quality_score",
              "required": 3.0, "comparator": "<=", "unit": "(1 best – 5 proxy)",
              "note": "PCAF expects disclosure of the score mix; proxy-heavy portfolios score worse"}],
    "sbti": [{"metric": "Implied annual reduction rate",
              "path": "ambition_assessment.implied_annual_linear_rate",
              "required": 0.042, "comparator": ">=", "unit": "/yr (1.5°C floor 4.2%)",
              "note": "SBTi near-term 1.5°C-aligned targets need at least 4.2%/yr linear reduction"}],
    "iso_14064_2": [
        {"metric": "Leakage quantified", "path": "quantification_tco2e.leakage",
         "required": 0.0, "comparator": ">=", "unit": "tCO2e",
         "note": "leakage must be assessed; 0 is acceptable only as an assessed result"}],
    "epd": [{"metric": "Impact categories covered", "path": None, "required": 1, "comparator": ">=",
             "unit": "of the EN 15804+A2 core set", "actual_override": 1,
             "note": "the platform produces the GWP-fossil indicator only"}],
    "pef": [{"metric": "Impact categories covered", "path": None, "required": 16, "comparator": ">=",
             "unit": "of 16 EF 3.1 categories", "actual_override": 1,
             "note": "a full PEF profile characterises all 16 categories"}],
}


def _resolve(payload: Any, path: Optional[str]) -> Any:
    """Walk a dotted path, tolerating keys that THEMSELVES contain dots.

    Report payloads use citation-style keys such as `C6.1_scope1_gross_tco2e`, so a naive
    split(".") would never find them. At each hop we try the longest remaining segment first
    and shorten until a key matches.
    """
    if not path:
        return None
    cur, rest = payload, path
    while rest:
        if not isinstance(cur, dict):
            return None
        parts = rest.split(".")
        for take in range(len(parts), 0, -1):        # longest match first
            candidate = ".".join(parts[:take])
            if candidate in cur:
                cur = cur[candidate]
                rest = ".".join(parts[take:])
                break
        else:
            return None
    return cur


def _present(v: Any) -> bool:
    """A requirement counts as met only if the field carries a real value."""
    if v is None or v is False:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True


def _count_scope3_screened(payload: dict) -> Optional[int]:
    """How many of the 15 Scope 3 categories have been screened (i.e. not left undeclared)."""
    for path in ("e1_6_scope3_screening", "scope3.completeness"):
        block = _resolve(payload, path)
        if isinstance(block, dict):
            undeclared = block.get("undeclared")
            if isinstance(undeclared, (list, tuple)):
                return 15 - len(undeclared)
            by_status = block.get("by_status")
            if isinstance(by_status, dict):
                return sum(len(v) if isinstance(v, (list, tuple)) else int(v or 0)
                           for k, v in by_status.items() if k != "undeclared")
    return None


COMPUTED = {"scope3_screened": _count_scope3_screened}


def _evaluate_threshold(payload: dict, spec: dict) -> dict:
    required, comparator = spec["required"], spec["comparator"]
    actual = spec.get("actual_override")
    if actual is None and spec.get("compute"):
        actual = COMPUTED[spec["compute"]](payload)
    if actual is None:
        actual = _resolve(payload, spec.get("path"))

    row = {"metric": spec["metric"], "required": required, "actual": actual,
           "unit": spec.get("unit", ""), "comparator": comparator, "note": spec.get("note")}

    if actual is None:
        row.update(status="not_assessable", gap=None,
                   explanation="not present in this report — cannot be checked")
        return row

    if comparator == "==":
        ok = str(actual) == str(required)
        row.update(status="met" if ok else "short", gap=None,
                   explanation="matches the required value" if ok
                   else f"is {actual!r}; the standard expects {required!r}")
        return row

    try:
        a, r = float(actual), float(required)
    except (TypeError, ValueError):
        row.update(status="not_assessable", gap=None,
                   explanation="value is not numeric — cannot be compared")
        return row

    if comparator == ">=":
        ok, gap = a + 1e-9 >= r, max(0.0, r - a)
        worse = f"short by {round(gap, 6)} {spec.get('unit', '')}".strip()
    else:                                            # "<="
        ok, gap = a <= r + 1e-9, max(0.0, a - r)
        worse = f"over by {round(gap, 6)} {spec.get('unit', '')}".strip()

    row.update(status="met" if ok else "short", gap=round(gap, 6),
               explanation=("meets the threshold" if ok else worse))
    return row


def evaluate(framework_key: str, payload: dict) -> dict:
    """Required-data checklist + threshold gaps for one report."""
    reqs = REQUIREMENTS.get(framework_key)
    if reqs is None:
        return {"assessable": False,
                "note": f"no required-data checklist is defined for {framework_key!r}"}

    fields, satisfied, missing = [], 0, 0
    preparer, assurance = 0, 0
    for r in reqs:
        row = {"ref": r["ref"], "requirement": r["what"], "source": r["source"]}
        if r["source"] == "platform":
            ok = _present(_resolve(payload, r.get("path")))
            row["status"] = "present" if ok else "missing"
            row["field"] = r.get("path")
            satisfied += ok
            missing += (not ok)
        elif r["source"] == "assurance":
            row["status"] = "requires_independent_assurance"
            assurance += 1
        else:
            row["status"] = "preparer_must_supply"
            preparer += 1
        fields.append(row)

    platform_total = satisfied + missing
    pct = round(100.0 * satisfied / platform_total, 1) if platform_total else 100.0

    thresholds = [_evaluate_threshold(payload, s) for s in THRESHOLDS.get(framework_key, [])]
    short = [t for t in thresholds if t["status"] == "short"]

    data_complete = missing == 0 and not short
    return {
        "assessable": True,
        "framework_key": framework_key,
        # Data completeness — the part the ENGINE can actually certify.
        "data_complete": data_complete,
        "data_completeness_pct": pct,
        "required_data_fields": platform_total,
        "data_fields_present": satisfied,
        "data_fields_missing": missing,
        "thresholds_short": len(short),
        # Everything the engine CANNOT supply, counted rather than hidden.
        "preparer_items_outstanding": preparer,
        "assurance_items_outstanding": assurance,
        "requirements": fields,
        "thresholds": thresholds,
        "verdict": _verdict(data_complete, pct, missing, short, preparer, assurance),
        "scope_note": (
            "This is a DATA-COMPLETENESS check against the framework's required disclosures, "
            "not a compliance opinion. Filing additionally requires the preparer-supplied "
            "narrative items above and, where the framework mandates it, independent "
            "assurance — neither of which any calculation engine can provide."),
    }


def _verdict(data_complete, pct, missing, short, preparer, assurance) -> str:
    """One sentence a preparer can act on: what's missing, and by how much."""
    if data_complete:
        head = "All required data fields are present and every threshold is met."
    elif missing and short:
        head = (f"{missing} required data field{'s are' if missing != 1 else ' is'} missing "
                f"({pct}% present), and " + "; ".join(t["metric"] + " " + t["explanation"]
                                                      for t in short) + ".")
    elif missing:
        head = (f"{missing} required data field{'s are' if missing != 1 else ' is'} missing "
                f"({pct}% of required fields present).")
    else:
        head = ("All required data fields are present, but "
                + "; ".join(t["metric"] + " " + t["explanation"] for t in short) + ".")
    tail = []
    if preparer:
        tail.append(f"{preparer} narrative/governance item{'s' if preparer != 1 else ''} "
                    f"must be supplied by the preparer")
    if assurance:
        tail.append(f"{assurance} item{'s' if assurance != 1 else ''} "
                    f"{'require' if assurance != 1 else 'requires'} independent assurance")
    return head + (" " + "; ".join(tail) + "." if tail else "")
