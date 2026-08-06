import { useState } from "react";
import { api, Settings } from "../api";

type Tab = "Dashboard" | "Upload" | "Review queue" | "Lineage" | "Reports";

const TEMPLATE =
  "date,category,subcategory,description,quantity,unit,geo\n" +
  "2025-01-15,electricity,,HQ office electricity,1200,kWh,GB\n" +
  "2025-01-20,gas,,HQ gas usage,800,kWh,GB\n" +
  "2025-02-18,flight,short_haul_economy,London-Paris return,900,pkm,GB\n";

const COLUMNS: [string, string, string][] = [
  ["date", "YYYY-MM-DD", "2025-01-15"],
  ["category", "what it is (electricity, gas, diesel, flight, car, waste…)", "electricity"],
  ["subcategory", "optional refinement", "short_haul_economy"],
  ["description", "free text (your reference)", "HQ electricity bill"],
  ["quantity", "amount consumed", "1200"],
  ["unit", "kWh, L, km, pkm, tkm, kg…", "kWh"],
  ["geo", "ISO country (defaults GB)", "GB"],
];

export default function Upload({ settings, onChanged, go }:
    { settings: Settings; onChanged: () => void; go: (t: Tab) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "upload" | "demo">(null);

  const upload = async () => {
    if (!file) return;
    setBusy("upload"); setError(null); setResult(null);
    try { setResult(await api.upload(settings, file)); onChanged(); }
    catch (e: any) { setError(e.message); }
    setBusy(null);
  };

  const loadDemo = async () => {
    setBusy("demo"); setError(null);
    try { await api.seedDemo(settings); onChanged(); go("Dashboard"); }
    catch (e: any) { setError(e.message); }
    setBusy(null);
  };

  const downloadTemplate = () => {
    const url = URL.createObjectURL(new Blob([TEMPLATE], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "activities_template.csv"; a.click();
    URL.revokeObjectURL(url);
  };

  const m = result?.mapping;
  return (
    <div className="panel">
      <h2>Add activity data</h2>

      <div style={{ padding: "0 0 12px" }}>
        <p className="muted" style={{ margin: "0 0 10px" }}>
          New here? Load a small sample dataset and jump straight to a finished inventory.
        </p>
        <button className="primary big" onClick={loadDemo} disabled={busy !== null}>
          {busy === "demo" ? "Loading…" : "✨ Load demo data"}
        </button>
      </div>

      <div className="divider" />

      <h2>…or upload your own CSV</h2>
      <div className="help">
        Upload a CSV with these columns. Exact factor matches bind automatically; coarser matches
        wait in the <b>Review queue</b> and are excluded from totals until you approve them.
        <table style={{ marginTop: 10 }}>
          <thead><tr><th>Column</th><th>Meaning</th><th>Example</th></tr></thead>
          <tbody>
            {COLUMNS.map(([c, meaning, ex]) => (
              <tr key={c}><td><code>{c}</code></td><td>{meaning}</td><td className="muted">{ex}</td></tr>
            ))}
          </tbody>
        </table>
        <div style={{ marginTop: 10 }}>
          <button className="ghost" onClick={downloadTemplate}>⬇ Download CSV template</button>
        </div>
      </div>

      <div className="row" style={{ marginTop: 12 }}>
        <input type="file" accept=".csv"
               onChange={(e) => setFile(e.target.files?.[0] || null)} />
        <button className="primary" onClick={upload} disabled={!file || busy !== null}>
          {busy === "upload" ? "Uploading…" : "Upload CSV"}
        </button>
      </div>

      {result && (
        <div style={{ marginTop: 10 }}>
          <p className="ok">
            Ingested {result.records_ingested} records
            {m && <> — {m.auto} auto-mapped, <b className={m.needs_review ? "warn" : ""}>{m.needs_review} need review</b>, {m.unmapped} unmapped</>}
          </p>
          {(result.issues || []).map((i: string, n: number) => (
            <p key={n} className="warn">⚠ {i}</p>
          ))}
          <button className="primary" onClick={() => go("Dashboard")}>
            Go to Dashboard → run a calculation
          </button>
        </div>
      )}
      {error && <p className="bad">{error}</p>}
    </div>
  );
}
