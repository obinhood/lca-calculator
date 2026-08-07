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
    // Carry the HTTP status so callers can tell "your key is dead" (401/403) apart from a
    // genuine data problem. A stale key in localStorage — e.g. after the demo database was
    // reset by a redeploy — otherwise looks like every action silently failing.
    const detail = json?.detail;
    const msg = typeof detail === "string" ? detail
      : detail ? JSON.stringify(detail) : `${res.status}: ${text.slice(0, 200)}`;
    throw new ApiError(msg, res.status);
  }
  return json;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const isAuthError = (e: any) => e?.status === 401 || e?.status === 403;

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
  seedDemo: (s: Settings) => request(s, "POST", "/demo/seed"),
  sectors: (s: Settings) => request(s, "GET", "/sectors"),
  setSector: (s: Settings, sector: string) =>
    request(s, "POST", "/organisations/profile", { params: { sector } }),
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
  compliance: (s: Settings, key: string,
               params: Record<string, string | number | undefined>) =>
    request(s, "GET", `/compliance/${key}`, { params }),
  // Server-rendered CSV/PDF download. Returns a Blob (not JSON), so it bypasses `request`.
  exportReport: async (s: Settings, key: string,
                       params: Record<string, string | number | undefined>): Promise<Blob> => {
    const url = new URL(`${s.baseUrl}/export/${key}`);
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
    }
    const res = await fetch(url.toString(),
      { headers: s.apiKey ? { "X-API-Key": s.apiKey } : {} });
    if (!res.ok) {
      let msg = `${res.status}`;
      try { const j = JSON.parse(await res.text()); msg = j?.detail || msg; } catch { /* binary */ }
      throw new ApiError(String(msg), res.status);
    }
    return res.blob();
  },
};
