import { useState } from "react";
import { api, Settings } from "../api";

// One immutable inventory -> three disclosure frameworks. Each report shows
// its fail-closed gate result (ready / blockers) before any numbers.
export default function Reports({ settings, runId }:
    { settings: Settings; runId?: number }) {
  const [framework, setFramework] = useState<"secr" | "sb253" | "esrs">("secr");
  const [denom, setDenom] = useState("1.0");
  const [denomUnit, setDenomUnit] = useState("£M revenue");
  const [assurance, setAssurance] = useState("limited");
  const [provider, setProvider] = useState("");
  const [revenue, setRevenue] = useState("10");
  const [currency, setCurrency] = useState("EUR");
  const [report, setReport] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const generate = async () => {
    setError(null); setReport(null);
    try {
      if (framework === "secr") {
        setReport(await api.secr(settings, runId, Number(denom), denomUnit));
      } else if (framework === "sb253") {
        setReport(await api.sb253(settings, runId, assurance, provider));
      } else {
        setReport(await api.esrs(settings, runId, Number(revenue), currency));
      }
    } catch (e: any) { setError(e.message); }
  };

  const ready = report && (report.disclosure_ready ?? report.filing_ready);
  const emissions = report?.emissions_tco2e || report?.e1_6_gross_ghg_emissions_tco2e;

  return (
    <div className="panel">
      <h2>Disclosure reports</h2>
      <div className="row">
        <select value={framework} onChange={(e) => { setFramework(e.target.value as any); setReport(null); }}>
          <option value="secr">UK SECR</option>
          <option value="sb253">California SB 253</option>
          <option value="esrs">CSRD ESRS E1</option>
        </select>
        {framework === "secr" && (
          <>
            <label className="muted">intensity denominator</label>
            <input style={{ width: 90 }} value={denom} onChange={(e) => setDenom(e.target.value)} />
            <input style={{ width: 130 }} value={denomUnit} onChange={(e) => setDenomUnit(e.target.value)} />
          </>
        )}
        {framework === "sb253" && (
          <>
            <label className="muted">assurance</label>
            <select value={assurance} onChange={(e) => setAssurance(e.target.value)}>
              <option>none</option><option>limited</option><option>reasonable</option>
            </select>
            <input style={{ width: 160 }} placeholder="provider" value={provider}
                   onChange={(e) => setProvider(e.target.value)} />
          </>
        )}
        {framework === "esrs" && (
          <>
            <label className="muted">net revenue (millions)</label>
            <input style={{ width: 90 }} value={revenue} onChange={(e) => setRevenue(e.target.value)} />
            <input style={{ width: 70 }} value={currency} onChange={(e) => setCurrency(e.target.value)} />
          </>
        )}
        <button className="primary" onClick={generate}>Generate</button>
        <span className="muted">{runId ? `run #${runId}` : "latest run"}</span>
      </div>
      {error && <p className="bad">{error}</p>}
      {report && (
        <>
          <p className={ready ? "ok" : "bad"}>
            {report.framework}: {ready ? "✔ disclosure-ready" : "✖ NOT ready"}
          </p>
          {!ready && (
            <div className="blockers">
              {(report.blockers || []).map((b: string, i: number) => <p key={i} className="bad">{b}</p>)}
            </div>
          )}
          {emissions && (
            <div className="kpis">
              {Object.entries(emissions).map(([k, v]: any) =>
                typeof v === "number" ? (
                  <div className="kpi" key={k}>
                    <div className="v">{v.toLocaleString(undefined, { maximumFractionDigits: 4 })}</div>
                    <div className="l">{k} (tCO₂e)</div>
                  </div>
                ) : null)}
            </div>
          )}
          {report.energy_use_kwh && (
            <p className="muted">Energy: {report.energy_use_kwh.total_kwh?.toLocaleString()} kWh
              (elec {report.energy_use_kwh.electricity}, gas {report.energy_use_kwh.gas}, diesel {report.energy_use_kwh.diesel})</p>
          )}
          {report.e1_5_energy_consumption && (
            <p className="muted">E1-5 energy: {report.e1_5_energy_consumption.total_mwh} MWh
              · renewable contractual {report.e1_5_energy_consumption.electricity_renewable_contractual_mwh} MWh</p>
          )}
          {report.intensity_ratio && (
            <p className="muted">Intensity: {report.intensity_ratio.tco2e_scope1_and_2_location} tCO₂e / {report.intensity_ratio.denominator_unit}</p>
          )}
          {report.methodology_statement && (
            <details>
              <summary className="muted">Methodology statement</summary>
              <pre className="detail">{report.methodology_statement}</pre>
            </details>
          )}
          <details>
            <summary className="muted">Full payload (JSON)</summary>
            <pre className="detail">{JSON.stringify(report, null, 2)}</pre>
          </details>
        </>
      )}
    </div>
  );
}
