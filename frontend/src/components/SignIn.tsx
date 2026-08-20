import { useEffect, useState } from "react";
import { api, isAuthError, Settings } from "../api";

/**
 * The auth area. Deliberately NOT an email-and-password form.
 *
 * This platform authenticates an ORGANISATION by API key (X-API-Key) — there are no user
 * accounts, no passwords, no SSO and no teams. Drawing a familiar-looking login box would
 * imply all four, and the first thing a new user would do is try to reset a password that
 * does not exist. The screen therefore says what the credential actually is, and the copy
 * treats it as a secret to store rather than a password to remember.
 *
 * Two flows, one screen: sign in with a key you hold, or create a workspace and be issued
 * one. Both END in the same place — a verified key handed to the app.
 */
type Mode = "signin" | "create";

export default function SignIn({ settings, onChange, onBack, notice, initialMode }: {
  settings: Settings;
  onChange: (s: Settings) => void;
  onBack: () => void;
  notice?: string | null;
  initialMode?: Mode;
}) {
  const [mode, setMode] = useState<Mode>(initialMode || "signin");
  const [key, setKey] = useState("");
  const [orgName, setOrgName] = useState("");
  const [sector, setSector] = useState("");
  const [sectors, setSectors] = useState<{ key: string; label: string }[]>([]);
  const [issued, setIssued] = useState<{ key: string; name: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  // Sector is optional but it routes Scope 3 screening, so it is worth asking once at
  // sign-up rather than leaving it to be discovered in Settings. /sectors is open
  // precisely because a caller needs it before it has a key.
  useEffect(() => {
    api.publicSectors(settings)
      .then((rows: any) => {
        const list = Array.isArray(rows) ? rows : rows?.sectors || [];
        setSectors(list.map((r: any) => ({
          key: r.key ?? r.sector ?? String(r),
          label: r.label ?? r.name ?? r.key ?? String(r),
        })).filter((r: any) => r.key));
      })
      .catch(() => setSectors([]));   // optional field; never block sign-up on it
  }, [settings.baseUrl]);

  const admit = (apiKey: string) => onChange({ ...settings, apiKey });

  const signIn = async () => {
    const k = key.trim();
    if (!k) return;
    setBusy(true); setError(null);
    try {
      // Prove it before admitting it. Accepting the key and letting the next screen fail
      // turns "you pasted the wrong key" into "the whole app is broken".
      await api.verifyKey({ ...settings, apiKey: k });
      admit(k);
    } catch (e: any) {
      setError(isAuthError(e)
        ? "That key was not recognised. Keys are issued once when a workspace is created — "
          + "if you have lost yours, create a new workspace."
        : `Could not reach the API at ${settings.baseUrl}. ${e.message}`);
    }
    setBusy(false);
  };

  const create = async () => {
    const n = orgName.trim();
    if (!n) return;
    setBusy(true); setError(null);
    try {
      const r = await api.register(settings, n, sector || undefined);
      setIssued({ key: r.api_key, name: r.name });
    } catch (e: any) {
      // A deployment can set REGISTRATION_TOKEN to close open sign-up. That is a
      // configuration answer, not a user error, so it gets its own sentence.
      setError(isAuthError(e)
        ? "Open registration is disabled on this deployment. Ask whoever runs it for a "
          + "workspace key, then sign in with it."
        : e.message);
    }
    setBusy(false);
  };

  const copy = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.key);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* clipboard blocked — the key is on screen and selectable anyway */ }
  };

  return (
    <div className="auth">
      {/* Left: why this screen looks the way it does. Carries the brand into a screen that
          would otherwise be a bare form on a white page. */}
      <aside className="auth-aside">
        <button className="auth-brand" onClick={onBack}>
          <span className="mark">🌿</span>
          <span><b>Carbon Platform</b><span>audit-grade accounting</span></span>
        </button>

        <div className="auth-aside-body">
          <h2>Your API key is your login.</h2>
          <p>
            A workspace is an organisation, and it is authenticated by a key rather than a
            password. There are no user accounts to manage and nothing to reset — which
            also means the key is the one thing you must not lose.
          </p>
          <ul className="auth-points">
            <li><b>Issued once.</b> We store only a hash, so we cannot show it to you again.</li>
            <li><b>Revocable.</b> Rotate or revoke it from Settings at any time.</li>
            <li><b>Scoped to one organisation.</b> Every request it makes is confined to your data.</li>
          </ul>
        </div>

        <p className="auth-foot muted">
          Working against another server? Set the endpoint under Advanced.
        </p>
      </aside>

      {/* Right: the actual work. */}
      <main className="auth-main">
        <div className="auth-card">
          {notice && <div className="callout warn" style={{ marginBottom: 18 }}>{notice}</div>}

          {issued ? (
            /* The one-time secret. Shown on its own, with no other control competing for
               attention, and gated behind an explicit acknowledgement — a key lost between
               this screen and the next means the workspace is unreachable for good. */
            <>
              <span className="auth-eyebrow">Step 2 of 2</span>
              <h1>Save your API key</h1>
              <p className="lead">
                This is the only time <b>{issued.name}</b>’s key is shown. Store it in a
                password manager now.
              </p>

              <div className="secret">
                <code>{issued.key}</code>
                <button className={copied ? "primary" : ""} onClick={copy}>
                  {copied ? "Copied ✓" : "Copy"}
                </button>
              </div>

              <label className="ack">
                <input type="checkbox" checked={saved}
                       onChange={(e) => setSaved(e.target.checked)} />
                <span>I have saved this key somewhere I can find it again.</span>
              </label>

              <button className="primary big block" disabled={!saved}
                      onClick={() => admit(issued.key)}>
                Open your workspace →
              </button>
            </>
          ) : (
            <>
              <div className="auth-tabs" role="tablist">
                <button role="tab" aria-selected={mode === "signin"}
                        className={mode === "signin" ? "active" : ""}
                        onClick={() => { setMode("signin"); setError(null); }}>Sign in</button>
                <button role="tab" aria-selected={mode === "create"}
                        className={mode === "create" ? "active" : ""}
                        onClick={() => { setMode("create"); setError(null); }}>Create workspace</button>
              </div>

              {mode === "signin" ? (
                <>
                  <h1>Sign in</h1>
                  <p className="lead">Paste the API key issued when your workspace was created.</p>
                  <label className="field block">API key
                    <input type="password" autoFocus placeholder="paste your key"
                           value={key} onChange={(e) => setKey(e.target.value)}
                           onKeyDown={(e) => { if (e.key === "Enter") signIn(); }} />
                  </label>
                  <button className="primary big block" onClick={signIn} disabled={!key.trim() || busy}>
                    {busy ? "Checking…" : "Sign in →"}
                  </button>
                  <p className="muted center">
                    No workspace yet?{" "}
                    <button className="link" onClick={() => { setMode("create"); setError(null); }}>
                      Create one
                    </button>
                  </p>
                </>
              ) : (
                <>
                  <span className="auth-eyebrow">Step 1 of 2</span>
                  <h1>Create a workspace</h1>
                  <p className="lead">
                    Your organisation name is all that is required. You will be issued an API
                    key on the next screen.
                  </p>
                  <label className="field block">Organisation name
                    <input autoFocus placeholder="e.g. Acme Ltd" value={orgName}
                           onChange={(e) => setOrgName(e.target.value)}
                           onKeyDown={(e) => { if (e.key === "Enter") create(); }} />
                  </label>
                  {sectors.length > 0 && (
                    <label className="field block">Sector <span className="hint">optional</span>
                      <select value={sector} onChange={(e) => setSector(e.target.value)}>
                        <option value="">Not sure yet — set it later</option>
                        {sectors.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
                      </select>
                      <span className="hint">
                        Routes which Scope 3 categories you have to justify excluding. Changeable later.
                      </span>
                    </label>
                  )}
                  <button className="primary big block" onClick={create}
                          disabled={!orgName.trim() || busy}>
                    {busy ? "Creating…" : "Create workspace →"}
                  </button>
                  <p className="muted center">
                    Already have a key?{" "}
                    <button className="link" onClick={() => { setMode("signin"); setError(null); }}>
                      Sign in
                    </button>
                  </p>
                </>
              )}

              {error && <div className="callout bad" style={{ marginTop: 14 }}>{error}</div>}

              <div className="divider" />
              <button className="link" onClick={() => setAdvanced(!advanced)}>
                {advanced ? "▾" : "▸"} Advanced — API endpoint
              </button>
              {advanced && (
                <label className="field block" style={{ marginTop: 8 }}>Base URL
                  <input value={settings.baseUrl}
                         onChange={(e) => onChange({ ...settings, baseUrl: e.target.value })} />
                  <span className="hint">
                    Defaults to this site. Only change it to point at another server.
                  </span>
                </label>
              )}
            </>
          )}
        </div>

        <button className="link auth-back" onClick={onBack}>← Back to site</button>
      </main>
    </div>
  );
}
