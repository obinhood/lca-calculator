"""Export a disclosure report as CSV or PDF.

The report is ALWAYS regenerated server-side from the immutable run (never built from a
payload the client posts back), so an exported document cannot contain figures the engine
would not itself produce. `BUILDERS` maps a framework key to the renderer that produces it,
mirroring the GET endpoints one-for-one.

The PDF is a disclosure DOCUMENT — the framework's required fields laid out for a reader —
not a certification. It carries the same fail-closed verdict as the JSON: a report that is
not disclosure-ready is stamped DRAFT and lists what is missing, and every document states
that it is unverified. See `_DISCLAIMER`.
"""
import csv
import io
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------------------
# Param coercion — query params arrive as strings.

def _int(v):    return int(v) if v not in (None, "") else None
def _float(v):  return float(v) if v not in (None, "") else None
def _str(v):    return str(v) if v not in (None, "") else None
def _bool(v):   return str(v).strip().lower() in ("1", "true", "yes", "on")


def _p(params: Dict[str, Any], name: str, cast: Callable, default=None):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid value for {name!r}: {raw!r}")


# ---------------------------------------------------------------------------------------
# Builders: framework key -> callable(db, organisation_id, params) -> report payload.

def _b_secr(db, org, p):
    from .secr import secr_report
    return secr_report(db, org, run_id=_p(p, "run_id", _int),
                       intensity_denominator=_p(p, "intensity_denominator", _float),
                       intensity_denominator_unit=_p(p, "intensity_denominator_unit", _str))

def _b_sb253(db, org, p):
    from .sb253 import sb253_report
    return sb253_report(db, org, run_id=_p(p, "run_id", _int),
                        assurance_level=_p(p, "assurance_level", _str, "limited"),
                        assurance_provider=_p(p, "assurance_provider", _str))

def _b_esrs(db, org, p):
    from .esrs_e1 import esrs_e1_report
    return esrs_e1_report(db, org, run_id=_p(p, "run_id", _int),
                          net_revenue_millions=_p(p, "net_revenue_millions", _float),
                          revenue_currency=_p(p, "revenue_currency", _str, "EUR"),
                          credits_as_of=_p(p, "credits_as_of", _str))

def _b_issb(db, org, p):
    from .issb_s2 import issb_s2_report
    return issb_s2_report(db, org, run_id=_p(p, "run_id", _int),
                          jurisdiction=_p(p, "jurisdiction", _str, "ISSB"))

def _b_gri(db, org, p):
    from .gri import gri_report
    return gri_report(db, org, run_id=_p(p, "run_id", _int),
                      base_run_id=_p(p, "base_run_id", _int),
                      intensity_denominator=_p(p, "intensity_denominator", _float),
                      intensity_denominator_unit=_p(p, "intensity_denominator_unit", _str),
                      intensity_denominator_period_days=_p(
                          p, "intensity_denominator_period_days", _int))

def _b_cdp(db, org, p):
    from .cdp import cdp_export
    return cdp_export(db, org, run_id=_p(p, "run_id", _int),
                      intensity_denominator=_p(p, "intensity_denominator", _float),
                      intensity_denominator_unit=_p(p, "intensity_denominator_unit", _str),
                      verification_status=_p(p, "verification_status", _str,
                                             "no_third_party_verification"))

def _b_tcfd(db, org, p):
    from .tcfd import tcfd_report
    return tcfd_report(db, org, run_id=_p(p, "run_id", _int),
                       jurisdiction_reference=_p(p, "jurisdiction_reference", _str))

def _b_csddd(db, org, p):
    from .csddd import csddd_report
    return csddd_report(db, org, run_id=_p(p, "run_id", _int),
                        target_id=_p(p, "target_id", _int),
                        current_year=_p(p, "current_year", _int))

def _b_scope3(db, org, p):
    from .scope3 import scope3_inventory_report
    return scope3_inventory_report(db, org, run_id=_p(p, "run_id", _int))

def _b_esos(db, org, p):
    from .compliance_extra import esos_report
    return esos_report(db, org, run_id=_p(p, "run_id", _int))

def _b_ets(db, org, p):
    from .compliance_extra import ets_mrv_report
    scheme = _p(p, "scheme", _str, "EU ETS")
    if scheme not in ("EU ETS", "UK ETS"):
        raise ValueError("scheme must be 'EU ETS' or 'UK ETS'")
    return ets_mrv_report(db, org, scheme, run_id=_p(p, "run_id", _int),
                          verified=_bool(p.get("verified")))

def _b_taxonomy(db, org, p):
    from .compliance_extra import taxonomy_report
    return taxonomy_report(db, org, _p(p, "reporting_year", _int, 2025))

def _b_cbam(db, org, p):
    from .cbam import cbam_declaration
    from ..services.cbam import DEFINITIVE_PERIOD_START
    # The ETS reference price must reach the declaration: without it the certificate
    # count is suppressed, and a CSV/PDF export silently loses the headline figure.
    # Default to the first definitive-period year, matching the UI. A 2025 default sent
    # unparameterised exports into the transitional period, where certificates are zero.
    return cbam_declaration(db, org, _p(p, "year", _int, DEFINITIVE_PERIOD_START),
                            ets_price_eur_per_t=_p(p, "ets_price_eur_per_t", _float))

def _b_ecovadis(db, org, p):
    from .ecovadis import ecovadis_readiness
    return ecovadis_readiness(
        db, org, run_id=_p(p, "run_id", _int), baseline_run_id=_p(p, "baseline_run_id", _int),
        intensity_denominator=_p(p, "intensity_denominator", _float),
        denominator_unit=_p(p, "denominator_unit", _str),
        has_environmental_policy=_bool(p.get("has_environmental_policy")),
        iso_14001_certified=_bool(p.get("iso_14001_certified")),
        published_sustainability_report=_bool(p.get("published_sustainability_report")))

def _b_pcaf(db, org, p):
    from ..services.pcaf import portfolio_financed
    inc = p.get("include_scope3")
    return portfolio_financed(db, org, include_scope3=(True if inc in (None, "") else _bool(inc)),
                              as_of=_p(p, "as_of", _str))

def _b_sfdr(db, org, p):
    from .sfdr_pai import sfdr_pai_report
    inc = p.get("include_scope3")
    return sfdr_pai_report(db, org,
                           portfolio_value_millions=_p(p, "portfolio_value_millions", _float),
                           include_scope3=(True if inc in (None, "") else _bool(inc)),
                           # PAI 2 is per EUR million: the denominator's currency travels
                           # with an export, or the exported figure claims a unit it
                           # cannot substantiate.
                           portfolio_value_currency=_p(p, "portfolio_value_currency",
                                                       _str, "EUR"),
                           fx_year=_p(p, "fx_year", _int))

def _b_sbti(db, org, p):
    from .sbti import sbti_report
    tid = _p(p, "target_id", _int)
    if tid is None:
        raise ValueError("target_id is required for an SBTi report")
    return sbti_report(db, org, tid, current_run_id=_p(p, "current_run_id", _int),
                       current_year=_p(p, "current_year", _int))

def _b_14064_2(db, org, p):
    from .iso_14064_2 import iso_14064_2_report
    return iso_14064_2_report(db, org, baseline_run_id=_p(p, "baseline_run_id", _int),
                              project_run_id=_p(p, "project_run_id", _int),
                              leakage_tco2e=_p(p, "leakage_tco2e", _float),
                              baseline_justification=_p(p, "baseline_justification", _str),
                              project_name=_p(p, "project_name", _str))

def _b_neutrality(db, org, p):
    from .summary import _resolve_run
    from ..services.neutrality import neutrality_assessment
    run = _resolve_run(db, org, _p(p, "run_id", _int))
    if run is None:
        return {"framework": "ISO 14068 carbon neutrality", "disclosure_ready": False,
                "blockers": ["no calculation run exists"]}
    basis = _p(p, "basis", _str, "location")
    if basis not in ("location", "market"):
        raise ValueError("basis must be location|market")
    return neutrality_assessment(db, org, run, basis=basis)

def _b_removals(db, org, p):
    from .summary import _resolve_run
    from ..services.removals import removals_completeness
    run = _resolve_run(db, org, _p(p, "run_id", _int))
    if run is None:
        return {"framework": "GHG Protocol Land Sector & Removals", "disclosure_ready": False,
                "blockers": ["no calculation run exists"]}
    g = removals_completeness(db, run)
    return {"framework": "GHG Protocol Land Sector & Removals",
            "disclosure_ready": bool(g.get("assessable")) and not g.get("blockers"),
            "blockers": g.get("blockers", []), "run": {"id": run.id}, **g}

def _b_assurance(db, org, p):
    from .summary import _resolve_run
    from ..services.assurance import readiness_assessment
    run = _resolve_run(db, org, _p(p, "run_id", _int))
    if run is None:
        return {"framework": "Assurance readiness", "disclosure_ready": False,
                "blockers": ["no calculation run exists"]}
    return readiness_assessment(db, run)

def _b_tnfd(db, org, p):
    from ..services.nature import leap_assessment
    return leap_assessment(db, org)

def _b_sbtn(db, org, p):
    from ..services.nature import sbtn_report
    return sbtn_report(db, org)

def _assessment(db, org, p):
    from ..models import LcaAssessment
    aid = _p(p, "assessment_id", _int)
    if aid is None:
        raise ValueError("assessment_id is required for this report")
    a = db.query(LcaAssessment).filter(LcaAssessment.id == aid,
                                       LcaAssessment.organisation_id == org).first()
    if a is None:
        raise ValueError("assessment not found for this organisation")
    return a

def _b_lca(db, org, p):
    from ..services.lca import compute_assessment
    return compute_assessment(db, _assessment(db, org, p))

def _b_epd(db, org, p):
    from .epd import epd_report
    return epd_report(db, org, _p(p, "assessment_id", _int),
                      pcr_reference=_p(p, "pcr_reference", _str),
                      programme_operator=_p(p, "programme_operator", _str))

def _b_rics(db, org, p):
    from .rics import rics_report
    return rics_report(db, org, _p(p, "assessment_id", _int), gia_unit=_p(p, "gia_unit", _str))

def _b_pef(db, org, p):
    from .pef import pef_report
    return pef_report(db, org, _p(p, "assessment_id", _int))


BUILDERS: Dict[str, Callable[[Session, int, dict], dict]] = {
    "secr": _b_secr, "sb253": _b_sb253, "esrs_e1": _b_esrs, "issb_s2": _b_issb,
    "gri": _b_gri, "cdp": _b_cdp, "tcfd": _b_tcfd, "csddd": _b_csddd,
    "scope3_inventory": _b_scope3, "removals": _b_removals, "neutrality": _b_neutrality,
    "assurance_readiness": _b_assurance, "esos": _b_esos, "ets_mrv": _b_ets,
    "eu_taxonomy": _b_taxonomy, "cbam": _b_cbam, "ecovadis": _b_ecovadis,
    "pcaf": _b_pcaf, "sfdr_pai": _b_sfdr, "sbti": _b_sbti, "iso_14064_2": _b_14064_2,
    "tnfd": _b_tnfd, "sbtn": _b_sbtn,
    "lca": _b_lca, "epd": _b_epd, "rics": _b_rics, "pef": _b_pef,
}


def build_report(db: Session, organisation_id: int, key: str, params: dict) -> dict:
    builder = BUILDERS.get(key)
    if builder is None:
        raise KeyError(key)
    return builder(db, organisation_id, params)


def build_with_compliance(db: Session, organisation_id: int, key: str, params: dict) -> dict:
    """The report plus its required-data checklist, so exports carry the completeness
    evidence rather than only the figures."""
    from .compliance import evaluate
    payload = build_report(db, organisation_id, key, params)
    payload = dict(payload)
    payload["required_data_check"] = evaluate(key, payload)
    return payload


# ---------------------------------------------------------------------------------------
# CSV

_SKIP_IN_CSV = {"guidance"}          # inline framework guidance is reference text, not data


def _flatten(obj: Any, prefix: str = "", out: Optional[list] = None) -> list:
    """Flatten a report payload to (path, value) rows — one row per leaf."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not prefix and k in _SKIP_IN_CSV:
                continue
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, (list, tuple)):
        if not obj:
            out.append((prefix, ""))
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out.append((prefix, "" if obj is None else obj))
    return out


def to_csv(payload: dict) -> str:
    """Report as CSV: section, field, value — one leaf per row, stable order.

    A flat long format (rather than a guessed wide table) because report payloads are deeply
    nested and differ per framework; this keeps every field addressable in a spreadsheet.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["section", "field", "value"])
    for path, value in _flatten(payload):
        head, _, tail = path.partition(".")
        w.writerow([head, tail or head, value])
    return buf.getvalue()


# ---------------------------------------------------------------------------------------
# PDF

_DISCLAIMER = (
    "This document was generated automatically from an immutable calculation run. It is "
    "NOT an independently verified or assured statement, and it is not legal or regulatory "
    "advice. Figures must be reviewed — and where the framework requires it, assured — "
    "before filing. Sections the platform does not produce are listed under Scope limitations."
)

# Payload keys that hold the headline figures, in the order we like to show them.
_FIGURE_BLOCKS = [
    ("emissions_tco2e", "Emissions (tCO2e)"),
    ("e1_6_gross_ghg_emissions_tco2e", "E1-6 Gross GHG emissions (tCO2e)"),
    ("ghg_emissions_tco2e", "GHG emissions (tCO2e)"),
    ("quantification_tco2e", "Quantification (tCO2e)"),
    ("energy_use_kwh", "Energy use (kWh)"),
    ("e1_5_energy_consumption", "E1-5 Energy consumption"),
    ("intensity_ratio", "Intensity ratio"),
    ("gri_305_1_scope1", "GRI 305-1 Scope 1"),
    ("gri_305_2_scope2", "GRI 305-2 Scope 2"),
    ("gri_305_3_scope3", "GRI 305-3 Scope 3"),
]

_LIMITATION_KEYS = [
    ("not_covered", "Not covered"),
    ("not_produced", "Not produced"),
    ("requirements_not_produced", "Requirements not produced"),
    ("narrative_disclosures_not_produced", "Narrative disclosures not produced"),
    ("exclusions", "Exclusions"),
]


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.6f}".rstrip("0").rstrip(".") if v else "0"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return "" if v is None else str(v)


def to_pdf(payload: dict, *, framework_label: str, organisation: str,
           generated_at: str) -> bytes:
    """Render the report as a disclosure document.

    Layout: title + status verdict, blockers (if not ready), the framework's reported
    figures, methodology, scope limitations, then the disclaimer. Requires reportlab.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                    ListFlowable, ListItem)

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=18, spaceAfter=4, alignment=0)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9.5, textColor=colors.HexColor("#5f6f68"))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8, leading=11,
                           textColor=colors.HexColor("#5f6f68"))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"{framework_label} — {organisation}",
                            author="Carbon Platform",
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    story = []

    story.append(Paragraph(framework_label, h1))
    story.append(Paragraph(
        f"Disclosure report · Organisation: <b>{organisation}</b><br/>"
        f"Generated {generated_at}", sub))
    story.append(Spacer(1, 10))

    # --- Verdict, carried through from the same fail-closed gate as the JSON. ---
    ready = None
    for k in ("disclosure_ready", "filing_ready", "ok"):
        if isinstance(payload.get(k), bool):
            ready = payload[k]
            break
    blockers = payload.get("blockers") or []
    if ready is True:
        banner, bg = "DISCLOSURE-READY", colors.HexColor("#dcfce7")
    elif ready is False:
        banner, bg = "DRAFT — NOT DISCLOSURE-READY", colors.HexColor("#fee2e2")
    else:
        banner, bg = "INFORMATION REPORT", colors.HexColor("#f0f4f2")
    t = Table([[Paragraph(f"<b>{banner}</b>", body)]], colWidths=[doc.width])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg),
                           ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfd9d4")),
                           ("PADDING", (0, 0), (-1, -1), 8)]))
    story.append(t)

    if blockers:
        story.append(Paragraph("Outstanding items before this can be filed", h2))
        story.append(ListFlowable(
            [ListItem(Paragraph(str(b), body)) for b in blockers],
            bulletType="bullet", start="•", leftIndent=12))

    if payload.get("report_scope"):
        story.append(Paragraph("Scope of this report", h2))
        story.append(Paragraph(str(payload["report_scope"]), body))

    # --- Reported figures. ---
    def _table(rows, title):
        if not rows:
            return
        # The derivation sub-tables sit under their own figure heading, so they pass
        # None rather than repeating it above every worked calculation.
        if title:
            story.append(Paragraph(title, h2))
        tb = Table([["Field", "Value"]] + rows, colWidths=[doc.width * 0.55, doc.width * 0.45])
        tb.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f4f2")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#5f6f68")),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8e4")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("PADDING", (0, 0), (-1, -1), 5)]))
        story.append(tb)

    shown = False
    for key, title in _FIGURE_BLOCKS:
        block = payload.get(key)
        if isinstance(block, dict):
            rows = [[Paragraph(k.replace("_", " "), body), Paragraph(_fmt(v), body)]
                    for k, v in block.items() if not isinstance(v, (dict, list))]
            if rows:
                _table(rows, title)
                shown = True

    # Fall back to top-level scalars so a framework with an unusual shape still shows figures.
    if not shown:
        rows = [[Paragraph(k.replace("_", " "), body), Paragraph(_fmt(v), body)]
                for k, v in payload.items()
                if isinstance(v, (int, float, str, bool))
                and k not in ("framework", "report_scope", "methodology",
                              "methodology_statement", "disclosure_ready", "filing_ready", "ok")]
        _table(rows[:40], "Reported values")

    # --- Assumed scopes. This is the document that gets FILED, so the caveat has to be
    # printed beside the figures it qualifies: where a category was unrecognised, which
    # scope those emissions sit in is a machine guess, not a preparer's classification.
    _sa = payload.get("scope_assumptions")
    if isinstance(_sa, dict) and _sa.get("assumed_scope3_by_category"):
        story.append(Paragraph("Assumed scope classification", h2))
        story.append(Paragraph(
            "Unrecognised categories defaulted to Scope 3 (activity count): "
            + ", ".join(f"<b>{k}</b> — {v}" for k, v in
                        sorted(_sa["assumed_scope3_by_category"].items())), body))
        story.append(Paragraph(f"<i>{_sa.get('note', '')}</i>", body))

    # --- How each figure was arrived at. The point of the section is reperformance:
    # an assurer should be able to rebuild every number from what is printed here.
    dvs = payload.get("derivations") or {}
    if dvs.get("figures"):
        story.append(Paragraph("How these figures were calculated", h2))
        story.append(Paragraph(str(dvs.get("note", "")), body))
        if dvs.get("independently_verified"):
            story.append(Paragraph(
                "Independently verified (checked against a separately derived value, so "
                "these fail on bad data): "
                + ", ".join(dvs["independently_verified"]), body))
        if dvs.get("warning"):
            story.append(Paragraph(f"<b>{dvs['warning']}</b>", body))
        for d in dvs["figures"]:
            unit = f" {d['unit']}" if d.get("unit") else ""
            story.append(Paragraph(
                f"<b>{d['figure']}</b> = {_fmt(d.get('reported'))}{unit}", body))
            drows = []
            if d.get("basis"):
                drows.append([Paragraph("basis", body), Paragraph(str(d["basis"]), body)])
            for t in d.get("terms", []):
                lbl = t["label"] + (f"  [{t['count']} record(s)]"
                                    if t.get("count") is not None else "")
                drows.append([Paragraph(lbl, body),
                              Paragraph(_fmt(t.get("value")), body)])
            if d["operation"] not in ("stated", "alternatives", "blocked"):
                drows.append([Paragraph("<b>=</b>", body),
                              Paragraph(f"<b>{d['expression']}</b>", body)])
            elif d["operation"] == "alternatives":
                drows.append([Paragraph("", body),
                              Paragraph("<i>alternative measurements of the same "
                                        "quantity — never added</i>", body)])
            if drows:
                _table(drows, None)
            # A blocked figure's `note` and `reconciliation_error` carry the same text,
            # so printing both said it twice.
            if d.get("note") and d["operation"] != "blocked":
                story.append(Paragraph(f"<i>{d['note']}</i>", body))
            if not d.get("reconciles"):
                _err = d.get("reconciliation_error") or "this figure could not be reconciled"
                story.append(Paragraph(f"<b>{_err}</b>", body))

    method = payload.get("methodology_statement") or payload.get("methodology")
    if method:
        story.append(Paragraph("Methodology", h2))
        story.append(Paragraph(str(method), body))

    # --- Scope limitations: what this platform does NOT produce for this framework. ---
    lim_rows = []
    for key, title in _LIMITATION_KEYS:
        vals = payload.get(key)
        if isinstance(vals, list) and vals:
            for v in vals:
                lim_rows.append(f"<b>{title}:</b> {v if isinstance(v, str) else _fmt(v)}")
    if lim_rows:
        story.append(Paragraph("Scope limitations", h2))
        story.append(ListFlowable([ListItem(Paragraph(x, body)) for x in lim_rows[:40]],
                                  bulletType="bullet", start="•", leftIndent=12))

    # --- Required-data checklist: does this document carry what the framework asks for? ---
    chk = payload.get("required_data_check") or {}
    if chk.get("assessable"):
        story.append(Paragraph("Required-data checklist", h2))
        story.append(Paragraph(str(chk.get("verdict", "")), body))
        rows = [[Paragraph(f"<b>{r['ref']}</b><br/>{r['requirement']}", body),
                 Paragraph({"present": "Present",
                            "missing": "MISSING",
                            "preparer_must_supply": "You must supply",
                            "requires_independent_assurance": "Independent assurance",
                            }.get(r["status"], r["status"]), body)]
                for r in chk.get("requirements", [])]
        _table(rows, f"Coverage of required disclosures "
                     f"({chk.get('data_fields_present')}/{chk.get('required_data_fields')} "
                     f"platform fields present)")

        thr = chk.get("thresholds") or []
        if thr:
            trows = [[Paragraph(t["metric"], body),
                      Paragraph(f"{_fmt(t['actual'])} {t.get('unit','')}", body),
                      Paragraph(f"{_fmt(t['required'])} {t.get('unit','')}", body),
                      Paragraph(t["explanation"], body)] for t in thr]
            story.append(Paragraph("Thresholds — achieved vs required", h2))
            tt = Table([["Metric", "Achieved", "Required", "Gap"]] + trows,
                       colWidths=[doc.width * .28, doc.width * .18, doc.width * .18,
                                  doc.width * .36])
            tt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f4f2")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#5f6f68")),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8e4")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 5)]))
            story.append(tt)
        story.append(Paragraph(str(chk.get("scope_note", "")), small))

    run = payload.get("run") or {}
    if isinstance(run, dict) and run.get("id"):
        story.append(Paragraph("Traceability", h2))
        story.append(Paragraph(
            f"Generated from immutable calculation run #{run.get('id')}"
            + (f", created {run.get('created_at')}" if run.get("created_at") else "")
            + (f", GWP set {run.get('gwp_set')}" if run.get("gwp_set") else "")
            + ". Recalculating creates a new run; this one is unchanged and reproducible.", body))

    story.append(Spacer(1, 14))
    story.append(Paragraph(_DISCLAIMER, small))

    doc.build(story)
    return buf.getvalue()
