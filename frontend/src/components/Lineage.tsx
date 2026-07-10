import { useEffect, useState } from "react";
import { api, Settings } from "../api";

// The assurer drill-down: any figure -> source activity -> frozen calculation
// detail (pinned factor, unit conversion, per-gas GWPs, market allocation,
// spend normalization, DQ) — two clicks, no live-state joins.
export default function Lineage({ settings, runId, version }:
    { settings: Settings; runId?: number; version: number }) {
  const [runs, setRuns] = useState<any[]>([]);
  const [selected, setSelected] = useState<number | undefined>(runId);
  const [data, setData] = useState<any>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.runs(settings).then((rs) => {
      setRuns(rs);
      if (!selected && rs.length) setSelected(rs[0].id);
    }).catch((e) => setError(e.message));
  }, [settings.apiKey, settings.baseUrl, version]);

  useEffect(() => {
    if (!selected) return;
    api.lineage(settings, selected).then(setData).catch((e) => setError(e.message));
  }, [selected, settings.apiKey, version]);

  return (
    <div className="panel">
      <div className="row">
        <h2 style={{ margin: 0 }}>Lineage explorer</h2>
        <select value={selected ?? ""} onChange={(e) => setSelected(Number(e.target.value))}>
          {runs.map((r) => (
            <option key={r.id} value={r.id}>run #{r.id} · {r.gwp_set} · {r.created_at?.slice(0, 16)}</option>
          ))}
        </select>
      </div>
      {error && <p className="bad">{error}</p>}
      {data && (
        <>
          <p className="muted">
            Immutable run #{data.run.id} · {data.run.gwp_set} · location {data.run.total_co2e?.toFixed(2)} kg ·
            market {data.run.total_co2e_market?.toFixed(2)} kg · biogenic {data.run.total_biogenic_co2e?.toFixed(2)} kg.
            Every line below carries the calculation detail frozen at compute time.
          </p>
          <table>
            <thead>
              <tr><th>Line</th><th>Activity</th><th>Scope</th><th>Method</th><th>kgCO₂e</th><th>Calc</th><th></th></tr>
            </thead>
            <tbody>
              {data.line_items.map((li: any) => (
                <>
                  <tr key={li.id}>
                    <td>#{li.id}</td>
                    <td>
                      #{li.activity.id} {li.activity.category}
                      {li.activity.subcategory ? `/${li.activity.subcategory}` : ""} ·{" "}
                      {li.activity.quantity} {li.activity.unit} ({li.activity.date})
                      <div className="muted">{li.activity.source_file}</div>
                    </td>
                    <td>{li.scope}</td>
                    <td>{li.method}</td>
                    <td>{li.co2e?.toFixed(4)}</td>
                    <td>
                      <span className="badge">{li.detail.calc_method || li.detail.method_basis || "—"}</span>{" "}
                      {li.detail.method_type && <span className="badge">{li.detail.method_type}</span>}
                    </td>
                    <td>
                      <button onClick={() => setOpen(open === li.id ? null : li.id)}>
                        {open === li.id ? "hide" : "trace"}
                      </button>
                    </td>
                  </tr>
                  {open === li.id && (
                    <tr>
                      <td colSpan={7}>
                        <pre className="detail">{JSON.stringify(li.detail, null, 2)}</pre>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
          {data.exclusions.length > 0 && (
            <>
              <h2 style={{ marginTop: 14 }} className="warn">Excluded from this run</h2>
              <table>
                <tbody>
                  {data.exclusions.map((x: any, i: number) => (
                    <tr key={i}><td>activity #{x.activity_id}</td><td className="bad">{x.error}</td></tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </div>
  );
}
