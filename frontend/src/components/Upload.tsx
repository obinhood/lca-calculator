import { useState } from "react";
import { api, isAuthError, Settings } from "../api";
import type { Page } from "../App";
import { SESSION_GONE } from "./Home";

const TEMPLATE =
  "date,category,subcategory,description,quantity,unit,geo\n" +
  "2025-01-15,electricity,,HQ office electricity,1200,kWh,GB\n" +
  "2025-01-20,gas,,HQ gas heating,800,kWh,GB\n" +
  "2025-02-10,diesel,,Backup generator,150,L,GB\n" +
  "2025-02-18,flight,short_haul_economy,London-Paris return x2,900,pkm,GB\n" +
  "2025-03-25,waste,landfill_msw,General office waste,250,kg,GB\n";

const COLUMNS: [string, string, string, boolean][] = [
  ["date", "When it happened (YYYY-MM-DD)", "2025-01-15", true],
  ["category", "What was consumed", "electricity", true],
  ["subcategory", "More detail, when it matters", "short_haul_economy", false],
  ["description", "Your own reference", "HQ electricity bill", false],
  ["quantity", "How much", "1200", true],
  ["unit", "Unit of the quantity", "kWh", true],
  ["geo", "Country (ISO code, defaults to GB)", "GB", false],
];

const EXAMPLES: [string, string, string, string][] = [
  ["⚡ Electricity", "electricity", "kWh", "From your utility bills or meter reads"],
  ["🔥 Gas & heating", "gas", "kWh", "Natural gas, district heating"],
  ["⛽ Fuel", "diesel / petrol", "L", "Generators, own vehicles"],
  ["✈️ Flights", "flight", "pkm", "Passenger-km = distance × passengers"],
  ["🚗 Road & rail", "car / train", "km / pkm", "Mileage claims, rail tickets"],
  ["🗑️ Waste", "waste", "kg", "Landfill, recycling, incineration"],
];

export default function Upload({ settings, onChanged, go, onAuthError }:
    { settings: Settings; onChanged: () => void; go: (p: Page) => void;
      onAuthError: (why: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "upload" | "demo">(null);
  const [over, setOver] = useState(false);

  const upload = async (f: File | null) => {
    if (!f) return;
    setBusy("upload"); setError(null); setResult(null);
    try { setResult(await api.upload(settings, f)); onChanged(); }
    catch (e: any) {
      if (isAuthError(e)) { onAuthError(SESSION_GONE); return; }
      setError(e.message);
    }
    setBusy(null);
  };

  const loadDemo = async () => {
    setBusy("demo"); setError(null);
    try { await api.seedDemo(settings); onChanged(); go("footprint"); }
    catch (e: any) {
      if (isAuthError(e)) { onAuthError(SESSION_GONE); return; }
      setError(e.message);
    }
    setBusy(null);
  };

  const downloadTemplate = () => {
    const url = URL.createObjectURL(new Blob([TEMPLATE], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "activity_data_template.csv"; a.click();
    URL.revokeObjectURL(url);
  };

  const m = result?.mapping;
  return (
    <>
      <div className="card">
        <h2>What you need to upload</h2>
        <p className="lead" style={{ marginTop: 6 }}>
          A <b>CSV of the things your business consumed</b> over the period — one row per bill,
          expense line or meter read. You don't calculate any emissions yourself: you give the
          <b> amount and the unit</b>, and we match each row to a published emission factor.
        </p>

        <div className="cards" style={{ marginTop: 16 }}>
          {EXAMPLES.map(([title, cat, unit, note]) => (
            <div key={title} className="feature">
              <b>{title}</b>
              <span>
                category <code>{cat}</code> · unit <code>{unit}</code><br />{note}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>CSV format</h2>
          <div className="spacer" />
          <button onClick={downloadTemplate}>⬇ Download template</button>
        </div>
        <table>
          <thead><tr><th>Column</th><th>What it means</th><th>Example</th><th>Required</th></tr></thead>
          <tbody>
            {COLUMNS.map(([c, meaning, ex, req]) => (
              <tr key={c}>
                <td><code>{c}</code></td>
                <td>{meaning}</td>
                <td className="muted">{ex}</td>
                <td>{req ? <span className="badge bad">Required</span>
                         : <span className="badge">Optional</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="callout" style={{ marginTop: 14 }}>
          <b>Example row.</b>{" "}
          <code>2025-01-15,electricity,,HQ office electricity,1200,kWh,GB</code>
          <div className="muted" style={{ marginTop: 6 }}>
            “On 15 Jan 2025 we used 1,200 kWh of electricity at our UK HQ.”
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Upload your file</h2>
        <div className={"drop" + (over ? " over" : "")}
             onDragOver={(e) => { e.preventDefault(); setOver(true); }}
             onDragLeave={() => setOver(false)}
             onDrop={(e) => {
               e.preventDefault(); setOver(false);
               const f = e.dataTransfer.files?.[0];
               if (f) { setFile(f); upload(f); }
             }}>
          <div className="big-ico">📄</div>
          <p style={{ margin: "8px 0" }}>
            <b>Drag your CSV here</b><br />
            <span className="muted">{file ? file.name : "or choose a file below"}</span>
          </p>
          <div className="row" style={{ justifyContent: "center" }}>
            <input type="file" accept=".csv"
                   onChange={(e) => setFile(e.target.files?.[0] || null)} />
            <button className="primary" onClick={() => upload(file)} disabled={!file || busy !== null}>
              {busy === "upload" ? "Uploading…" : "Upload"}
            </button>
          </div>
        </div>

        {result && (
          <div className="callout info" style={{ marginTop: 14 }}>
            <b>Imported {result.records_ingested} rows.</b>{" "}
            {m && <>{m.auto} matched automatically
              {m.needs_review > 0 && <> · <b>{m.needs_review} need your review</b></>}
              {m.unmapped > 0 && <> · {m.unmapped} unmatched</>}</>}
            <div className="row" style={{ marginTop: 10 }}>
              <button className="primary" onClick={() => go("footprint")}>
                Calculate my footprint →
              </button>
              {m?.needs_review > 0 && (
                <button onClick={() => go("review")}>Review {m.needs_review} matches</button>
              )}
            </div>
            {(result.issues || []).map((i: string, n: number) => (
              <p key={n} className="warn" style={{ margin: "8px 0 0" }}>⚠ {i}</p>
            ))}
          </div>
        )}
        {error && <div className="callout bad" style={{ marginTop: 14 }}>{error}</div>}
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h3>Don't have data ready?</h3>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              Load a sample company's data to explore the whole product first.
            </p>
          </div>
          <button className="primary" onClick={loadDemo} disabled={busy !== null}>
            {busy === "demo" ? "Loading…" : "✨ Load sample data"}
          </button>
        </div>
      </div>
    </>
  );
}
