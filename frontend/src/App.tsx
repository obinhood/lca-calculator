import { useState } from "react";
import { loadSettings, saveSettings, Settings } from "./api";
import Setup from "./components/Setup";
import Upload from "./components/Upload";
import Review from "./components/Review";
import Dashboard from "./components/Dashboard";
import Lineage from "./components/Lineage";
import Reports from "./components/Reports";

const TABS = [
  { key: "Dashboard", hint: "Totals, scopes, coverage & data quality" },
  { key: "Upload", hint: "Add activity data (CSV) or load the demo" },
  { key: "Review queue", hint: "Approve coarse factor matches" },
  { key: "Lineage", hint: "Trace every number back to its source" },
  { key: "Reports", hint: "Generate framework disclosures" },
] as const;
type Tab = (typeof TABS)[number]["key"];

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings());
  const [tab, setTab] = useState<Tab>("Dashboard");
  const [runId, setRunId] = useState<number | undefined>(undefined);
  // Bumped by Upload/Review/Dashboard actions so sibling panels refetch.
  const [version, setVersion] = useState(0);
  const [hasRun, setHasRun] = useState(false);   // drives the onboarding stepper
  const bump = () => setVersion((v) => v + 1);
  const update = (s: Settings) => { saveSettings(s); setSettings(s); };
  const go = (t: Tab) => setTab(t);

  const connected = !!settings.apiKey;

  return (
    <div>
      <div className="brand">
        <h1>🌿 Carbon Platform</h1>
        <span className="subtle">audit-grade GHG accounting &amp; disclosure</span>
      </div>

      {!connected ? (
        <>
          <div className="hero">
            <h2>Turn activity data into audit-grade climate disclosures</h2>
            <p className="lead">
              Upload your energy, travel and other activity data once. The platform builds an
              immutable, fully-traceable emissions inventory and renders it into 25+ reporting
              frameworks — SECR, CSRD/ESRS, ISSB S2, GRI, CDP, CBAM and more — each with a
              fail-closed “disclosure-ready” gate so you never file a number you can’t defend.
            </p>
            <div className="features">
              <div className="feature"><b>Every number traceable</b>
                <span>Immutable runs; each figure links back to its source activity and emission factor.</span></div>
              <div className="feature"><b>25+ frameworks, one inventory</b>
                <span>Generate many disclosures from a single dataset, grouped by purpose.</span></div>
              <div className="feature"><b>Honest by design</b>
                <span>Reports state exactly what they cover and block on missing data.</span></div>
            </div>
          </div>
          <Setup settings={settings} onChange={update} />
        </>
      ) : (
        <>
          <Setup settings={settings} onChange={update} />

          <div className="steps">
            <div className="step done"><span className="n">✓</span> Organisation</div>
            <div className={"step " + (tab === "Upload" && !hasRun ? "active" : hasRun ? "done" : "")}>
              <span className="n">{hasRun ? "✓" : "2"}</span> Add data</div>
            <div className={"step " + (hasRun ? "done" : "")}>
              <span className="n">{hasRun ? "✓" : "3"}</span> Calculation run</div>
            <div className={"step " + (tab === "Reports" ? "active" : "")}>
              <span className="n">4</span> Disclosures</div>
          </div>

          <div className="tabs">
            {TABS.map((t) => (
              <button key={t.key} className={t.key === tab ? "active" : ""}
                      onClick={() => setTab(t.key)} title={t.hint}>
                {t.key}
              </button>
            ))}
          </div>

          {tab === "Dashboard" && (
            <Dashboard settings={settings} runId={runId} onSelectRun={setRunId} version={version}
                       onChanged={() => { bump(); setHasRun(true); }} go={go} />
          )}
          {tab === "Upload" && (
            <Upload settings={settings} onChanged={() => { bump(); setHasRun(true); }} go={go} />
          )}
          {tab === "Review queue" && <Review settings={settings} version={version} onChanged={bump} />}
          {tab === "Lineage" && <Lineage settings={settings} runId={runId} version={version} />}
          {tab === "Reports" && <Reports settings={settings} runId={runId} />}
        </>
      )}
    </div>
  );
}
