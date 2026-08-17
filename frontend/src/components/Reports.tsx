import { useMemo, useState } from "react";
import Derivations from "./Derivations";
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
  const [check, setCheck] = useState<any>(null);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const generate = async () => {
    setError(null); setReport(null); setCheck(null); setCheckError(null);
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
      const cparams = { ...params };
      if (fw.assessmentScoped) cparams.assessment_id = assessmentId.trim();
      // Awaited, with its own error state: a silently-swallowed failure would just hide the
      // checklist and read as "this framework has no requirements".
      try { setCheck(await api.compliance(settings, fw.key, cparams)); }
      catch (e: any) { setCheck(null); setCheckError(e.message); }
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

  const saveBlob = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  };

  const downloadJson = () =>
    saveBlob(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }),
             `${fw.key}_report.json`);

  // CSV/PDF are produced by the SERVER from the same immutable run (never from this page's
  // copy of the payload), so a downloaded document can't contain client-edited figures.
  const [exporting, setExporting] = useState<null | "csv" | "pdf">(null);
  const downloadServer = async (format: "csv" | "pdf") => {
    setExporting(format); setError(null);
    try {
      const params: Record<string, string | number | undefined> = { format };
      if (fw.assessmentScoped) params.assessment_id = assessmentId.trim();
      else if (fw.runScoped !== false) params.run_id = runId;
      for (const p of fw.params) {
        const v = vals[p.name];
        if (v !== undefined && v !== "") params[p.name] = v;
      }
      const blob = await api.exportReport(settings, fw.key, params);
      saveBlob(blob, `${fw.key}_report.${format}`);
    } catch (e: any) { setError(e.message); }
    finally { setExporting(null); }
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
            <button onClick={() => downloadServer("pdf")} disabled={exporting !== null}>
              {exporting === "pdf" ? "Preparing…" : "📄 PDF"}
            </button>
            <button onClick={() => downloadServer("csv")} disabled={exporting !== null}>
              {exporting === "csv" ? "Preparing…" : "📊 CSV"}
            </button>
            <button onClick={downloadJson}>⬇ JSON</button>
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

      {report?.derivations && <Derivations block={report.derivations} />}

      {checkError && (
        <div className="callout warn">Could not load the required-data checklist: {checkError}</div>
      )}

      {check?.assessable && (
        <div className="card">
          <div className="card-head">
            <h3>Required-data checklist</h3>
            <div className="spacer" />
            <span className={"badge " + (check.data_complete ? "ok" : "warn")}>
              {check.data_fields_present}/{check.required_data_fields} required fields
              {check.data_complete ? " · complete" : ` · ${check.data_completeness_pct}%`}
            </span>
          </div>
          <p className="lead" style={{ marginTop: 0 }}>{check.verdict}</p>

          {(check.thresholds || []).length > 0 && (
            <table style={{ marginBottom: 14 }}>
              <thead><tr><th>Metric</th><th>Achieved</th><th>Required</th><th>Gap</th></tr></thead>
              <tbody>
                {check.thresholds.map((t: any, i: number) => (
                  <tr key={i}>
                    <td>{t.metric}</td>
                    <td>{t.actual ?? "—"} {t.unit}</td>
                    <td>{t.required} {t.unit}
                      {t.provenance === "platform" && (
                        <div className="muted">platform guideline</div>
                      )}
                    </td>
                    <td className={t.status === "met" ? "ok" : t.status === "short" ? "bad" : "muted"}>
                      {t.status === "met" ? "✓ met" : t.explanation}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <table>
            <thead><tr><th>Reference</th><th>Requirement</th><th>Status</th></tr></thead>
            <tbody>
              {(check.requirements || []).map((r: any, i: number) => (
                <tr key={i}>
                  <td className="muted">{r.ref}</td>
                  <td>{r.requirement}</td>
                  <td>
                    {r.status === "present" && <span className="badge ok">Present</span>}
                    {r.status === "missing" && <span className="badge bad">Missing</span>}
                    {r.status === "preparer_must_supply" && <span className="badge warn">You supply</span>}
                    {r.status === "requires_independent_assurance" && <span className="badge">Assurance</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ marginTop: 12 }}>{check.scope_note}</p>
        </div>
      )}
    </>
  );
}
