import { useState } from "react";
import { loadSettings, saveSettings, Settings } from "./api";
import Setup from "./components/Setup";
import Upload from "./components/Upload";
import Review from "./components/Review";
import Dashboard from "./components/Dashboard";
import Lineage from "./components/Lineage";
import Reports from "./components/Reports";

const TABS = ["Dashboard", "Upload", "Review queue", "Lineage", "Reports"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings());
  const [tab, setTab] = useState<Tab>("Dashboard");
  const [runId, setRunId] = useState<number | undefined>(undefined);
  // Bumped by Upload/Review/Dashboard actions so sibling panels refetch.
  const [version, setVersion] = useState(0);
  const bump = () => setVersion((v) => v + 1);

  const update = (s: Settings) => { saveSettings(s); setSettings(s); };

  return (
    <div>
      <h1>🌿 Carbon Platform <span className="muted">audit-grade GHG accounting</span></h1>
      <Setup settings={settings} onChange={update} />
      {settings.apiKey ? (
        <>
          <div className="tabs">
            {TABS.map((t) => (
              <button key={t} className={t === tab ? "active" : ""} onClick={() => setTab(t)}>
                {t}
              </button>
            ))}
          </div>
          {tab === "Dashboard" && (
            <Dashboard settings={settings} runId={runId} onSelectRun={setRunId}
                       version={version} onChanged={bump} />
          )}
          {tab === "Upload" && <Upload settings={settings} onChanged={bump} />}
          {tab === "Review queue" && <Review settings={settings} version={version} onChanged={bump} />}
          {tab === "Lineage" && <Lineage settings={settings} runId={runId} version={version} />}
          {tab === "Reports" && <Reports settings={settings} runId={runId} />}
        </>
      ) : (
        <div className="panel muted">
          Enter your API key (or register an organisation) to begin.
        </div>
      )}
    </div>
  );
}
