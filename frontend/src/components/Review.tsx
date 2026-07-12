import { useEffect, useState } from "react";
import { api, Settings } from "../api";

// The review gate's human half: approve the resolver's suggestion or override
// with an explicitly chosen factor. Unreviewed rows stay OUT of totals.
export default function Review({ settings, version, onChanged }:
    { settings: Settings; version: number; onChanged: () => void }) {
  const [queue, setQueue] = useState<any[]>([]);
  const [factors, setFactors] = useState<any[]>([]);
  const [overrideSel, setOverrideSel] = useState<Record<number, number>>({});
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    api.reviewQueue(settings).then(setQueue).catch((e) => setError(e.message));
    api.factors(settings).then(setFactors).catch(() => {});
  };
  useEffect(refresh, [settings.apiKey, settings.baseUrl, version]);

  const act = async (fn: () => Promise<any>) => {
    setError(null);
    try { await fn(); refresh(); onChanged(); } catch (e: any) { setError(e.message); }
  };

  return (
    <div className="panel">
      <h2>Mapping review queue</h2>
      {error && <p className="bad">{error}</p>}
      {queue.length === 0 ? (
        <p className="ok">Queue is empty — every mapped activity was an exact match or human-reviewed.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Activity</th><th>Qty</th><th>Suggestion</th>
              <th>Basis / confidence</th><th>Decision</th>
            </tr>
          </thead>
          <tbody>
            {queue.map((q) => {
              const sf = q.suggested_factor;
              return (
                <tr key={q.activity_id}>
                  <td>
                    #{q.activity_id} {q.date}<br />
                    <b>{q.category}</b>{q.subcategory ? `/${q.subcategory}` : ""} ({q.geo})
                    <div className="muted">{q.description}</div>
                  </td>
                  <td>{q.quantity} {q.unit}</td>
                  <td>
                    {sf ? (
                      <>
                        {sf.category}{sf.subcategory ? `/${sf.subcategory}` : ""} [{sf.geography}]<br />
                        <span className="muted">{sf.value} kgCO₂e/{sf.unit} · {sf.source} v{sf.version}</span>
                      </>
                    ) : <span className="muted">none</span>}
                  </td>
                  <td>
                    {q.mapping_basis}<br />
                    <span className="muted">confidence {q.mapping_confidence}</span>
                  </td>
                  <td>
                    <div className="row">
                      <button className="primary" disabled={!sf}
                              onClick={() => act(() => api.approve(settings, q.activity_id))}>
                        Approve
                      </button>
                      <select value={overrideSel[q.activity_id] || ""}
                              onChange={(e) => setOverrideSel({ ...overrideSel, [q.activity_id]: Number(e.target.value) })}>
                        <option value="">override with…</option>
                        {factors.map((f) => (
                          <option key={f.id} value={f.id}>
                            {f.cat}{f.subcat ? `/${f.subcat}` : ""} [{f.geo}] {f.value}/{f.unit}
                          </option>
                        ))}
                      </select>
                      <button disabled={!overrideSel[q.activity_id]}
                              onClick={() => act(() => api.override(settings, q.activity_id, overrideSel[q.activity_id]))}>
                        Override
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
