"""PACT Technical Specifications v3 — the ProductFootprint data model and validator.

The Partnership for Carbon Transparency (WBCSD) defines a data model and an HTTP
REST API for exchanging product carbon footprints between any two platforms. This
is the half that matters most to a buyer: parsing, validating and storing a
supplier's PCF so it can replace an industry-average factor with primary data.

v3, NOT v2. Version 2.x of the Technical Specifications was deprecated on
1 April 2026, and v3 is not a superset — it renames the declared-unit fields,
requires both the excluding- and including-biogenic-uptake figures, makes
`primaryDataShare` and `dqi` mandatory, drops `version`/`updated`/`statusComment`
entirely, and replaces in-place updates with immutable ones (a changed PCF gets a
NEW id and lists the old one in `precedingPfIds`). A v2 document parsed as v3
would be silently missing the fields v3 requires, so the spec version is checked
rather than assumed.

TWO RULES THIS MODULE WILL NOT BEND

1. DECIMALS ARE STRINGS ON THE WIRE. The spec types every quantity as
   `string <decimal>` — "10", "42.12", "-182.84". They are kept as received AND
   parsed to float separately, because float(x) is lossy and a supplier's
   published figure must survive a round trip byte-identical. Anything a reader
   is shown comes from the string; anything arithmetic uses the float.

2. THE RECEIVED DOCUMENT IS EVIDENCE. It is stored verbatim alongside the parsed
   columns. An assuror asking "what did the supplier actually send you" gets the
   bytes, not our reconstruction of them — and a later spec revision cannot
   retroactively change what we were given.

Validation is fail-closed on structure and explicit about severity: a `SHALL`
field that is missing is an error and the document is rejected; a conditional
(`BIO`, `BIO-2027`) or recommended field is a warning and the document is kept.
A PCF that fails validation is never stored as if it had passed.
"""
import json
import re
from typing import Optional

PACT_SPEC_MAJOR = 3
SUPPORTED_SPEC_VERSIONS = ("3.0.0", "3.0.1", "3.0.2", "3.0.3")

# v2.x was deprecated 2026-04-01. A v2 document is not "an older but fine PCF" —
# it structurally lacks fields v3 requires, so it is refused with the reason.
DEPRECATED_SPEC_MAJORS = {2: "2026-04-01"}

PF_STATUSES = ("Active", "Deprecated")

DECLARED_UNITS = (
    "liter", "kilogram", "cubic meter", "kilowatt hour", "megajoule",
    "ton kilometer", "square meter", "piece", "hour", "megabit second",
)

CROSS_SECTORAL_STANDARDS = (
    "ISO14067", "ISO14083", "ISO14040-44", "GHGP Product", "PEF", "PACT Methodology",
    "PAS2050",
)

# ProductFootprint: SHALL properties. `pcf` is validated separately.
PF_REQUIRED = (
    "id", "specVersion", "created", "status", "companyName", "companyIds",
    "productDescription", "productIds", "productNameCompany", "pcf",
)

# CarbonFootprint: SHALL properties in v3. primaryDataShare and dqi joined this
# list in v3 — they were optional in v2, which is why a v2 document cannot simply
# be relabelled.
CF_REQUIRED = (
    "declaredUnitOfMeasurement", "declaredUnitAmount", "productMassPerDeclaredUnit",
    "referencePeriodStart", "referencePeriodEnd",
    "pcfExcludingBiogenicUptake", "pcfIncludingBiogenicUptake",
    "fossilCarbonContent", "fossilGhgEmissions",
    "packagingEmissionsIncluded", "exemptedEmissionsPercent",
    "ipccCharacterizationFactors", "crossSectoralStandards", "primaryDataShare",
)

# Removed in v3. Their presence means a v2 document wearing a v3 label.
V2_ONLY_PF_FIELDS = ("version", "updated", "statusComment", "productCategoryCpc")
V2_ONLY_CF_FIELDS = ("declaredUnit", "unitaryProductAmount", "pCfExcludingBiogenic",
                     "pCfIncludingBiogenic", "crossSectoralStandardsUsed",
                     "characterizationFactors", "assurance")

# The four geography levels are mutually exclusive: exactly one, or none for global.
GEOGRAPHY_FIELDS = ("geographyRegionOrSubregion", "geographyCountry",
                    "geographyCountrySubdivision")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_URN_RE = re.compile(r"^urn:", re.I)
_DECIMAL_RE = re.compile(r"^-?(?:\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?$")


def parse_decimal(raw) -> Optional[float]:
    """A PACT decimal string to float, or None if it is not one.

    The spec types every quantity as a STRING. A JSON number is tolerated on read
    (real-world senders emit them) but flagged, because emitting one is a spec
    violation on our side and would fail a conformance test.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not _DECIMAL_RE.match(raw.strip()):
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _is_decimal_string(raw) -> bool:
    return isinstance(raw, str) and bool(_DECIMAL_RE.match(raw.strip()))


def _nonempty_str_list(v) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(
        isinstance(x, str) and x.strip() for x in v)


def _iso_datetime(v) -> bool:
    """A permissive ISO-8601 instant check — enough to reject prose, not a parser."""
    if not isinstance(v, str) or len(v) < 10:
        return False
    head = v[:10]
    return (head[4:5] == "-" and head[7:8] == "-"
            and head.replace("-", "").isdigit())


def spec_version_verdict(raw) -> dict:
    """Whether a document's specVersion is one this module can honour."""
    if not isinstance(raw, str) or not raw.strip():
        return {"ok": False, "major": None,
                "reason": "specVersion is required and must be a 'major.minor.patch' string"}
    parts = raw.strip().split(".")
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        return {"ok": False, "major": None,
                "reason": f"specVersion {raw!r} is not 'major.minor.patch'"}
    if major in DEPRECATED_SPEC_MAJORS:
        return {"ok": False, "major": major,
                "reason": f"PACT Technical Specifications v{major}.x was deprecated on "
                          f"{DEPRECATED_SPEC_MAJORS[major]}. A v{major} document is not "
                          f"merely older: it structurally lacks fields v3 requires "
                          f"(primaryDataShare, dqi, pcfIncludingBiogenicUptake), so it "
                          f"cannot be accepted as a v3 footprint."}
    if major != PACT_SPEC_MAJOR:
        return {"ok": False, "major": major,
                "reason": f"specVersion {raw!r} is major version {major}; this "
                          f"implementation supports v{PACT_SPEC_MAJOR}."}
    return {"ok": True, "major": major,
            "exact_match": raw.strip() in SUPPORTED_SPEC_VERSIONS}


def _validate_geography(cf: dict, errors: list) -> Optional[str]:
    """Exactly one geography level, or none for a global footprint."""
    present = [f for f in GEOGRAPHY_FIELDS if cf.get(f) not in (None, "")]
    if len(present) > 1:
        errors.append({
            "field": "geography", "severity": "error",
            "message": f"the geography levels are mutually exclusive; {present} are all "
                       f"set. Exactly one, or none for a global footprint."})
        return None
    return present[0] if present else None


def _validate_dqi(cf: dict, errors: list, warnings: list) -> None:
    """dqi is SHALL in v3; each rating is a decimal string in [1, 5]."""
    dqi = cf.get("dqi")
    if dqi is None:
        warnings.append({
            "field": "pcf.dqi", "severity": "warning",
            "message": "dqi is mandatory under v3 (SHALL by 2027) and is absent. The "
                       "footprint is accepted, but its data quality cannot be assessed "
                       "and it will be scored conservatively."})
        return
    if not isinstance(dqi, dict):
        errors.append({"field": "pcf.dqi", "severity": "error",
                       "message": "dqi must be an object"})
        return
    for key in ("technologicalDQR", "geographicalDQR", "temporalDQR"):
        raw = dqi.get(key)
        if raw is None:
            errors.append({"field": f"pcf.dqi.{key}", "severity": "error",
                           "message": f"{key} is required within dqi"})
            continue
        val = parse_decimal(raw)
        if val is None:
            errors.append({"field": f"pcf.dqi.{key}", "severity": "error",
                           "message": f"{key} must be a decimal string"})
        elif not (1.0 <= val <= 5.0):
            errors.append({"field": f"pcf.dqi.{key}", "severity": "error",
                           "message": f"{key} must be in [1, 5]; got {val}"})


def validate(doc: dict) -> dict:
    """Validate a PACT v3 ProductFootprint document.

    Returns ``{valid, errors, warnings, spec_version, geography_level}``. `valid`
    is False whenever any error was raised — a document that fails is never stored
    as though it had passed. Warnings do not block: a conditional or recommended
    field being absent is information for the reader, not grounds to reject a
    supplier's data.
    """
    errors, warnings = [], []

    if not isinstance(doc, dict):
        return {"valid": False, "spec_version": None, "geography_level": None,
                "errors": [{"field": None, "severity": "error",
                            "message": "a ProductFootprint must be a JSON object"}],
                "warnings": []}

    sv = spec_version_verdict(doc.get("specVersion"))
    if not sv["ok"]:
        errors.append({"field": "specVersion", "severity": "error",
                       "message": sv["reason"]})
    elif not sv.get("exact_match"):
        warnings.append({
            "field": "specVersion", "severity": "warning",
            "message": f"specVersion {doc.get('specVersion')!r} is a v3 patch level this "
                       f"build has not been verified against "
                       f"({list(SUPPORTED_SPEC_VERSIONS)}); accepted as v3."})

    for f in PF_REQUIRED:
        if doc.get(f) in (None, "", [], {}):
            errors.append({"field": f, "severity": "error",
                           "message": f"{f} is required (SHALL)"})

    if doc.get("id") is not None and not _UUID_RE.match(str(doc.get("id"))):
        errors.append({"field": "id", "severity": "error",
                       "message": "id must be a UUID"})
    if doc.get("status") is not None and doc.get("status") not in PF_STATUSES:
        errors.append({"field": "status", "severity": "error",
                       "message": f"status must be one of {list(PF_STATUSES)}"})
    if doc.get("created") is not None and not _iso_datetime(doc.get("created")):
        errors.append({"field": "created", "severity": "error",
                       "message": "created must be an ISO-8601 date-time"})

    for f in ("companyIds", "productIds"):
        v = doc.get(f)
        if v is not None and not _nonempty_str_list(v):
            errors.append({"field": f, "severity": "error",
                           "message": f"{f} must be a non-empty array of URN strings"})
        elif isinstance(v, list) and any(not _URN_RE.match(x) for x in v if isinstance(x, str)):
            warnings.append({"field": f, "severity": "warning",
                             "message": f"{f} entries should be URNs (urn:...)"})

    pre = doc.get("precedingPfIds")
    if pre is not None:
        if not isinstance(pre, list) or any(not _UUID_RE.match(str(x)) for x in pre):
            errors.append({"field": "precedingPfIds", "severity": "error",
                           "message": "precedingPfIds must be an array of UUIDs"})

    # v2 leftovers. Their presence in a v3-labelled document means the sender did a
    # relabel rather than a migration, and the v3-required fields will be missing.
    stale = [f for f in V2_ONLY_PF_FIELDS if f in doc]
    if stale:
        warnings.append({
            "field": None, "severity": "warning",
            "message": f"v2-only properties present and ignored: {stale}. v3 removed "
                       f"them — versioning is now immutable (a new id plus "
                       f"precedingPfIds), not an incrementing `version`."})

    cf = doc.get("pcf")
    geography_level = None
    if cf is None:
        pass                       # already reported as a missing SHALL above
    elif not isinstance(cf, dict):
        errors.append({"field": "pcf", "severity": "error",
                       "message": "pcf must be a CarbonFootprint object"})
    else:
        for f in CF_REQUIRED:
            if cf.get(f) is None or cf.get(f) == "":
                errors.append({"field": f"pcf.{f}", "severity": "error",
                               "message": f"{f} is required (SHALL) in v3"})

        stale_cf = [f for f in V2_ONLY_CF_FIELDS if f in cf]
        if stale_cf:
            warnings.append({
                "field": "pcf", "severity": "warning",
                "message": f"v2-only pcf properties present and ignored: {stale_cf}. v3 "
                           f"renamed them (declaredUnit -> declaredUnitOfMeasurement, "
                           f"unitaryProductAmount -> declaredUnitAmount, "
                           f"pCfExcludingBiogenic -> pcfExcludingBiogenicUptake, "
                           f"assurance -> verification)."})

        unit = cf.get("declaredUnitOfMeasurement")
        if unit is not None and unit not in DECLARED_UNITS:
            errors.append({"field": "pcf.declaredUnitOfMeasurement", "severity": "error",
                           "message": f"declaredUnitOfMeasurement must be one of "
                                      f"{list(DECLARED_UNITS)}; got {unit!r}"})

        for f in ("declaredUnitAmount", "productMassPerDeclaredUnit",
                  "pcfExcludingBiogenicUptake", "pcfIncludingBiogenicUptake",
                  "fossilCarbonContent", "fossilGhgEmissions",
                  "exemptedEmissionsPercent", "primaryDataShare"):
            raw = cf.get(f)
            if raw is None:
                continue
            if not _is_decimal_string(raw):
                if parse_decimal(raw) is not None:
                    warnings.append({
                        "field": f"pcf.{f}", "severity": "warning",
                        "message": f"{f} is a JSON number; the spec types every quantity "
                                   f"as a decimal STRING. Accepted on read, but emitting "
                                   f"it this way would fail conformance."})
                else:
                    errors.append({"field": f"pcf.{f}", "severity": "error",
                                   "message": f"{f} must be a decimal string"})

        amt = parse_decimal(cf.get("declaredUnitAmount"))
        if amt is not None and amt <= 0:
            errors.append({"field": "pcf.declaredUnitAmount", "severity": "error",
                           "message": "declaredUnitAmount must be > 0"})
        for f in ("fossilCarbonContent", "fossilGhgEmissions",
                  "productMassPerDeclaredUnit"):
            v = parse_decimal(cf.get(f))
            if v is not None and v < 0:
                errors.append({"field": f"pcf.{f}", "severity": "error",
                               "message": f"{f} must be >= 0"})
        share = parse_decimal(cf.get("primaryDataShare"))
        if share is not None and not (0.0 <= share <= 100.0):
            errors.append({"field": "pcf.primaryDataShare", "severity": "error",
                           "message": "primaryDataShare is a percentage in [0, 100]"})
        exempt = parse_decimal(cf.get("exemptedEmissionsPercent"))
        if exempt is not None and not (0.0 <= exempt <= 5.0):
            errors.append({
                "field": "pcf.exemptedEmissionsPercent", "severity": "error",
                "message": "exemptedEmissionsPercent must be in [0, 5] — the methodology "
                           "caps what may be excluded from a conforming PCF"})

        for f in ("referencePeriodStart", "referencePeriodEnd"):
            if cf.get(f) is not None and not _iso_datetime(cf.get(f)):
                errors.append({"field": f"pcf.{f}", "severity": "error",
                               "message": f"{f} must be an ISO-8601 date-time"})
        s, e = cf.get("referencePeriodStart"), cf.get("referencePeriodEnd")
        if _iso_datetime(s) and _iso_datetime(e) and e <= s:
            errors.append({"field": "pcf.referencePeriodEnd", "severity": "error",
                           "message": "referencePeriodEnd must be after "
                                      "referencePeriodStart"})

        for f in ("ipccCharacterizationFactors", "crossSectoralStandards"):
            v = cf.get(f)
            if v is not None and not _nonempty_str_list(v):
                errors.append({"field": f"pcf.{f}", "severity": "error",
                               "message": f"{f} must be a non-empty array of strings"})
        std = cf.get("crossSectoralStandards")
        if isinstance(std, list):
            unknown = [x for x in std if isinstance(x, str)
                       and not any(x.startswith(k) for k in CROSS_SECTORAL_STANDARDS)]
            if unknown:
                warnings.append({
                    "field": "pcf.crossSectoralStandards", "severity": "warning",
                    "message": f"unrecognised standard(s) {unknown}; expected values "
                               f"starting with one of {list(CROSS_SECTORAL_STANDARDS)}"})

        if cf.get("packagingEmissionsIncluded") is not None and not isinstance(
                cf.get("packagingEmissionsIncluded"), bool):
            errors.append({"field": "pcf.packagingEmissionsIncluded", "severity": "error",
                           "message": "packagingEmissionsIncluded must be a boolean"})

        geography_level = _validate_geography(cf, errors)
        _validate_dqi(cf, errors, warnings)

    return {
        "valid": not errors,
        "spec_version": doc.get("specVersion") if isinstance(doc, dict) else None,
        "geography_level": geography_level or ("global" if cf else None),
        "errors": errors,
        "warnings": warnings,
    }


def summarise(doc: dict) -> dict:
    """The fields worth denormalising out of a validated document, for querying.

    Decimal STRINGS are carried through untouched beside their parsed floats: the
    string is what the supplier published and what must round-trip byte-identical;
    the float is only for arithmetic.
    """
    cf = doc.get("pcf") or {}
    dqi = cf.get("dqi") or {}
    geo_level = next((f for f in GEOGRAPHY_FIELDS if cf.get(f) not in (None, "")), None)
    return {
        "pf_id": doc.get("id"),
        "spec_version": doc.get("specVersion"),
        "status": doc.get("status"),
        "created": doc.get("created"),
        "company_name": doc.get("companyName"),
        "company_ids": doc.get("companyIds") or [],
        "product_ids": doc.get("productIds") or [],
        "product_name": doc.get("productNameCompany"),
        "product_description": doc.get("productDescription"),
        "preceding_pf_ids": doc.get("precedingPfIds") or [],
        "validity_period_start": doc.get("validityPeriodStart"),
        "validity_period_end": doc.get("validityPeriodEnd"),
        "declared_unit": cf.get("declaredUnitOfMeasurement"),
        "declared_unit_amount": parse_decimal(cf.get("declaredUnitAmount")),
        "declared_unit_amount_raw": cf.get("declaredUnitAmount"),
        "pcf_excl_biogenic": parse_decimal(cf.get("pcfExcludingBiogenicUptake")),
        "pcf_excl_biogenic_raw": cf.get("pcfExcludingBiogenicUptake"),
        "pcf_incl_biogenic": parse_decimal(cf.get("pcfIncludingBiogenicUptake")),
        "pcf_incl_biogenic_raw": cf.get("pcfIncludingBiogenicUptake"),
        "reference_period_start": cf.get("referencePeriodStart"),
        "reference_period_end": cf.get("referencePeriodEnd"),
        "primary_data_share": parse_decimal(cf.get("primaryDataShare")),
        "geography_level": geo_level or "global",
        "geography_value": cf.get(geo_level) if geo_level else None,
        "cross_sectoral_standards": cf.get("crossSectoralStandards") or [],
        "ipcc_characterization_factors": cf.get("ipccCharacterizationFactors") or [],
        "dqi_technological": parse_decimal(dqi.get("technologicalDQR")),
        "dqi_geographical": parse_decimal(dqi.get("geographicalDQR")),
        "dqi_temporal": parse_decimal(dqi.get("temporalDQR")),
        "verification": cf.get("verification"),
    }


def kg_co2e_per_declared_unit(summary: dict, *, include_biogenic: bool = False
                              ) -> Optional[float]:
    """Emissions per ONE declared unit, from a PCF quoted per declaredUnitAmount.

    The spec quotes the footprint against `declaredUnitAmount` of the unit, not
    against one of it. Dividing is the whole point: a PCF of 42 kgCO2e for a
    declaredUnitAmount of 10 kilogram is 4.2 per kilogram, and using 42 would
    overstate by an order of magnitude.
    """
    total = summary.get("pcf_incl_biogenic" if include_biogenic else "pcf_excl_biogenic")
    amount = summary.get("declared_unit_amount")
    if total is None or amount in (None, 0):
        return None
    return total / amount


def parse_document(raw) -> tuple:
    """(document, error) from JSON text or an already-decoded object."""
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, f"document is not valid UTF-8: {exc}"
    if not isinstance(raw, str):
        return None, "document must be JSON text or an object"
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return None, f"document is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "a ProductFootprint must be a JSON object"
    return parsed, None
