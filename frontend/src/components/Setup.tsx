import { useState } from "react";
import { api, Settings } from "../api";

export default function Setup({ settings, onChange }:
    { settings: Settings; onChange: (s: Settings) => void }) {
  const [orgName, setOrgName] = useState("");
  const [issuedKey, setIssuedKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const connected = !!settings.apiKey;

  const register = async () => {
    setError(null);
    try {
      const r = await api.register(settings, orgName);
      setIssuedKey(r.api_key);
    } catch (e: any) { setError(e.message); }
  };

  const advancedRow = (
    <p className="muted" style={{ marginTop: 8 }}>
      <a style={{ cursor: "pointer", color: "var(--link)" }} onClick={() => setAdvanced(!advanced)}>
        {advanced ? "▾" : "▸"} Advanced (API endpoint)
      </a>
      {advanced && (
        <span className="row" style={{ marginTop: 6 }}>
          <label className="muted">API base URL</label>
          <input style={{ width: 260 }} value={settings.baseUrl}
                 onChange={(e) => onChange({ ...settings, baseUrl: e.target.value })} />
          <span className="muted">defaults to this site — only change to point at another server</span>
        </span>
      )}
    </p>
  );

  // --- Connected: a slim status bar ---
  if (connected) {
    return (
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="ok">● Connected</span>
          <span className="row">
            <span className="muted">API key</span>
            <code>••••{settings.apiKey.slice(-4)}</code>
            <button className="ghost" onClick={() => onChange({ ...settings, apiKey: "" })}>
              Sign out
            </button>
          </span>
        </div>
        {advancedRow}
      </div>
    );
  }

  // --- Signed out: a register / sign-in card ---
  return (
    <div className="panel">
      <h2>Get started</h2>
      <p className="help" style={{ marginBottom: 12 }}>
        Create an <b>organisation</b> to get an API key — that key is your login and grants full
        access to your own data and every report. Already have a key? Paste it on the right.
      </p>
      <div className="row">
        <label className="field">Organisation name
          <input style={{ width: 220 }} placeholder="e.g. Acme Ltd" value={orgName}
                 onChange={(e) => setOrgName(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && orgName) register(); }} />
        </label>
        <button className="primary big" onClick={register} disabled={!orgName}>Create organisation</button>
        <span className="muted">or</span>
        <label className="field">Existing API key
          <input style={{ width: 300 }} type="password" placeholder="paste X-API-Key"
                 value={settings.apiKey}
                 onChange={(e) => onChange({ ...settings, apiKey: e.target.value })} />
        </label>
      </div>
      {issuedKey && (
        <div className="help" style={{ marginTop: 12, borderColor: "var(--accent)" }}>
          <b className="warn">Save this API key — it is shown only once:</b>
          <div style={{ margin: "6px 0" }}><code>{issuedKey}</code></div>
          <button className="primary" onClick={() => {
            onChange({ ...settings, apiKey: issuedKey }); setIssuedKey(null);
          }}>Continue →</button>
        </div>
      )}
      {error && <p className="bad">{error}</p>}
      {advancedRow}
    </div>
  );
}
