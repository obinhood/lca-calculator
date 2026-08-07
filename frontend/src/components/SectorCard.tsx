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

export default function SectorCard({ settings }: { settings: Settings }) {
  const [cat, setCat] = useState<Catalogue | null>(null);
  const [chosen, setChosen] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.sectors(settings).then(setCat).catch((e) => setErr(String(e.message || e)));
  }, [settings.baseUrl]);

  const save = async () => {
    if (!chosen) return;
    setBusy(true); setErr(null);
    try {
      const r = await api.setSector(settings, chosen);
      setSaved(r.sector_label);
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
        <button className="primary" onClick={save} disabled={!chosen || busy}>
          {busy ? "Saving…" : "Save"}
        </button>
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
