import { useEffect, useState } from "react";
import { api, isAuthError, ReportingPeriod, Settings } from "../api";
import type { Page } from "../App";
import { SESSION_GONE } from "./Home";

const fmt = (v: number | null | undefined, d = 1) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: d });

// The sentinel for a deliberate unscoped run. A reporting period is not defaulted,
// because the default was the defect: this client sent no period at all, so every run
// covered the whole uploaded history and a second year's upload doubled the headline.
// The two cases are now both reachable and neither is implicit.
const WHOLE_HISTORY = "whole";

const periodLabel = (p: ReportingPeriod) =>
  `${p.label}${p.start_date ? ` · ${p.start_date} → ${p.end_date ?? "?"}` : ""}` +
  (p.frozen ? " · frozen" : "");


export default function Dashboard({ settings, runId, onSelectRun, version, onChanged, go,
                                   onAuthError }: {
  settings: Settings; runId?: number; onSelectRun: (id?: number) => void;
  version: number; onChanged: () => void; go: (p: Page) => void;
  onAuthError: (why: string) => void;
}) {
  const [runs, setRuns] = useState<any[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [gwpSet, setGwpSet] = useState("AR6");
  const [busy, setBusy] = useState(false);
  const [seeding, setSeeding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [periods, setPeriods] = useState<ReportingPeriod[]>([]);
  // "" = nothing chosen yet, which DISABLES the run button. Deliberately not
  // pre-selected: silently choosing for the preparer is how the unscoped run became
  // universal in the first place.
  const [periodChoice, setPeriodChoice] = useState<string>("");
  const [newPeriod, setNewPeriod] = useState<{ label: string; start: string; end: string }
    | null>(null);
  const [creating, setCreating] = useState(false);

  const loadDemo = async () => {
    setSeeding(true); setError(null);
    try { const r = await api.seedDemo(settings); onSelectRun(r.run_id); onChanged(); }
    catch (e: any) {
      if (isAuthError(e)) { onAuthError(SESSION_GONE); return; }
      setError(e.message);
    }
    setSeeding(false);
  };

  useEffect(() => {
    api.runs(settings).then((r) => setRuns(r.runs))
      .catch((e) => { if (isAuthError(e)) onAuthError(SESSION_GONE); else setError(e.message); });
    api.summary(settings, runId).then(setSummary)
      .catch((e) => { if (!isAuthError(e)) setError(e.message); });
    api.reportingPeriods(settings).then((r) => setPeriods(r.items))
      .catch((e) => { if (!isAuthError(e)) setError(e.message); });
  }, [settings.apiKey, settings.baseUrl, runId, version]);

  const compute = async () => {
    if (!periodChoice) {
      setError("Choose the reporting period this run covers — or “whole history” if you "
               + "deliberately want every uploaded activity in one figure.");
      return;
    }
    setBusy(true); setError(null);
    try {
      // undefined for the whole-history case, so no reporting_period_id is sent.
      const r = await api.run(settings, gwpSet,
        periodChoice === WHOLE_HISTORY ? undefined : Number(periodChoice));
      onSelectRun(r.run?.id);
      onChanged();
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };

  const createPeriod = async () => {
    if (!newPeriod) return;
    const { label, start, end } = newPeriod;
    if (!label.trim() || !start || !end) {
      setError("A reporting period needs a label and both dates — the dates are what "
               + "scope the run, and what every comparative is checked against.");
      return;
    }
    setCreating(true); setError(null);
    try {
      const p = await api.createReportingPeriod(settings, label.trim(), start, end);
      const r = await api.reportingPeriods(settings);
      setPeriods(r.items);
      setPeriodChoice(String(p.id));          // select what was just created
      setNewPeriod(null);
    } catch (e: any) { setError(e.message); }
    setCreating(false);
  };

  const cov = summary?.coverage;
  const s2 = summary?.scope2;
  const dq = summary?.data_quality;
  const ms = summary?.method_split;

  return (
    <>
      <div className="panel">
        <div className="row">
          <label className="muted">Reporting period</label>
          <select value={periodChoice} onChange={(e) => setPeriodChoice(e.target.value)}>
            <option value="">— choose —</option>
            {periods.map((p) => (
              <option key={p.id} value={String(p.id)} disabled={p.frozen}>
                {periodLabel(p)}
              </option>
            ))}
            <option value={WHOLE_HISTORY}>Whole history (not a reporting period)</option>
          </select>
          <button onClick={() => setNewPeriod(
            newPeriod ? null : { label: "", start: "", end: "" })}>
            {newPeriod ? "Cancel" : "+ New period"}
          </button>
          <span className="muted">|</span>
          <select value={gwpSet} onChange={(e) => setGwpSet(e.target.value)}>
            <option>AR6</option><option>AR5</option>
          </select>
          <button className="primary" onClick={compute} disabled={busy || !periodChoice}>
            {busy ? "Computing…" : "New calculation run"}
          </button>
          <span className="muted">|</span>
          <label className="muted">Run</label>
          <select value={runId ?? ""} onChange={(e) => onSelectRun(e.target.value ? Number(e.target.value) : undefined)}>
            <option value="">latest</option>
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                #{r.id} · {periods.find((p) => p.id === r.reporting_period_id)?.label
                           ?? "whole history"} · {r.gwp_set} ·{" "}
                {r.created_at?.slice(0, 16)} · {fmt(r.total_co2e)} kg
              </option>
            ))}
          </select>
          <span className="muted">runs are immutable — recomputing creates a new one</span>
        </div>
        {newPeriod && (
          <div className="row" style={{ alignItems: "flex-end", gap: 10, marginTop: 10 }}>
            <label className="field">Label
              <input style={{ width: 120 }} placeholder="FY2025" value={newPeriod.label}
                     onChange={(e) => setNewPeriod({ ...newPeriod, label: e.target.value })} />
            </label>
            <label className="field">Start
              <input type="date" value={newPeriod.start}
                     onChange={(e) => setNewPeriod({ ...newPeriod, start: e.target.value })} />
            </label>
            <label className="field">End
              <input type="date" value={newPeriod.end}
                     onChange={(e) => setNewPeriod({ ...newPeriod, end: e.target.value })} />
            </label>
            <button className="primary" onClick={createPeriod} disabled={creating}>
              {creating ? "Creating…" : "Create period"}
            </button>
            <span className="muted">
              only activities dated inside the period are computed
            </span>
          </div>
        )}
        {periodChoice === WHOLE_HISTORY && (
          <p className="warn" style={{ marginBottom: 0 }}>
            ⚠ A whole-history run totals <b>every</b> uploaded activity, however many years
            that spans. It is an exploratory view: the annual frameworks (ESRS E1, SECR)
            will refuse to file it, because it would state a multi-year total as the
            period's.
          </p>
        )}
        {error && <p className="bad">{error}</p>}
      </div>

      {summary?.run ? (
        <>
          {summary.partial && (
            <div className="panel blockers">
              <b className="bad">PARTIAL RUN</b> — excluded: {JSON.stringify(summary.partial_reasons)}
            </div>
          )}
          {/* The payload has carried `coverage.period_scoped` all along; nothing
              displayed it, so an unscoped run was indistinguishable on screen from a
              year's. That silence is half of the defect — the doubled figure was the
              other half. */}
          {cov && cov.period_scoped === false && (
            <div className="panel blockers">
              <b className="bad">NOT SCOPED TO A REPORTING PERIOD</b> — the figures below
              total every uploaded activity, across all years on file. Correct for that
              span, but it is <b>not</b> a reporting period's footprint: on two years of
              data this total is roughly twice the latter year's. Pick a reporting period
              above and recompute before quoting or filing any of it.
            </div>
          )}
          <div className="panel">
            <h2>Run #{summary.run.id} <span className="muted">{summary.run.gwp_set} · {summary.run.created_at}</span></h2>
            <p className="muted" style={{ marginTop: -4 }}>
              {summary.run.reporting_period_id
                ? `Reporting period: ${periods.find((p) => p.id === summary.run.reporting_period_id)
                    ? periodLabel(periods.find((p) => p.id === summary.run.reporting_period_id)!)
                    : `#${summary.run.reporting_period_id}`}`
                : "Reporting period: none — whole uploaded history"}
            </p>
            <div className="kpis">
              <div className="kpi"><div className="v">{fmt(summary.total_co2e)}</div><div className="l">kgCO₂e location-based</div></div>
              <div className="kpi"><div className="v">{fmt(summary.total_co2e_market)}</div><div className="l">kgCO₂e market-based</div></div>
              <div className="kpi"><div className="v">{fmt(summary.biogenic_co2e_separate)}</div><div className="l">biogenic CO₂ (separate)</div></div>
              <div className="kpi">
                <div className="v">{cov ? `${cov.coverage_pct}%` : "—"}</div>
                <div className="l">coverage ({cov?.coverage_basis})</div>
                <div className="bar"><div style={{ width: `${cov?.coverage_pct || 0}%` }} /></div>
              </div>
              <div className="kpi">
                <div className="v">{dq?.has_data ? dq.emissions_weighted_score : "—"}</div>
                <div className="l">data quality (1 best..5 worst)</div>
              </div>
            </div>
            {cov?.warning && <p className="warn">⚠ {cov.warning}</p>}
          </div>

          <div className="panel">
            <h2>By scope <span className="muted">(location basis)</span></h2>
            <table>
              <thead><tr><th>Scope</th><th>kgCO₂e</th><th></th></tr></thead>
              <tbody>
                {(summary.by_scope || []).map((r: any) => (
                  <tr key={r.scope}>
                    <td>Scope {r.scope}</td>
                    <td>{fmt(r.co2e)}</td>
                    <td style={{ width: "50%" }}>
                      <div className="bar"><div style={{ width: `${(100 * r.co2e) / (summary.total_co2e || 1)}%` }} /></div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {s2 && (
              <p className="muted">
                Scope 2 dual: location {fmt(s2.location_based)} / market {fmt(s2.market_based)} kg ·
                contractual {fmt(s2.kwh_contractual)} kWh · grid fallback {fmt(s2.kwh_grid_fallback)} kWh
              </p>
            )}
          </div>

          <div className="panel">
            <h2>Method &amp; uncertainty</h2>
            {ms && (
              <p>
                {Object.entries(ms.co2e_by_method || {}).map(([k, v]: any) => (
                  <span key={k} className="badge" style={{ marginRight: 6 }}>{k}: {fmt(v)} kg</span>
                ))}
                <span className="muted"> · primary-data share {ms.primary_data_share_pct}% · spend-based {ms.spend_based_share_pct}%</span>
              </p>
            )}
            {dq?.has_data && (
              <p className="muted">
                Emissions-weighted 95% band: {fmt(dq.approx_ci95_low)} – {fmt(dq.approx_ci95_high)} kgCO₂e
                · ratings: {Object.entries(dq.co2e_by_rating || {}).map(([k, v]: any) =>
                  <span key={k} className={`badge ${k}`} style={{ marginLeft: 4 }}>{k} {fmt(v)}</span>)}
              </p>
            )}
            {(summary.exclusions || []).length > 0 && (
              <details>
                <summary className="warn">{summary.exclusions.length} excluded activities (click)</summary>
                <table>
                  <tbody>
                    {summary.exclusions.map((x: any, i: number) => (
                      <tr key={i}><td>#{x.activity_id}</td><td className="bad">{x.error}</td></tr>
                    ))}
                  </tbody>
                </table>
              </details>
            )}
          </div>
        </>
      ) : (
        <div className="panel">
          <div className="empty">
            <h3>No emissions inventory yet</h3>
            <p>
              Load the sample dataset to see a complete inventory and reports in one click, or add
              your own activity data and run a calculation.
            </p>
            <div className="cta" style={{ justifyContent: "center" }}>
              <button className="primary big" onClick={loadDemo} disabled={seeding}>
                {seeding ? "Loading…" : "✨ Load demo data"}
              </button>
              <button className="big" onClick={() => go("data")}>Add my own data →</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
