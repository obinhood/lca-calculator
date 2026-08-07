import { Settings } from "../api";
import SectorCard from "./SectorCard";

export default function SettingsPage({ settings, onChange }:
    { settings: Settings; onChange: (s: Settings) => void }) {
  return (
    <>
      <SectorCard settings={settings} />

      <div className="card">
        <h2>Your workspace</h2>
        <table style={{ marginTop: 10 }}>
          <tbody>
            <tr>
              <td style={{ width: 200 }}><b>API key</b></td>
              <td><code>••••••••{settings.apiKey.slice(-6)}</code>
                <div className="muted" style={{ marginTop: 4 }}>
                  This key is your login and grants full access to your organisation's data and
                  every report. Keep it secret; anyone with it can read your inventory.
                </div>
              </td>
            </tr>
            <tr>
              <td><b>Status</b></td>
              <td><span className="badge ok">Connected</span></td>
            </tr>
          </tbody>
        </table>
        <div className="row" style={{ marginTop: 14 }}>
          <button onClick={() => onChange({ ...settings, apiKey: "" })}>Sign out</button>
        </div>
      </div>

      <div className="card">
        <h2>Connection</h2>
        <p className="lead" style={{ marginTop: 6 }}>
          The API endpoint this app talks to. It defaults to the site you're on — only change it
          if you're pointing at a different server.
        </p>
        <label className="field" style={{ maxWidth: 420 }}>API base URL
          <input value={settings.baseUrl}
                 onChange={(e) => onChange({ ...settings, baseUrl: e.target.value })} />
        </label>
        <p className="muted" style={{ marginTop: 12 }}>
          Full API documentation is at <code>{settings.baseUrl}/docs</code>.
        </p>
      </div>
    </>
  );
}
