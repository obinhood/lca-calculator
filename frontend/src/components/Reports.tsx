import { useMemo, useState } from "react";
import { api, Settings } from "../api";

// One immutable inventory -> many disclosure frameworks. Each is ONE registry entry
// (endpoint path + its params); the renderer below is generic — it shows the fail-closed
// gate (ready / blockers) first, then headline numbers, methodology and the full payload.
// Adding a framework's UI is adding a REGISTRY entry, not new rendering code.

type Param = {
  name: string;                 // query-param name sent to the backend
  label: string;
  def: string;                  // default value
  options?: string[];           // present -> render a <select>
  width?: number;
};

type Framework = {
  key: string;
  label: string;
  path: string;                 // GET endpoint (base path; for assessmentScoped, /{id} is appended)
  runScoped?: boolean;          // send run_id (default true; ignored when assessmentScoped)
  assessmentScoped?: boolean;   // report is over an LCA assessment id (path param), not a run
  params: Param[];
  note?: string;                // honest-scope one-liner shown under the picker
};

// run-scoped org inventory reports. All are org-scoped by the API key.
const FRAMEWORKS: Framework[] = [
  { key: "secr", label: "UK SECR", path: "/reports/secr", params: [
      { name: "intensity_denominator", label: "intensity denom", def: "1.0", width: 90 },
      { name: "intensity_denominator_unit", label: "unit", def: "£M revenue", width: 130 } ] },
  { key: "sb253", label: "California SB 253", path: "/reports/sb253", params: [
      { name: "assurance_level", label: "assurance", def: "limited",
        options: ["none", "limited", "reasonable"] },
      { name: "assurance_provider", label: "provider", def: "", width: 160 } ] },
  { key: "esrs_e1", label: "CSRD ESRS E1", path: "/reports/esrs_e1", params: [
      { name: "net_revenue_millions", label: "net revenue (M)", def: "10", width: 90 },
      { name: "revenue_currency", label: "currency", def: "EUR", width: 70 } ] },
  { key: "issb_s2", label: "ISSB IFRS S2", path: "/reports/issb_s2", params: [
      { name: "jurisdiction", label: "jurisdiction", def: "ISSB",
        options: ["ISSB", "UK_SRS", "JP_SSBJ", "SG_SGX", "HK_HKEX"] } ] },
  { key: "gri", label: "GRI 305 / 302", path: "/reports/gri", params: [
      { name: "base_run_id", label: "base run id (305-5)", def: "", width: 110 },
      { name: "intensity_denominator", label: "intensity denom", def: "1.0", width: 90 },
      { name: "intensity_denominator_unit", label: "unit", def: "unit", width: 90 } ] },
  { key: "cdp", label: "CDP Climate", path: "/reports/cdp", params: [
      { name: "intensity_denominator", label: "intensity denom", def: "1.0", width: 90 },
      { name: "intensity_denominator_unit", label: "unit", def: "unit", width: 90 },
      { name: "verification_status", label: "verification", def: "no_third_party_verification",
        width: 200 } ] },
  { key: "neutrality", label: "ISO 14068 neutrality", path: "/reports/neutrality", params: [
      { name: "basis", label: "basis", def: "location", options: ["location", "market"] } ] },
  { key: "scope3_inventory", label: "GHGP Scope 3 inventory", path: "/reports/scope3_inventory",
    params: [] },
  { key: "removals", label: "GHGP removals", path: "/reports/removals", params: [] },
  { key: "esos", label: "UK ESOS", path: "/reports/esos", params: [] },
  { key: "assurance_readiness", label: "Assurance readiness (ISO 14064)",
    path: "/reports/assurance_readiness", params: [] },
  { key: "ets_mrv", label: "EU/UK ETS — MRV", path: "/reports/ets_mrv", params: [
      { name: "scheme", label: "scheme", def: "EU ETS", options: ["EU ETS", "UK ETS"] },
      { name: "verified", label: "verified", def: "false", options: ["false", "true"] } ] },
  { key: "tcfd", label: "TCFD (cross-reference map)", path: "/reports/tcfd", params: [
      { name: "jurisdiction_reference", label: "jurisdiction ref", def: "", width: 160 } ],
    note: "Cross-reference map: only Metrics & Targets (b) is machine-produced (from ISSB S2)." },
  { key: "csddd", label: "EU CSDDD (Art 22 carbon inputs)", path: "/reports/csddd", params: [
      { name: "target_id", label: "SBTi target id", def: "", width: 100 },
      { name: "current_year", label: "current year", def: "", width: 90 } ],
    note: "Readiness of the Art 22 carbon INPUTS only — not the plan or the due-diligence process." },
  { key: "ecovadis", label: "EcoVadis (carbon/energy)", path: "/reports/ecovadis", params: [
      { name: "baseline_run_id", label: "baseline run id", def: "", width: 100 },
      { name: "intensity_denominator", label: "intensity denom", def: "1.0", width: 90 },
      { name: "denominator_unit", label: "unit", def: "unit", width: 80 },
      { name: "has_environmental_policy", label: "env policy", def: "false",
        options: ["false", "true"] },
      { name: "iso_14001_certified", label: "ISO 14001", def: "false", options: ["false", "true"] },
      { name: "published_sustainability_report", label: "published report", def: "false",
        options: ["false", "true"] } ],
    note: "Covers only the carbon/energy portion of the Environment theme — no score or medal." },
  // --- Nature (separate spatial data model; org-scoped, not tied to a carbon run) ---
  { key: "tnfd", label: "TNFD (nature)", path: "/reports/tnfd", runScoped: false, params: [] },
  { key: "sbtn", label: "SBTN (nature targets)", path: "/reports/sbtn", runScoped: false,
    params: [] },
  // --- Not tied to a single calculation run ---
  { key: "sbti", label: "SBTi target", path: "/reports/sbti", runScoped: false, params: [
      { name: "target_id", label: "target id", def: "", width: 100 } ] },
  { key: "iso_14064_2", label: "ISO 14064-2 (project)", path: "/reports/iso_14064_2",
    runScoped: false, params: [
      { name: "baseline_run_id", label: "baseline run id", def: "", width: 110 },
      { name: "project_run_id", label: "project run id", def: "", width: 110 },
      { name: "leakage_tco2e", label: "leakage tCO₂e", def: "0", width: 90 },
      { name: "baseline_justification", label: "baseline justification", def: "", width: 220 },
      { name: "project_name", label: "project name", def: "", width: 140 } ],
    note: "Reduction = baseline run − project run − leakage; a separate account from the corporate inventory." },
  { key: "pcaf", label: "PCAF (financed emissions)", path: "/reports/pcaf", runScoped: false,
    params: [
      { name: "include_scope3", label: "incl. scope 3", def: "true", options: ["true", "false"] },
      { name: "as_of", label: "as of (ISO ts)", def: "", width: 170 } ] },
  { key: "sfdr_pai", label: "SFDR PAI", path: "/reports/sfdr_pai", runScoped: false, params: [
      { name: "portfolio_value_millions", label: "portfolio value (M)", def: "", width: 120 },
      { name: "include_scope3", label: "incl. scope 3", def: "true", options: ["true", "false"] } ] },
  { key: "cbam", label: "EU CBAM", path: "/reports/cbam", runScoped: false, params: [
      { name: "year", label: "year", def: "2025", width: 70 } ] },
  { key: "eu_taxonomy", label: "EU Taxonomy", path: "/reports/eu_taxonomy", runScoped: false,
    params: [ { name: "reporting_year", label: "reporting year", def: "2025", width: 90 } ] },
  // --- Product / building LCA reports (over an LCA assessment id, not a run) ---
  { key: "lca", label: "LCA assessment", path: "/reports/lca", assessmentScoped: true,
    params: [] },
  { key: "epd", label: "ISO 14025 / EN 15804 EPD", path: "/reports/epd", assessmentScoped: true,
    params: [ { name: "pcr_reference", label: "PCR reference", def: "", width: 160 } ],
    note: "The GWP-fossil core a verifier would check — not a verified EPD (needs a PCR + programme operator)." },
  { key: "rics", label: "RICS Whole Life Carbon", path: "/reports/rics", assessmentScoped: true,
    params: [ { name: "gia_unit", label: "GIA unit", def: "", width: 90 } ],
    note: "RICS groupings over an en_15978 building; GWP-fossil only, not a full RICS-compliant WLCA." },
  { key: "pef", label: "EU PEF (single category)", path: "/reports/pef", assessmentScoped: true,
    params: [],
    note: "1 of 16 EF 3.1 impact categories (Climate change, GWP-fossil) — not a PEF profile." },
];

// Frameworks grouped into sections so the picker reads as categories, not a flat list of 27.
const GROUPS: { label: string; keys: string[] }[] = [
  { label: "Corporate carbon reporting", keys: ["secr", "sb253", "esrs_e1", "issb_s2", "gri", "cdp", "tcfd"] },
  { label: "Inventory & assurance", keys: ["scope3_inventory", "removals", "neutrality", "assurance_readiness"] },
  { label: "Compliance & carbon pricing", keys: ["ets_mrv", "esos", "cbam", "eu_taxonomy", "csddd"] },
  { label: "Finance", keys: ["pcaf", "sfdr_pai"] },
  { label: "Targets & projects", keys: ["sbti", "iso_14064_2"] },
  { label: "Product & building footprints", keys: ["lca", "epd", "rics", "pef"] },
  { label: "Nature", keys: ["tnfd", "sbtn"] },
  { label: "Ratings", keys: ["ecovadis"] },
];

// Safety net: any framework not placed in a group above still shows, under "Other" —
// so adding a registry entry can never make it silently disappear from the picker.
const _GROUPED = new Set(GROUPS.flatMap((g) => g.keys));
const _RENDER_GROUPS = [
  ...GROUPS,
  { label: "Other", keys: FRAMEWORKS.filter((f) => !_GROUPED.has(f.key)).map((f) => f.key) },
].filter((g) => g.keys.length > 0);

export default function Reports({ settings, runId }:
    { settings: Settings; runId?: number }) {
  const [key, setKey] = useState<string>(FRAMEWORKS[0].key);
  const fw = useMemo(() => FRAMEWORKS.find((f) => f.key === key)!, [key]);
  const [vals, setVals] = useState<Record<string, string>>(
    () => Object.fromEntries(FRAMEWORKS[0].params.map((p) => [p.name, p.def])));
  const [assessmentId, setAssessmentId] = useState<string>("");
  const [report, setReport] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const selectFramework = (k: string) => {
    const next = FRAMEWORKS.find((f) => f.key === k)!;
    setKey(k);
    setVals(Object.fromEntries(next.params.map((p) => [p.name, p.def])));
    setReport(null); setError(null);
  };

  const generate = async () => {
    setError(null); setReport(null);
    // Assessment-scoped reports need an id in the PATH; guard before firing a bad request.
    if (fw.assessmentScoped && !assessmentId.trim()) {
      setError("enter an LCA assessment id for this report"); return;
    }
    setLoading(true);
    try {
      const params: Record<string, string | number | undefined> = {};
      if (!fw.assessmentScoped && fw.runScoped !== false) params.run_id = runId;
      for (const p of fw.params) {
        const v = vals[p.name];
        if (v !== undefined && v !== "") params[p.name] = v;
      }
      const path = fw.assessmentScoped ? `${fw.path}/${assessmentId.trim()}` : fw.path;
      setReport(await api.report(settings, path, params));
    } catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  };

  // Generic gate + numbers extraction — works across every framework's payload shape.
  const readyKey = report
    ? (["disclosure_ready", "filing_ready", "ok"] as const).find(
        (k) => typeof report[k] === "boolean")
    : undefined;
  const ready = readyKey ? report[readyKey] : undefined;
  const blockers: string[] = report?.blockers || [];
  const emissions = report && (
    report.emissions_tco2e || report.e1_6_gross_ghg_emissions_tco2e ||
    report.ghg_emissions_tco2e || null);
  const methodology = report?.methodology_statement || report?.methodology || null;

  return (
    <div className="panel">
      <h2>Disclosure reports</h2>
      <div className="row">
        <select value={key} onChange={(e) => selectFramework(e.target.value)}>
          {_RENDER_GROUPS.map((g) => (
            <optgroup key={g.label} label={g.label}>
              {g.keys.map((k) => {
                const f = FRAMEWORKS.find((x) => x.key === k);
                return f ? <option key={f.key} value={f.key}>{f.label}</option> : null;
              })}
            </optgroup>
          ))}
        </select>
        {fw.assessmentScoped && (
          <span className="row" style={{ gap: 4 }}>
            <label className="muted">assessment id</label>
            <input style={{ width: 90 }} value={assessmentId}
                   onChange={(e) => setAssessmentId(e.target.value)} />
          </span>
        )}
        {fw.params.map((p) => (
          <span key={p.name} className="row" style={{ gap: 4 }}>
            <label className="muted">{p.label}</label>
            {p.options ? (
              <select value={vals[p.name] ?? p.def}
                      onChange={(e) => setVals({ ...vals, [p.name]: e.target.value })}>
                {p.options.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : (
              <input style={{ width: p.width ?? 110 }} value={vals[p.name] ?? ""}
                     onChange={(e) => setVals({ ...vals, [p.name]: e.target.value })} />
            )}
          </span>
        ))}
        <button className="primary" onClick={generate} disabled={loading}>
          {loading ? "…" : "Generate"}
        </button>
        <span className="muted">
          {fw.assessmentScoped ? "over an LCA assessment"
            : runId ? `run #${runId}` : "latest run"}
        </span>
      </div>
      {fw.note && <p className="muted">{fw.note}</p>}
      {error && <p className="bad">{error}</p>}
      {report && (
        <>
          {ready !== undefined && (
            <p className={ready ? "ok" : "bad"}>
              {report.framework}: {ready ? "✔ disclosure-ready" : "✖ NOT ready"}
            </p>
          )}
          {report.report_scope && <p className="muted">{report.report_scope}</p>}
          {ready !== true && blockers.length > 0 && (
            <div className="blockers">
              {blockers.map((b, i) => <p key={i} className="bad">{b}</p>)}
            </div>
          )}
          {emissions && (
            <div className="kpis">
              {Object.entries(emissions).map(([k, v]: any) =>
                typeof v === "number" ? (
                  <div className="kpi" key={k}>
                    <div className="v">{v.toLocaleString(undefined, { maximumFractionDigits: 4 })}</div>
                    <div className="l">{k} (tCO₂e)</div>
                  </div>
                ) : null)}
            </div>
          )}
          {methodology && (
            <details>
              <summary className="muted">Methodology</summary>
              <pre className="detail">{methodology}</pre>
            </details>
          )}
          <details>
            <summary className="muted">Full payload (JSON)</summary>
            <pre className="detail">{JSON.stringify(report, null, 2)}</pre>
          </details>
        </>
      )}
    </div>
  );
}
