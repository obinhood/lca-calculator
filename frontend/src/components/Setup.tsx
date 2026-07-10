import { useState } from "react";
import { api, Settings } from "../api";

export default function Setup({ settings, onChange }:
    { settings: Settings; onChange: (s: Settings) => void }) {
  const [orgName, setOrgName] = useState("");
  const [issuedKey, setIssuedKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const register = async () => {
    setError(null);
    try {
      const r = await api.register(settings, orgName);
      setIssuedKey(r.api_key);
    } catch (e: any) { setError(e.message); }
  };

  return (
    <div className="panel">
      <div className="row">
        <label className="muted">API</label>
        <input style={{ width: 220 }} value={settings.baseUrl}
               onChange={(e) => onChange({ ...settings, baseUrl: e.target.value })} />
        <label className="muted">API key</label>
        <input style={{ width: 300 }} type="password" value={settings.apiKey}
               placeholder="X-API-Key"
               onChange={(e) => onChange({ ...settings, apiKey: e.target.value })} />
        <span className="muted">|</span>
        <input style={{ width: 160 }} placeholder="new organisation name"
               value={orgName} onChange={(e) => setOrgName(e.target.value)} />
        <button onClick={register} disabled={!orgName}>Register</button>
      </div>
      {issuedKey && (
        <p className="warn">
          Key (shown once — store it now):&nbsp;<code>{issuedKey}</code>&nbsp;
          <button onClick={() => { onChange({ ...settings, apiKey: issuedKey }); setIssuedKey(null); }}>
            Use it
          </button>
        </p>
      )}
      {error && <p className="bad">{error}</p>}
    </div>
  );
}
