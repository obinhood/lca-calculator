import { useEffect, useState } from "react";
import { api, Settings } from "../api";

type Sector = {
  key: string; label: string; note: string;
  nace: string | null; sic: string | null; dominant_scope3: number[];
};
type Catalogue = {
  sectors: Sector[];
  what_sector_does: string[];
  what_sector_does_not_do: string[];
};

// The Scope 3 category names, so a dominant-category list reads as English rather than
// as bare numbers a user would have to look up.
const CAT: Record<number, string> = {
  1: "Purchased goods & services", 2: "Capital goods", 3: "Fuel & energy (upstream)",
  4: "Upstream transport", 5: "Waste", 6: "Business travel", 7: "Commuting",
  8: "Upstream leased assets", 9: "Downstream transport", 10: "Processing of sold products",
  11: "Use of sold products", 12: "End-of-life of sold products",
  13: "Downstream leased assets", 14: "Franchises", 15: "Investments (financed)",
};

export default function SectorCard({ settings, onSaved }:
    { settings: Settings; onSaved?: () => void }) {
  const [cat, setCat] = useState<Catalogue | null>(null);
  const [chosen, setChosen] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Entity size, territory and listing — these decide which regimes COMPEL a filing.
  const [emp, setEmp] = useState("");
  const [turn, setTurn] = useState("");
  const [bs, setBs] = useState("");
  const [cur, setCur] = useState("EUR");
  const [jur, setJur] = useState<string[]>([]);
  const [mkt, setMkt] = useState<string[]>([]);
  const [touchedJur, setTouchedJur] = useState(false);
  const [touchedMkt, setTouchedMkt] = useState(false);
  const [vocab, setVocab] = useState<{ jurisdictions: Record<string, string>;
                                       listing_markets: Record<string, string> } | null>(null);

  useEffect(() => {
    api.sectors(settings).then(setCat).catch((e) => setErr(String(e.message || e)));
    api.applicabilityVocab(settings).then(setVocab).catch(() => {});
    // Load what is ALREADY saved. Rendering an empty form over a stored profile tells
    // the user nothing is set when something is — and they would then have to retype
    // values they already gave us, or leave fields blank and lose them.
    api.applicability(settings).then((a: any) => {
      const p = a.entity_profile || {};
      if (p.sector) setChosen(p.sector);
      if (p.employees != null) setEmp(String(p.employees));
      if (p.annual_turnover != null) setTurn(String(p.annual_turnover));
      if (p.balance_sheet_total != null) setBs(String(p.balance_sheet_total));
      if (p.currency) setCur(p.currency);
      if (p.jurisdictions) { setJur(p.jurisdictions); setTouchedJur(true); }
      if (p.listed_markets) { setMkt(p.listed_markets); setTouchedMkt(true); }
    }).catch(() => {});
  }, [settings.baseUrl, settings.apiKey]);

  const toggle = (list: string[], set: (v: string[]) => void, key: string) =>
    set(list.includes(key) ? list.filter((x) => x !== key) : [...list, key]);

  const save = async () => {
    setBusy(true); setErr(null);
    try {
      // Only send what was actually filled in — an untouched field must stay absent
      // rather than being written as a value, because absent is what produces
      // "cannot determine" instead of a wrong "not required".
      const p: Record<string, string | number | undefined> = {};
      if (chosen) p.sector = chosen;
      if (emp !== "") p.employees = Number(emp);
      if (turn !== "") p.annual_turnover = Number(turn);
      if (bs !== "") p.balance_sheet_total = Number(bs);
      if (turn !== "" || bs !== "") p.financials_currency = cur;
      // Sent even when empty once the user has engaged with them: "" is how the API
      // records "answered: none". Without this a user could never say "we are unlisted",
      // and an unrecorded listing status leaves regimes that catch quoted companies at
      // any size permanently undecidable.
      if (touchedJur) p.jurisdictions = jur.join(",");
      if (touchedMkt) p.listed_markets = mkt.join(",");
      if (!Object.keys(p).length) { setErr("Nothing to save yet."); return; }
      const r = await api.setProfile(settings, p);
      setSaved(r.sector_label || "Profile saved");
      onSaved?.();
    } catch (e: any) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const sel = cat?.sectors.find((s) => s.key === chosen);

  return (
    <div className="card">
      <h2>Your industry</h2>
      <p className="lead" style={{ marginTop: 6 }}>
        Your sector decides which parts of your value chain you'll be expected to account
        for. It does not change any of your emission figures.
      </p>

      {err && <div className="notice warn" style={{ marginTop: 10 }}>{err}</div>}

      <div className="row" style={{ marginTop: 12, alignItems: "flex-end" }}>
        <label className="field" style={{ maxWidth: 380 }}>Industry
          <select value={chosen} onChange={(e) => { setChosen(e.target.value); setSaved(null); }}>
            <option value="">Select your industry…</option>
            {(cat?.sectors || []).map((s) => (
              <option key={s.key} value={s.key}>{s.label}</option>
            ))}
          </select>
        </label>
      </div>

      {sel && (
        <div className="notice" style={{ marginTop: 12 }}>
          <b>{sel.label}</b>
          <div className="muted" style={{ marginTop: 4 }}>{sel.note}</div>
          {sel.nace && (
            <div className="muted" style={{ marginTop: 4 }}>
              Roughly NACE {sel.nace}{sel.sic ? ` · SIC ${sel.sic}` : ""}
            </div>
          )}
          {sel.dominant_scope3.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <div className="muted">
                You can still exclude any of these — but you'll be asked to say why the
                sector pattern doesn't hold for your entity:
              </div>
              <div className="row" style={{ marginTop: 6, flexWrap: "wrap", gap: 6 }}>
                {sel.dominant_scope3.map((c) => (
                  <span key={c} className="badge">Cat {c} — {CAT[c]}</span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <h3 style={{ margin: "20px 0 6px" }}>Size, territory and listing</h3>
      <p className="lead" style={{ marginTop: 0 }}>
        These decide which disclosure regimes legally compel a filing from you. Leave a
        field blank if you don't know it — blank means "unanswered", never "not required".
      </p>

      <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label className="field" style={{ maxWidth: 150 }}>Employees (avg FTE)
          <input type="number" min="0" value={emp} placeholder="e.g. 1200"
                 onChange={(e) => setEmp(e.target.value)} />
        </label>
        <label className="field" style={{ maxWidth: 190 }}>Net turnover
          <input type="number" min="0" value={turn} placeholder="e.g. 600000000"
                 onChange={(e) => setTurn(e.target.value)} />
        </label>
        <label className="field" style={{ maxWidth: 190 }}>Balance sheet total
          <input type="number" min="0" value={bs} placeholder="e.g. 900000000"
                 onChange={(e) => setBs(e.target.value)} />
        </label>
        <label className="field" style={{ maxWidth: 110 }}>Currency
          <select value={cur} onChange={(e) => setCur(e.target.value)}>
            {["EUR", "GBP", "USD", "CHF", "SEK", "DKK", "NOK", "AUD", "CAD", "JPY"]
              .map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
      </div>

      {vocab && (
        <>
          <div style={{ marginTop: 14 }}>
            <div className="muted">Where do you operate or have an establishment?</div>
            <div className="row" style={{ marginTop: 6, flexWrap: "wrap", gap: 6 }}>
              {Object.entries(vocab.jurisdictions).map(([k, label]) => (
                <button key={k} className={jur.includes(k) ? "primary" : ""}
                        onClick={() => { setTouchedJur(true); toggle(jur, setJur, k); }}
                        title={label}>{k}</button>
              ))}
            </div>
          </div>
          <div style={{ marginTop: 14 }}>
            <div className="muted">
              Listed anywhere? Several regimes catch listed entities at any size, so
              leaving this blank leaves those unanswered. If you are unlisted, click one
              then click it again to record that explicitly.
            </div>
            <div className="row" style={{ marginTop: 6, flexWrap: "wrap", gap: 6 }}>
              {Object.entries(vocab.listing_markets).map(([k, label]) => (
                <button key={k} className={mkt.includes(k) ? "primary" : ""}
                        onClick={() => { setTouchedMkt(true); toggle(mkt, setMkt, k); }}
                        title={label}>{label}</button>
              ))}
            </div>
          </div>
        </>
      )}

      <div className="row" style={{ marginTop: 16 }}>
        <button className="primary" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Save profile"}
        </button>
      </div>

      {saved && (
        <div className="notice ok" style={{ marginTop: 10 }}>
          Saved — {saved}. Recalculate your footprint to apply this to a run's Scope 3 screening;
          runs you've already calculated keep the sector they were computed under.
        </div>
      )}

      {cat && (
        <div className="grid-2" style={{ marginTop: 16 }}>
          <div>
            <h3 style={{ margin: "0 0 6px" }}>What your industry changes</h3>
            <ul className="muted" style={{ margin: 0, paddingLeft: 18 }}>
              {cat.what_sector_does.map((t, i) => <li key={i} style={{ marginBottom: 6 }}>{t}</li>)}
            </ul>
          </div>
          <div>
            <h3 style={{ margin: "0 0 6px" }}>What it does <i>not</i> change</h3>
            <ul className="muted" style={{ margin: 0, paddingLeft: 18 }}>
              {cat.what_sector_does_not_do.map((t, i) => (
                <li key={i} style={{ marginBottom: 6 }}>{t}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
