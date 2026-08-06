import { useState } from "react";
import { api, Settings } from "../api";

// Signed-out entry point: explain the product, then get the user an organisation + API key.
export default function Landing({ settings, onChange, notice }:
    { settings: Settings; onChange: (s: Settings) => void; notice?: string | null }) {
  const [orgName, setOrgName] = useState("");
  const [issuedKey, setIssuedKey] = useState<string | null>(null);
  const [existingKey, setExistingKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  const register = async () => {
    setBusy(true); setError(null);
    try { setIssuedKey((await api.register(settings, orgName)).api_key); }
    catch (e: any) { setError(e.message); }
    setBusy(false);
  };

  return (
    <div className="landing">
      <div className="landing-hero">
        <div style={{ fontSize: 40 }}>🌿</div>
        <h1>Measure your carbon footprint. Report it with confidence.</h1>
        <p>
          Upload your energy, travel and waste data once. We build an auditable emissions
          inventory and turn it into the disclosures you actually have to file — SECR, CSRD,
          ISSB, GRI, CDP, CBAM and 20 more.
        </p>
      </div>

      {notice && (
        <div className="callout warn" style={{ marginBottom: 20 }}>{notice}</div>
      )}

      <div className="features">
        <div className="feature"><div className="ico">🔗</div><b>Every number traceable</b>
          <span>Each figure links back to the source record and emission factor behind it.</span></div>
        <div className="feature"><div className="ico">📄</div><b>27 frameworks, one dataset</b>
          <span>Enter your data once and generate any disclosure from the same inventory.</span></div>
        <div className="feature"><div className="ico">🛡️</div><b>It tells you when it can't</b>
          <span>Reports block instead of guessing, and say exactly what's missing.</span></div>
        <div className="feature"><div className="ico">⚡</div><b>See it in one click</b>
          <span>Load a sample dataset and explore a finished footprint immediately.</span></div>
      </div>

      <div className="card">
        <h2>Create your workspace</h2>
        <p className="lead" style={{ margin: "6px 0 16px" }}>
          Your organisation name is all we need. You'll get an API key — that key is your
          login, so keep it somewhere safe.
        </p>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="field" style={{ flex: 1, minWidth: 240 }}>Organisation name
            <input placeholder="e.g. Acme Ltd" value={orgName}
                   onChange={(e) => setOrgName(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && orgName && !busy) register(); }} />
          </label>
          <button className="primary big" onClick={register} disabled={!orgName || busy}>
            {busy ? "Creating…" : "Create workspace →"}
          </button>
        </div>

        {issuedKey && (
          <div className="callout info" style={{ marginTop: 16 }}>
            <b>Save your API key — it is shown only once</b>
            <div style={{ margin: "8px 0" }}><code>{issuedKey}</code></div>
            <button className="primary" onClick={() => onChange({ ...settings, apiKey: issuedKey })}>
              Continue to your workspace →
            </button>
          </div>
        )}
        {error && <p className="bad" style={{ marginBottom: 0 }}>{error}</p>}

        <div className="divider" />
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="field" style={{ flex: 1, minWidth: 240 }}>Already have an API key?
            <input type="password" placeholder="paste your key" value={existingKey}
                   onChange={(e) => setExistingKey(e.target.value)}
                   onKeyDown={(e) => {
                     if (e.key === "Enter" && existingKey) onChange({ ...settings, apiKey: existingKey });
                   }} />
          </label>
          <button onClick={() => onChange({ ...settings, apiKey: existingKey })}
                  disabled={!existingKey}>Sign in</button>
        </div>

        <p className="muted" style={{ marginBottom: 0 }}>
          <button className="link" onClick={() => setAdvanced(!advanced)}>
            {advanced ? "▾" : "▸"} Advanced — API endpoint
          </button>
          {advanced && (
            <span className="row" style={{ marginTop: 8 }}>
              <input style={{ width: 300 }} value={settings.baseUrl}
                     onChange={(e) => onChange({ ...settings, baseUrl: e.target.value })} />
              <span className="muted">Defaults to this site. Only change it to point at another server.</span>
            </span>
          )}
        </p>
      </div>
    </div>
  );
}
