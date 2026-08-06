import { useEffect, useState } from "react";
import { api, Settings } from "../api";
import type { Page } from "../App";

const fmt = (v: number | null | undefined, d = 1) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: d });

// The guided landing INSIDE the app: a 4-step checklist that always says what to do next,
// plus a snapshot once there is a footprint.
export default function Home({ settings, go, version, hasData, hasRun, reviewCount,
                              onChanged, onSelectRun }: {
  settings: Settings; go: (p: Page) => void; version: number;
  hasData: boolean; hasRun: boolean; reviewCount: number;
  onChanged: () => void; onSelectRun: (id?: number) => void;
}) {
  const [summary, setSummary] = useState<any>(null);
  const [seeding, setSeeding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!hasRun) { setSummary(null); return; }
    api.summary(settings).then(setSummary).catch(() => {});
  }, [settings.apiKey, settings.baseUrl, version, hasRun]);

  const loadDemo = async () => {
    setSeeding(true); setError(null);
    try { const r = await api.seedDemo(settings); onSelectRun(r.run_id); onChanged(); }
    catch (e: any) { setError(e.message); }
    setSeeding(false);
  };

  // step state: done | next (the first incomplete one) | todo
  const steps = [
    { done: true, title: "Create your workspace",
      body: "Your organisation is set up and your API key is active.", action: null as any },
    { done: hasData, title: "Add your activity data",
      body: "Upload a CSV of energy, travel, fuel and waste — or load our sample dataset.",
      action: <button className="primary" onClick={() => go("data")}>Add data →</button> },
    { done: hasRun, title: "Calculate your footprint",
      body: "We match every line to an emission factor and produce an immutable run.",
      action: <button className="primary" onClick={() => go("footprint")}>Go to footprint →</button> },
    { done: false, title: "Generate a disclosure",
      body: "Pick a framework — SECR, CSRD, ISSB, CDP and 23 more — and generate the report.",
      action: <button className="primary" onClick={() => go("reports")}>Browse reports →</button> },
  ];
  const nextIdx = steps.findIndex((s) => !s.done);

  return (
    <>
      {!hasRun && (
        <div className="card" style={{ background: "var(--brand-soft)", borderColor: "transparent" }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h2>Not sure where to start?</h2>
              <p className="lead" style={{ margin: "4px 0 0", color: "var(--brand-ink)" }}>
                Load a sample company's data and get a complete footprint and reports in one click.
                You can clear it and add your own data later.
              </p>
            </div>
            <button className="primary big" onClick={loadDemo} disabled={seeding}>
              {seeding ? "Loading…" : "✨ Load sample data"}
            </button>
          </div>
          {error && <p className="bad" style={{ marginBottom: 0 }}>{error}</p>}
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <h2>Your setup</h2>
          <div className="spacer" />
          <span className="muted">{steps.filter((s) => s.done).length} of {steps.length} complete</span>
        </div>
        <div className="checklist">
          {steps.map((s, i) => (
            <div key={s.title}
                 className={"check " + (s.done ? "done" : i === nextIdx ? "next" : "")}>
              <div className="n">{s.done ? "✓" : i + 1}</div>
              <div className="body">
                <b>{s.title}</b>
                <span>{s.body}</span>
              </div>
              {!s.done && i === nextIdx && s.action}
            </div>
          ))}
        </div>
      </div>

      {reviewCount > 0 && (
        <div className="callout warn" style={{ marginBottom: 16 }}>
          <b>{reviewCount} item{reviewCount === 1 ? "" : "s"} need your review.</b>{" "}
          They are excluded from your totals until you approve a factor match.{" "}
          <button className="link" onClick={() => go("review")}>Open the review queue →</button>
        </div>
      )}

      {summary?.run && (
        <div className="card">
          <div className="card-head">
            <h2>Your footprint</h2>
            <div className="spacer" />
            <button onClick={() => go("footprint")}>See full breakdown →</button>
          </div>
          <div className="kpis">
            <div className="kpi">
              <div className="v">{fmt((summary.total_co2e || 0) / 1000, 2)}</div>
              <div className="l">tCO₂e total (location-based)</div>
            </div>
            {(summary.by_scope || []).map((r: any) => (
              <div className="kpi" key={r.scope}>
                <div className="v">{fmt(r.co2e / 1000, 2)}</div>
                <div className="l">tCO₂e Scope {r.scope}</div>
              </div>
            ))}
            <div className="kpi">
              <div className="v">{summary.coverage ? `${summary.coverage.coverage_pct}%` : "—"}</div>
              <div className="l">data coverage</div>
              <div className="bar"><div style={{ width: `${summary.coverage?.coverage_pct || 0}%` }} /></div>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <h2>How this works</h2>
        <div className="grid-2" style={{ marginTop: 12 }}>
          <div>
            <b>1 · Activity data in</b>
            <p className="muted">
              You provide what your business consumed — kWh of electricity, litres of fuel,
              passenger-km flown, kg of waste. Each row is matched to a published emission factor.
            </p>
          </div>
          <div>
            <b>2 · An immutable run</b>
            <p className="muted">
              Calculating freezes a snapshot: totals, factors and coverage. Recalculating creates a
              NEW run, so a filed number can always be reproduced.
            </p>
          </div>
          <div>
            <b>3 · Disclosures out</b>
            <p className="muted">
              Every framework reads that same run, so your SECR, CSRD and CDP numbers agree by
              construction rather than by reconciliation.
            </p>
          </div>
          <div>
            <b>4 · Honest gates</b>
            <p className="muted">
              A report is only “disclosure-ready” when it can stand up. Otherwise it lists exactly
              what's missing instead of quietly filling a gap.
            </p>
          </div>
        </div>
      </div>
    </>
  );
}
