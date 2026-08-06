import { useEffect, useState } from "react";
import { api, loadSettings, saveSettings, Settings } from "./api";
import Landing from "./components/Landing";
import Home from "./components/Home";
import Upload from "./components/Upload";
import Review from "./components/Review";
import Dashboard from "./components/Dashboard";
import Lineage from "./components/Lineage";
import Reports from "./components/Reports";
import SettingsPage from "./components/SettingsPage";

export type Page =
  | "home" | "data" | "review" | "footprint" | "audit" | "reports" | "settings";

const NAV: { group: string; items: { key: Page; label: string; ico: string }[] }[] = [
  { group: "Overview", items: [
      { key: "home", label: "Get started", ico: "🏠" } ] },
  { group: "Your data", items: [
      { key: "data", label: "Activity data", ico: "📥" },
      { key: "review", label: "Review queue", ico: "🔍" } ] },
  { group: "Carbon footprint", items: [
      { key: "footprint", label: "Footprint", ico: "📊" },
      { key: "audit", label: "Audit trail", ico: "🧾" } ] },
  { group: "Disclosures", items: [
      { key: "reports", label: "Reports", ico: "📄" } ] },
];

const TITLES: Record<Page, { title: string; sub: string }> = {
  home: { title: "Get started", sub: "Your setup progress and what to do next" },
  data: { title: "Activity data", sub: "Upload the energy, travel and waste data behind your footprint" },
  review: { title: "Review queue", sub: "Approve factor matches that need a human decision" },
  footprint: { title: "Carbon footprint", sub: "Your emissions inventory by scope, with coverage and data quality" },
  audit: { title: "Audit trail", sub: "Trace every number back to its source record and emission factor" },
  reports: { title: "Reports", sub: "Generate a disclosure for any framework from your inventory" },
  settings: { title: "Settings", sub: "Organisation, API key and connection" },
};

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings());
  const [page, setPage] = useState<Page>("home");
  const [runId, setRunId] = useState<number | undefined>(undefined);
  const [version, setVersion] = useState(0);            // bump -> siblings refetch
  const [reviewCount, setReviewCount] = useState(0);
  const [hasData, setHasData] = useState(false);
  const [hasRun, setHasRun] = useState(false);

  const bump = () => setVersion((v) => v + 1);
  const update = (s: Settings) => { saveSettings(s); setSettings(s); };

  // Drives the sidebar badge + the Get-started checklist.
  useEffect(() => {
    if (!settings.apiKey) return;
    api.runs(settings)
      .then((rs: any[]) => {
        setHasRun(rs.length > 0);
        if (rs.length > 0) setHasData(true);
      })
      .catch(() => {});
    api.reviewQueue(settings)
      .then((q: any[]) => { setReviewCount(q.length); if (q.length) setHasData(true); })
      .catch(() => {});
  }, [settings.apiKey, settings.baseUrl, version]);

  if (!settings.apiKey) return <Landing settings={settings} onChange={update} />;

  const t = TITLES[page];
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="logo">
          <span className="mark">🌿</span>
          <span><b>Carbon Platform</b><span>audit-grade accounting</span></span>
        </div>

        {NAV.map((g) => (
          <div key={g.group}>
            <div className="nav-label">{g.group}</div>
            {g.items.map((it) => (
              <button key={it.key}
                      className={"nav-item" + (page === it.key ? " active" : "")}
                      onClick={() => setPage(it.key)}>
                <span className="ico">{it.ico}</span>{it.label}
                {it.key === "review" && reviewCount > 0 && (
                  <span className="count">{reviewCount}</span>
                )}
              </button>
            ))}
          </div>
        ))}

        <div className="sidebar-foot">
          <button className={"nav-item" + (page === "settings" ? " active" : "")}
                  onClick={() => setPage("settings")}>
            <span className="ico">⚙️</span>Settings
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div>
            <h1>{t.title}</h1>
            <div className="sub">{t.sub}</div>
          </div>
          <div className="spacer" />
          {hasRun && page !== "home" && (
            <span className="badge brand">{runId ? `Run #${runId}` : "Latest run"}</span>
          )}
        </header>

        <div className="content">
          {page === "home" && (
            <Home settings={settings} go={setPage} version={version}
                  hasData={hasData} hasRun={hasRun} reviewCount={reviewCount}
                  onChanged={() => { setHasData(true); setHasRun(true); bump(); }}
                  onSelectRun={setRunId} />
          )}
          {page === "data" && (
            <Upload settings={settings} go={setPage}
                    onChanged={() => { setHasData(true); bump(); }} />
          )}
          {page === "review" && <Review settings={settings} version={version} onChanged={bump} />}
          {page === "footprint" && (
            <Dashboard settings={settings} runId={runId} onSelectRun={setRunId} version={version}
                       onChanged={() => { setHasRun(true); bump(); }} go={setPage} />
          )}
          {page === "audit" && <Lineage settings={settings} runId={runId} version={version} />}
          {page === "reports" && <Reports settings={settings} runId={runId} go={setPage} hasRun={hasRun} />}
          {page === "settings" && <SettingsPage settings={settings} onChange={update} />}
        </div>
      </div>
    </div>
  );
}
