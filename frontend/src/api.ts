// Thin API client. The key is held in memory + localStorage; every org-scoped
// call carries X-API-Key. All errors surface the server's detail message.

export type Settings = { baseUrl: string; apiKey: string };

export function loadSettings(): Settings {
  // In the deployed single-service demo the API serves this very page, so default to the
  // SAME origin and everything works with no configuration. Under the Vite dev server
  // (:5173) the API runs separately, so fall back to the local API port there.
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const devDefault = origin.includes(":5173") ? "http://127.0.0.1:8000" : origin;
  return {
    baseUrl: localStorage.getItem("baseUrl") || devDefault,
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
  // Generic disclosure-report fetch: the report registry in Reports.tsx supplies the
  // endpoint path and its query params, so a new framework is one registry entry, not a
  // new client method. Every report endpoint is GET and org-scoped by the API key.
  report: (s: Settings, path: string,
           params: Record<string, string | number | undefined>) =>
    request(s, "GET", path, { params }),
};
