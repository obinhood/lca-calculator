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
  path: string;                 // GET endpoint
  runScoped?: boolean;          // send run_id (default true)
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
];

export default function Reports({ settings, runId }:
    { settings: Settings; runId?: number }) {
  const [key, setKey] = useState<string>(FRAMEWORKS[0].key);
  const fw = useMemo(() => FRAMEWORKS.find((f) => f.key === key)!, [key]);
  const [vals, setVals] = useState<Record<string, string>>(
    () => Object.fromEntries(FRAMEWORKS[0].params.map((p) => [p.name, p.def])));
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
    setError(null); setReport(null); setLoading(true);
    try {
      const params: Record<string, string | number | undefined> = {};
      if (fw.runScoped !== false) params.run_id = runId;
      for (const p of fw.params) {
        const v = vals[p.name];
        if (v !== undefined && v !== "") params[p.name] = v;
      }
      setReport(await api.report(settings, fw.path, params));
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
          {FRAMEWORKS.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
        </select>
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
        <span className="muted">{runId ? `run #${runId}` : "latest run"}</span>
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
