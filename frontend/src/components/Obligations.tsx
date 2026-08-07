import { useEffect, useState } from "react";
import { api, Settings } from "../api";
import type { Page } from "../App";

type Test = {
  metric: string; operator: string; value: number; unit: string;
  actual: number | null; passed: boolean | null; distance?: number;
  note?: string; why?: string; conversion?: string;
};
type Result = {
  framework: string; kind: string; label: string; verdict: string;
  jurisdiction?: string; citation?: string; summary?: string; reason?: string;
  first_reporting_year?: number; phase_in?: string; status?: string;
  enforceable?: boolean; status_note?: string; confidence?: string;
  model_limitations?: string; tests: Test[]; missing_inputs: string[];
};
type Assessment = {
  entity_profile: Record<string, any>;
  profile_complete: boolean; profile_missing: string[];
  results: Result[]; counts: Record<string, number>;
  required_but_not_enforceable: string[]; caveats: string[];
};

// Ordered so the answers that demand action come first, and the "nothing to do here"
// buckets sink to the bottom. `cannot_determine` sits second on purpose: an unanswered
// obligation is more urgent than a settled one, not less.
const GROUPS: { verdict: string; title: string; blurb: string; tone: string }[] = [
  { verdict: "required", tone: "bad", title: "You must file these",
    blurb: "You are caught by these on the profile you have given us — by size, or by " +
           "listing status where the regime catches listed entities at any size." },
  { verdict: "cannot_determine", tone: "warn", title: "Can't tell yet",
    blurb: "These need more of your entity profile. An unknown is not a 'no' — until " +
           "you fill these in, these obligations are unanswered rather than ruled out." },
  { verdict: "activity_based", tone: "warn", title: "Depends on what you do",
    blurb: "Compelled by a specific activity or entity type rather than your size — " +
           "importing certain goods, running an installation, being a regulated firm." },
  { verdict: "voluntary", tone: "", title: "Voluntary",
    blurb: "Not compelled by any law this tool models. A customer, investor or " +
           "procurement gate may still require one — and some become binding where a " +
           "jurisdiction adopts them, which each entry notes." },
  { verdict: "not_a_filing", tone: "", title: "Methodology standards",
    blurb: "Standards you APPLY when preparing a disclosure, rather than file in their " +
           "own right. Some are separately written into law elsewhere — where that is " +
           "so, the entry says which regime carries the duty." },
  { verdict: "not_required", tone: "ok", title: "Below the threshold",
    blurb: "On the profile you have given us, a test was applied and you are under it. " +
           "Check the tests behind 'Why?' — a threshold you sit close to is worth " +
           "re-checking as you grow." },
  { verdict: "out_of_territory", tone: "ok", title: "Outside their territory",
    blurb: "On the territories you have listed, these regimes do not reach you. Not the " +
           "same as being too small — operating somewhere new can bring them into play." },
];

const METRIC_LABEL: Record<string, string> = {
  employees: "Employees", annual_turnover: "Turnover",
  balance_sheet_total: "Balance sheet total",
};

const fmt = (v: number, unit: string) =>
  unit === "headcount" ? v.toLocaleString()
    : `${unit} ${v >= 1e6 ? (v / 1e6).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "m" : v.toLocaleString()}`;

function TestRow({ t }: { t: Test }) {
  const icon = t.passed === true ? "✓" : t.passed === false ? "✗" : "?";
  return (
    <div className="row" style={{ gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
      <span style={{ width: 14 }}>{icon}</span>
      <span style={{ minWidth: 150 }}>{METRIC_LABEL[t.metric] || t.metric}</span>
      <code>{t.operator} {fmt(t.value, t.unit)}</code>
      <span className="muted">
        {t.actual === null ? (t.why || "not provided")
          : `you: ${fmt(t.actual, t.unit)}${
              t.distance !== undefined
                ? ` (${t.distance >= 0 ? "+" : "−"}${fmt(Math.abs(t.distance), t.unit)})`
                : ""}`}
      </span>
      {t.conversion && (
        <span className="muted" style={{ flexBasis: "100%", paddingLeft: 22 }}>
          {t.conversion}
        </span>
      )}
      {t.note && <span className="muted" style={{ flexBasis: "100%", paddingLeft: 22 }}>{t.note}</span>}
    </div>
  );
}

function Card({ r }: { r: Result }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="notice" style={{ marginTop: 8 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <div>
          <b>{r.label}</b>
          {r.jurisdiction && <span className="badge" style={{ marginLeft: 8 }}>{r.jurisdiction}</span>}
          {r.enforceable === false && (
            <span className="badge warn" style={{ marginLeft: 6 }}>not enforceable</span>
          )}
          {r.confidence === "medium" && (
            <span className="badge" style={{ marginLeft: 6 }}>medium confidence</span>
          )}
        </div>
        <button onClick={() => setOpen(!open)}>{open ? "Hide" : "Why?"}</button>
      </div>
      {r.summary && <div className="muted" style={{ marginTop: 4 }}>{r.summary}</div>}
      {r.first_reporting_year && (
        <div className="muted" style={{ marginTop: 4 }}>
          First reporting year: <b>{r.first_reporting_year}</b>
          {r.enforceable === false && " (currently suspended)"}
        </div>
      )}

      {open && (
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--line)" }}>
          {r.reason && <div style={{ marginBottom: 8 }}>{r.reason}</div>}
          {r.tests.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <div className="muted" style={{ marginBottom: 4 }}>Tests applied:</div>
              {r.tests.map((t, i) => <TestRow key={i} t={t} />)}
            </div>
          )}
          {r.status_note && (
            <div className="muted" style={{ marginBottom: 8 }}><b>Status:</b> {r.status_note}</div>
          )}
          {r.phase_in && (
            <div className="muted" style={{ marginBottom: 8 }}><b>Timing:</b> {r.phase_in}</div>
          )}
          {r.model_limitations && (
            <div className="notice warn" style={{ marginBottom: 8 }}>
              <b>What this check does not test:</b> {r.model_limitations}
            </div>
          )}
          {r.citation && (
            <div className="muted" style={{ fontSize: "0.9em" }}>
              <b>Source:</b> {r.citation}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function Obligations({ settings, go, version }:
    { settings: Settings; go: (p: Page) => void; version: number }) {
  const [a, setA] = useState<Assessment | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.applicability(settings).then(setA).catch((e) => setErr(String(e.message || e)));
  }, [settings.apiKey, settings.baseUrl, version]);

  if (err) return <div className="card"><div className="notice warn">{err}</div></div>;
  if (!a) return <div className="card"><div className="muted">Assessing…</div></div>;

  const byVerdict = (v: string) => a.results.filter((r) => r.verdict === v);

  return (
    <>
      <div className="card">
        <h2>Your obligations at a glance</h2>
        <div className="kpis" style={{ marginTop: 12 }}>
          <div className="kpi"><div className="v">{a.counts.required}</div>
            <div className="l">you must file</div></div>
          <div className="kpi"><div className="v">{a.counts.cannot_determine}</div>
            <div className="l">unanswered — not ruled out</div></div>
          <div className="kpi"><div className="v">{a.counts.activity_based}</div>
            <div className="l">depend on your activity</div></div>
          <div className="kpi"><div className="v">{a.counts.voluntary}</div>
            <div className="l">voluntary</div></div>
        </div>

        {!a.profile_complete && (
          <div className="notice warn" style={{ marginTop: 14 }}>
            <b>{a.counts.cannot_determine} obligation(s) can't be assessed yet.</b>
            <div style={{ marginTop: 4 }}>
              Missing: {a.profile_missing.join(", ")}. These are <i>unanswered</i>, not ruled
              out — an unknown is never a "no".
            </div>
            <button className="primary" style={{ marginTop: 10 }} onClick={() => go("settings")}>
              Complete your entity profile
            </button>
          </div>
        )}

        {a.caveats.map((c, i) => (
          <div key={i} className="muted" style={{ marginTop: 10, fontSize: "0.92em" }}>{c}</div>
        ))}
      </div>

      {GROUPS.map((g) => {
        const rows = byVerdict(g.verdict);
        if (!rows.length) return null;
        return (
          <div className="card" key={g.verdict}>
            <h2>
              {g.title}
              <span className={"badge " + g.tone} style={{ marginLeft: 8 }}>{rows.length}</span>
            </h2>
            <p className="lead" style={{ marginTop: 6 }}>{g.blurb}</p>
            {rows.map((r) => <Card key={r.framework} r={r} />)}
          </div>
        );
      })}
    </>
  );
}
