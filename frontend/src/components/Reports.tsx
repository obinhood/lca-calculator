import { useMemo, useState } from "react";
import { api, Settings } from "../api";
import { CATEGORIES, FRAMEWORKS, Framework, NEEDS_LABEL } from "../frameworks";
import type { Page } from "../App";

// Two views: a BROWSABLE CATALOGUE of every framework (searchable, grouped by category), and
// a runner for the one you pick. The runner is generic — it reads the registry entry for the
// endpoint + its inputs, then shows the fail-closed gate, the numbers, and the raw payload.

export default function Reports({ settings, runId, go, hasRun }:
    { settings: Settings; runId?: number; go: (p: Page) => void; hasRun: boolean }) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [cat, setCat] = useState<string>("All");

  const matches = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return FRAMEWORKS.filter((f) =>
      (cat === "All" || f.category === cat) &&
      (!needle ||
        f.label.toLowerCase().includes(needle) ||
        (f.full || "").toLowerCase().includes(needle) ||
        f.blurb.toLowerCase().includes(needle) ||
        f.category.toLowerCase().includes(needle)));
  }, [q, cat]);

  const open = FRAMEWORKS.find((f) => f.key === openKey);
  if (open) return <Runner fw={open} settings={settings} runId={runId} back={() => setOpenKey(null)} />;

  const shownCats = CATEGORIES.filter((c) => matches.some((f) => f.category === c));

  return (
    <>
      {!hasRun && (
        <div className="callout warn" style={{ marginBottom: 16 }}>
          <b>You don't have a calculation run yet.</b> Most reports need one — add data and
          calculate first, and they'll fill in automatically.{" "}
          <button className="link" onClick={() => go("data")}>Add data →</button>
        </div>
      )}

      <div className="filters">
        <input placeholder="Search frameworks — e.g. CSRD, Scope 3, CBAM…" value={q}
               onChange={(e) => setQ(e.target.value)} style={{ minWidth: 300, flex: 1 }} />
        <button className={"chip" + (cat === "All" ? " active" : "")}
                onClick={() => setCat("All")}>All {FRAMEWORKS.length}</button>
        {CATEGORIES.map((c) => (
          <button key={c} className={"chip" + (cat === c ? " active" : "")}
                  onClick={() => setCat(c)}>{c}</button>
        ))}
      </div>

      {matches.length === 0 && (
        <div className="card empty">
          <h3>No frameworks match “{q}”</h3>
          <p>Try a different term, or clear the filters.</p>
          <button onClick={() => { setQ(""); setCat("All"); }}>Clear filters</button>
        </div>
      )}

      {shownCats.map((c) => (
        <div key={c}>
          <div className="cat-title">{c}</div>
          <div className="cards">
            {matches.filter((f) => f.category === c).map((f) => (
              <button key={f.key} className="rcard" onClick={() => setOpenKey(f.key)}>
                <b>{f.label}</b>
                {f.full && <span className="full">{f.full}</span>}
                <p>{f.blurb}</p>
                <div className="foot">
                  <span className="badge">{NEEDS_LABEL[f.needs]}</span>
                  {f.note && <span className="badge warn">Partial scope</span>}
                </div>
              </button>
            ))}
          </div>
        </div>
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------------------
function Runner({ fw, settings, runId, back }:
    { fw: Framework; settings: Settings; runId?: number; back: () => void }) {
  const [vals, setVals] = useState<Record<string, string>>(
    () => Object.fromEntries(fw.params.map((p) => [p.name, p.def])));
  const [assessmentId, setAssessmentId] = useState("");
  const [report, setReport] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const generate = async () => {
    setError(null); setReport(null);
    if (fw.assessmentScoped && !assessmentId.trim()) {
      setError("Enter the LCA assessment ID this report should cover."); return;
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

  const readyKey = report
    ? (["disclosure_ready", "filing_ready", "ok"] as const).find((k) => typeof report[k] === "boolean")
    : undefined;
  const ready = readyKey ? report[readyKey] : undefined;
  const blockers: string[] = report?.blockers || [];
  const emissions = report && (report.emissions_tco2e || report.e1_6_gross_ghg_emissions_tco2e ||
    report.ghg_emissions_tco2e || null);
  const methodology = report?.methodology_statement || report?.methodology || null;

  const download = () => {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url; a.download = `${fw.key}_report.json`; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <button className="link" onClick={back} style={{ marginBottom: 12 }}>← All reports</button>

      <div className="card">
        <div className="card-head">
          <div>
            <h2>{fw.label}</h2>
            <div className="muted">{fw.full || fw.category}</div>
          </div>
          <div className="spacer" />
          <span className="badge brand">{fw.category}</span>
        </div>
        <p className="lead">{fw.blurb}</p>
        {fw.note && <div className="callout warn" style={{ marginTop: 12 }}>
          <b>Scope note.</b> {fw.note}
        </div>}
      </div>

      <div className="card">
        <h3 style={{ marginBottom: 10 }}>Inputs</h3>
        {fw.params.length === 0 && !fw.assessmentScoped && (
          <p className="muted">No inputs needed — this report reads your inventory directly.</p>
        )}
        <div className="row" style={{ alignItems: "flex-end", gap: 14 }}>
          {fw.assessmentScoped && (
            <label className="field">LCA assessment ID
              <input style={{ width: 130 }} value={assessmentId}
                     onChange={(e) => setAssessmentId(e.target.value)} placeholder="e.g. 1" />
              <span className="hint">the product or building assessment to report on</span>
            </label>
          )}
          {fw.params.map((p) => (
            <label className="field" key={p.name}>{p.label}
              {p.options ? (
                <select value={vals[p.name] ?? p.def}
                        onChange={(e) => setVals({ ...vals, [p.name]: e.target.value })}>
                  {p.options.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <input style={{ width: p.width ?? 130 }} value={vals[p.name] ?? ""}
                       onChange={(e) => setVals({ ...vals, [p.name]: e.target.value })} />
              )}
              {p.help && <span className="hint">{p.help}</span>}
            </label>
          ))}
          <button className="primary big" onClick={generate} disabled={loading}>
            {loading ? "Generating…" : "Generate report"}
          </button>
        </div>
        {error && <p className="bad" style={{ marginBottom: 0 }}>{error}</p>}
      </div>

      {report && (
        <div className="card">
          <div className="card-head">
            <h3>Result</h3>
            <div className="spacer" />
            {ready !== undefined && (
              <span className={"badge " + (ready ? "ok" : "warn")}>
                {ready ? "✓ Disclosure-ready" : "Not ready yet"}
              </span>
            )}
            <button onClick={download}>⬇ Download JSON</button>
          </div>

          {report.report_scope && <p className="muted">{report.report_scope}</p>}

          {ready !== true && blockers.length > 0 && (
            <div className="callout bad" style={{ marginBottom: 14 }}>
              <b>What's missing before this can be filed</b>
              <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                {blockers.map((b, i) => <li key={i} style={{ marginBottom: 4 }}>{b}</li>)}
              </ul>
            </div>
          )}

          {emissions && (
            <div className="kpis">
              {Object.entries(emissions).map(([k, v]: any) =>
                typeof v === "number" ? (
                  <div className="kpi" key={k}>
                    <div className="v">{v.toLocaleString(undefined, { maximumFractionDigits: 4 })}</div>
                    <div className="l">{k.replace(/_/g, " ")} (tCO₂e)</div>
                  </div>
                ) : null)}
            </div>
          )}

          {methodology && (
            <details style={{ marginTop: 12 }}>
              <summary>Methodology statement</summary>
              <pre className="detail">{methodology}</pre>
            </details>
          )}
          <details>
            <summary>Full data (JSON)</summary>
            <pre className="detail">{JSON.stringify(report, null, 2)}</pre>
          </details>
        </div>
      )}
    </>
  );
}
