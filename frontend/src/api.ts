// Thin API client. The key is held in memory + localStorage; every org-scoped
// call carries X-API-Key. All errors surface the server's detail message.

export type Settings = { baseUrl: string; apiKey: string };

export function loadSettings(): Settings {
  return {
    baseUrl: localStorage.getItem("baseUrl") || "http://127.0.0.1:8000",
    apiKey: localStorage.getItem("apiKey") || "",
  };
}

export function saveSettings(s: Settings) {
  localStorage.setItem("baseUrl", s.baseUrl);
  localStorage.setItem("apiKey", s.apiKey);
}

async function request(s: Settings, method: string, path: string,
                       opts: { params?: Record<string, string | number | undefined>,
                               body?: FormData } = {}) {
  const url = new URL(s.baseUrl + path);
  for (const [k, v] of Object.entries(opts.params || {})) {
    if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
  }
  const res = await fetch(url.toString(), {
    method,
    headers: s.apiKey ? { "X-API-Key": s.apiKey } : {},
    body: opts.body,
  });
  const text = await res.text();
  let json: any = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* plaintext */ }
  if (!res.ok) {
    throw new Error(json?.detail ? String(JSON.stringify(json.detail)) : `${res.status}: ${text.slice(0, 200)}`);
  }
  return json;
}

export const api = {
  register: (s: Settings, name: string) =>
    request(s, "POST", "/organisations", { params: { name } }),
  upload: (s: Settings, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request(s, "POST", "/activities/upload_csv", { body: fd });
  },
  reviewQueue: (s: Settings) => request(s, "GET", "/mappings/review"),
  approve: (s: Settings, activityId: number) =>
    request(s, "POST", `/mappings/${activityId}/approve`),
  override: (s: Settings, activityId: number, factorId: number) =>
    request(s, "POST", `/mappings/${activityId}/override`, { params: { factor_id: factorId } }),
  factors: (s: Settings, category?: string) =>
    request(s, "GET", "/factors", { params: { category } }),
  run: (s: Settings, gwpSet: string) =>
    request(s, "POST", "/calculate/run", { params: { gwp_set: gwpSet } }),
  runs: (s: Settings) => request(s, "GET", "/runs"),
  summary: (s: Settings, runId?: number) =>
    request(s, "GET", "/results/summary", { params: { run_id: runId } }),
  lineage: (s: Settings, runId: number) =>
    request(s, "GET", `/runs/${runId}/lineage`),
  secr: (s: Settings, runId: number | undefined, denom: number, unit: string) =>
    request(s, "GET", "/reports/secr", {
      params: { run_id: runId, intensity_denominator: denom, intensity_denominator_unit: unit } }),
  sb253: (s: Settings, runId: number | undefined, level: string, provider: string) =>
    request(s, "GET", "/reports/sb253", {
      params: { run_id: runId, assurance_level: level, assurance_provider: provider } }),
  esrs: (s: Settings, runId: number | undefined, revenue: number, currency: string) =>
    request(s, "GET", "/reports/esrs_e1", {
      params: { run_id: runId, net_revenue_millions: revenue, revenue_currency: currency } }),
};
