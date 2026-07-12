import { useEffect, useState } from "react";
import { api, Settings } from "../api";

const fmt = (v: number | null | undefined, d = 1) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: d });

export default function Dashboard({ settings, runId, onSelectRun, version, onChanged }: {
  settings: Settings; runId?: number; onSelectRun: (id?: number) => void;
  version: number; onChanged: () => void;
}) {
  const [runs, setRuns] = useState<any[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [gwpSet, setGwpSet] = useState("AR6");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.runs(settings).then(setRuns).catch((e) => setError(e.message));
    api.summary(settings, runId).then(setSummary).catch((e) => setError(e.message));
  }, [settings.apiKey, settings.baseUrl, runId, version]);

  const compute = async () => {
    setBusy(true); setError(null);
    try {
      const r = await api.run(settings, gwpSet);
      onSelectRun(r.run?.id);
      onChanged();
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };

  const cov = summary?.coverage;
  const s2 = summary?.scope2;
  const dq = summary?.data_quality;
  const ms = summary?.method_split;

  return (
    <>
      <div className="panel">
        <div className="row">
          <select value={gwpSet} onChange={(e) => setGwpSet(e.target.value)}>
            <option>AR6</option><option>AR5</option>
          </select>
          <button className="primary" onClick={compute} disabled={busy}>
            {busy ? "Computing…" : "New calculation run"}
          </button>
          <span className="muted">|</span>
          <label className="muted">Run</label>
          <select value={runId ?? ""} onChange={(e) => onSelectRun(e.target.value ? Number(e.target.value) : undefined)}>
            <option value="">latest</option>
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                #{r.id} · {r.gwp_set} · {r.created_at?.slice(0, 16)} · {fmt(r.total_co2e)} kg
              </option>
            ))}
          </select>
          <span className="muted">runs are immutable — recomputing creates a new one</span>
        </div>
        {error && <p className="bad">{error}</p>}
      </div>

      {summary?.run ? (
        <>
          {summary.partial && (
            <div className="panel blockers">
              <b className="bad">PARTIAL RUN</b> — excluded: {JSON.stringify(summary.partial_reasons)}
            </div>
          )}
          <div className="panel">
            <h2>Run #{summary.run.id} <span className="muted">{summary.run.gwp_set} · {summary.run.created_at}</span></h2>
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
        <div className="panel muted">No calculation run yet — upload activities, then run a calculation.</div>
      )}
    </>
  );
}
