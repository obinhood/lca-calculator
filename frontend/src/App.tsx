import { useEffect, useState } from "react";
import { api, isAuthError, loadSettings, saveSettings, Settings } from "./api";
import Landing from "./components/Landing";
import SignIn from "./components/SignIn";
import Home, { SESSION_GONE } from "./components/Home";
import Upload from "./components/Upload";
import Review from "./components/Review";
import Dashboard from "./components/Dashboard";
import Lineage from "./components/Lineage";
import Reports from "./components/Reports";
import Obligations from "./components/Obligations";
import SettingsPage from "./components/SettingsPage";

export type Page =
  | "home" | "data" | "review" | "footprint" | "audit" | "reports" | "obligations"
  | "settings";

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
      { key: "obligations", label: "What applies to you", ico: "⚖️" },
      { key: "reports", label: "Reports", ico: "📄" } ] },
];

const TITLES: Record<Page, { title: string; sub: string }> = {
  home: { title: "Get started", sub: "Your setup progress and what to do next" },
  data: { title: "Activity data", sub: "Upload the energy, travel and waste data behind your footprint" },
  review: { title: "Review queue", sub: "Approve factor matches that need a human decision" },
  footprint: { title: "Carbon footprint", sub: "Your emissions inventory by scope, with coverage and data quality" },
  audit: { title: "Audit trail", sub: "Trace every number back to its source record and emission factor" },
  obligations: { title: "What applies to you", sub: "Which disclosure regimes compel a filing from your entity, and which merely apply" },
  reports: { title: "Reports", sub: "Generate a disclosure for any framework from your inventory" },
  settings: { title: "Settings", sub: "Organisation, API key and connection" },
};

// Signed-out routing. The marketing page and the auth screen are separate destinations —
// a visitor reading about the product should not be looking at a credential field, and
// someone returning to sign in should not have to scroll past a pitch. The hash keeps
// /#/signin linkable and makes the browser back button behave.
type View = "landing" | "signin";
const viewFromHash = (): View =>
  typeof window !== "undefined" && window.location.hash.startsWith("#/signin")
    ? "signin" : "landing";

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings());
  const [view, setView] = useState<View>(viewFromHash);
  const [authMode, setAuthMode] = useState<"signin" | "create">("signin");
  const [page, setPage] = useState<Page>("home");
  const [runId, setRunId] = useState<number | undefined>(undefined);
  const [version, setVersion] = useState(0);            // bump -> siblings refetch
  const [reviewCount, setReviewCount] = useState(0);
  const [hasData, setHasData] = useState(false);
  const [hasRun, setHasRun] = useState(false);

  const [notice, setNotice] = useState<string | null>(null);

  const bump = () => setVersion((v) => v + 1);
  const update = (s: Settings) => { saveSettings(s); setSettings(s); };

  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const goAuth = (mode: "signin" | "create") => {
    setAuthMode(mode);
    window.location.hash = "#/signin";
    setView("signin");
  };
  const goLanding = () => {
    // replaceState rather than clearing the hash, so leaving the auth screen does not
    // push an extra entry the back button has to climb through.
    history.replaceState(null, "", window.location.pathname + window.location.search);
    // Clearing the notice matters: a dead key PINS the user to the auth screen so the
    // explanation has somewhere to live, and without this "Back to site" would be a
    // button that visibly does nothing.
    setNotice(null);
    setView("landing");
  };

  // A stored key can be DEAD — most often because the demo database was reset by a redeploy,
  // so the organisation it belonged to no longer exists. Without this the app would sail past
  // the sign-in screen on a saved key and then fail every action with no explanation.
  const signOut = (why?: string) => {
    saveSettings({ ...settings, apiKey: "" });
    setSettings({ ...settings, apiKey: "" });
    setNotice(why || null);
  };

  // Drives the sidebar badge + the Get-started checklist, and validates the key.
  useEffect(() => {
    if (!settings.apiKey) return;
    api.runs(settings)
      .then((rs: any[]) => {
        setHasRun(rs.length > 0);
        if (rs.length > 0) setHasData(true);
      })
      .catch((e) => {
        if (isAuthError(e)) {
          signOut(SESSION_GONE);
        }
      });
    api.reviewQueue(settings)
      .then((q: any[]) => { setReviewCount(q.length); if (q.length) setHasData(true); })
      .catch(() => {});
  }, [settings.apiKey, settings.baseUrl, version]);

  if (!settings.apiKey) {
    // A dead key drops the user straight onto the auth screen with the reason, rather
    // than onto the marketing page, where the explanation would have nowhere to live.
    if (view === "signin" || notice) {
      return <SignIn settings={settings} onChange={update} onBack={goLanding}
                     notice={notice} initialMode={authMode} />;
    }
    return <Landing onSignIn={() => goAuth("signin")} onGetStarted={() => goAuth("create")} />;
  }

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
                  onSelectRun={setRunId} onAuthError={signOut} />
          )}
          {page === "data" && (
            <Upload settings={settings} go={setPage} onAuthError={signOut}
                    onChanged={() => { setHasData(true); bump(); }} />
          )}
          {page === "review" && <Review settings={settings} version={version} onChanged={bump} />}
          {page === "footprint" && (
            <Dashboard settings={settings} runId={runId} onSelectRun={setRunId} version={version}
                       onChanged={() => { setHasRun(true); bump(); }} go={setPage}
                       onAuthError={signOut} />
          )}
          {page === "audit" && <Lineage settings={settings} runId={runId} version={version} />}
          {page === "obligations" && <Obligations settings={settings} go={setPage} version={version} />}
          {page === "reports" && <Reports settings={settings} runId={runId} go={setPage} hasRun={hasRun} />}
          {page === "settings" && <SettingsPage settings={settings} onChange={update} onSaved={bump} />}
        </div>
      </div>
    </div>
  );
}
