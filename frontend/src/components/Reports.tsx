import { useEffect, useMemo, useState } from "react";
import Derivations from "./Derivations";
import { api, ReportingPeriod, Settings } from "../api";
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
  // Loaded only when a param actually needs them, so every other report keeps its two
  // requests.
  const needsRuns = fw.params.some((p) => p.kind === "run");
  const [runs, setRuns] = useState<any[]>([]);
  const [periods, setPeriods] = useState<ReportingPeriod[]>([]);

  useEffect(() => {
    if (!needsRuns) return;
    api.runs(settings).then((r) => setRuns(r.runs)).catch(() => { /* picker stays empty */ });
    api.reportingPeriods(settings).then((r) => setPeriods(r.items)).catch(() => { });
  }, [needsRuns, settings.apiKey, settings.baseUrl]);

  // A run is only useful as a comparative if a reader can see WHICH period it covers —
  // the whole point of the gates is that two runs of different spans must not be
  // subtracted, and an id alone hides the span.
  const runOptionLabel = (r: any) => {
    const p = periods.find((x) => x.id === r.reporting_period_id);
    const where = p ? `${p.label} (${p.start_date} → ${p.end_date})` : "whole history";
    return `#${r.id} · ${where} · ${r.gwp_set} · ${r.created_at?.slice(0, 10)}`;
  };

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
      // A report with no required-data checklist has nothing to show here, and
      // asking for one would render an empty "not assessable" panel.
      if (fw.hasChecklist === false) { setCheck(null); }
      else try { setCheck(await api.compliance(settings, fw.key, cparams)); }
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
  // Each framework names its own comparative clause, but the block shape is one shape —
  // it comes from a single builder, so one renderer reads all of them.
  const comparative = report?.e1_6_comparative || report?.prior_year_comparative || null;

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
              {p.kind === "run" ? (
                <select value={vals[p.name] ?? ""}
                        onChange={(e) => setVals({ ...vals, [p.name]: e.target.value })}>
                  <option value="">— none —</option>
                  {runs.filter((r) => r.id !== runId).map((r) => (
                    <option key={r.id} value={String(r.id)}>{runOptionLabel(r)}</option>
                  ))}
                </select>
              ) : p.options ? (
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
            {fw.exportable !== false && (
              <>
                <button onClick={() => downloadServer("pdf")} disabled={exporting !== null}>
                  {exporting === "pdf" ? "Preparing…" : "📄 PDF"}
                </button>
                <button onClick={() => downloadServer("csv")} disabled={exporting !== null}>
                  {exporting === "csv" ? "Preparing…" : "📊 CSV"}
                </button>
              </>
            )}
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

          {comparative && <Comparative block={comparative} />}

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

// ---------------------------------------------------------------------------------------
// The prior-period comparative. One component for every framework, because the block
// comes from one builder (app/reports/comparative.py).
//
// The job here is to keep the three refusals DISTINGUISHABLE on screen, which is the
// same requirement the payload meets with `reason_code`. A first reporting year, an
// omitted comparative and a pair the gates refused are three different facts, and
// flattening them into one grey "no comparative available" would undo the reason the
// block exists. So: a neutral note, a warning, and a refusal with every reason listed.
function Comparative({ block }: { block: any }) {
  const computed = block.status === "computed";
  const firstYear = block.reason_code === "no_prior_period_on_file";
  const periods = block.period_comparability;

  const fmtNum = (v: number | null | undefined) =>
    v === null || v === undefined
      ? "—"
      : v.toLocaleString(undefined, { maximumFractionDigits: 4 });

  return (
    <div style={{ marginTop: 14 }}>
      <h4 style={{ marginBottom: 6 }}>
        {block.clause}
        {computed
          ? <span className="badge ok" style={{ marginLeft: 8 }}>✓ comparable</span>
          : firstYear
            ? <span className="badge" style={{ marginLeft: 8 }}>first reporting period</span>
            : <span className="badge warn" style={{ marginLeft: 8 }}>cannot determine</span>}
      </h4>

      {!computed && (
        <div className={"callout " + (firstYear ? "" : "bad")}>
          <p style={{ margin: 0 }}>{block.reason}</p>
          {(block.blockers || []).length > 0 && (
            <>
              <b style={{ display: "block", marginTop: 8 }}>
                {/* Only `not_comparable` is a verdict ON A PAIR. For the others there
                    is no second run, and heading the list "why these two runs cannot be
                    subtracted" would assert a gate result that was never reached. */}
                {block.reason_code === "not_comparable"
                  ? "Why these two runs cannot be subtracted"
                  : "What this blocks"}
              </b>
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {block.blockers.map((b: string, i: number) =>
                  <li key={i} style={{ marginBottom: 4 }}>{b}</li>)}
              </ul>
            </>
          )}
          {/* No figures are shown for a refused comparative. A delta beside a blocker
              still gets quoted, which is exactly how a divestment came to be published
              as a substantiated reduction. */}
        </div>
      )}

      {computed && (
        <table>
          <thead>
            <tr>
              <th>Figure</th>
              <th>Reporting period</th>
              <th>Comparative period</th>
              <th>Change</th>
              <th>%</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(block.figures || {}).map(([k, f]: any) => (
              <tr key={k}>
                <td>{k.replace(/_tco2e$/, "").replace(/_/g, " ")}</td>
                <td>{fmtNum(f.current)}</td>
                <td>{fmtNum(f.prior)}</td>
                {/* A fall is good news, a rise is not, and NO change is neither —
                    colouring zero as a warning reads as a finding where there is none. */}
                <td className={!f.change ? "" : f.change < 0 ? "ok" : "warn"}>
                  {f.status === "computed" ? fmtNum(f.change) : "cannot determine"}
                </td>
                <td>
                  {f.change_pct === null || f.change_pct === undefined
                    ? <span className="muted" title={f.pct_reason || f.reason || ""}>—</span>
                    : `${f.change_pct > 0 ? "+" : ""}${fmtNum(f.change_pct)}%`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Both windows, published whether or not the delta was. A reader of a refused
          comparative needs them as much as a reader of a computed one. */}
      {periods && (
        <p className="muted" style={{ marginTop: 8 }}>
          Reporting period {periods.reporting_period?.label ?? "—"}:{" "}
          {periods.reporting_days ?? "?"} days · comparative{" "}
          {periods.comparative_period?.label ?? "—"}:{" "}
          {periods.comparative_days ?? "?"} days · tolerance {periods.tolerance_pct}%
        </p>
      )}
      {(block.prior_periods_on_file || []).length > 0 && !computed && (
        <p className="muted">
          Runs available as a comparative:{" "}
          {block.prior_periods_on_file.map((c: any) =>
            `#${c.run_id} (${c.reporting_period?.label ?? "unscoped"})`).join(", ")}
        </p>
      )}
    </div>
  );
}
