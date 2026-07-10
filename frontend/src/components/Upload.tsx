import { useState } from "react";
import { api, Settings } from "../api";

export default function Upload({ settings, onChanged }:
    { settings: Settings; onChanged: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const upload = async () => {
    if (!file) return;
    setBusy(true); setError(null); setResult(null);
    try {
      const r = await api.upload(settings, file);
      setResult(r);
      onChanged();
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };

  const m = result?.mapping;
  return (
    <div className="panel">
      <h2>Upload activity CSV</h2>
      <p className="muted">
        Columns: date, category, subcategory, description, quantity, unit, geo.
        Exact factor matches bind automatically; coarser matches wait in the
        review queue and are excluded from totals until a human decides.
      </p>
      <div className="row">
        <input type="file" accept=".csv"
               onChange={(e) => setFile(e.target.files?.[0] || null)} />
        <button className="primary" onClick={upload} disabled={!file || busy}>
          {busy ? "Uploading…" : "Upload"}
        </button>
      </div>
      {result && (
        <div>
          <p className="ok">
            Ingested {result.records_ingested} records
            {m && <> — {m.auto} auto-mapped, <b className={m.needs_review ? "warn" : ""}>{m.needs_review} need review</b>, {m.unmapped} unmapped</>}
          </p>
          {(result.issues || []).map((i: string, n: number) => (
            <p key={n} className="warn">⚠ {i}</p>
          ))}
        </div>
      )}
      {error && <p className="bad">{error}</p>}
    </div>
  );
}
