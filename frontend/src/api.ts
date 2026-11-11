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

/** The window every paginated collection carries. `next_offset` is null on the last
 *  page, so a client looping on it terminates. */
export type Page = {
  limit: number; offset: number; returned: number;
  total_matched: number; has_more: boolean; next_offset: number | null;
};
export type ReviewPage = { activities: any[]; page: Page };
export type RunsPage = { runs: any[]; page: Page };
export type FactorsPage = { factors: any[]; page: Page };
export type LineagePage = { run: any; exclusions: any[]; page: Page; line_items: any[] };

async function request<T = any>(s: Settings, method: string, path: string,
                       opts: { params?: Record<string, string | number | undefined>,
                               body?: FormData } = {}): Promise<T> {
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
  return json as T;
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
  register: (s: Settings, name: string, sector?: string) =>
    request(s, "POST", "/organisations", { params: { name, sector } }),
  // Reference data the sign-up form needs BEFORE a key exists, so it is deliberately
  // callable with an empty one.
  publicSectors: (s: Settings) => request({ ...s, apiKey: "" }, "GET", "/sectors"),
  // Prove a key works before admitting it. Accepting a key optimistically and only
  // discovering it is dead on the next screen turns "wrong key" into "the app is broken":
  // /runs is org-scoped and cheap, so a 200 means the key resolves to a live tenant.
  verifyKey: (s: Settings) => request(s, "GET", "/runs", { params: { limit: 1 } }),
  upload: (s: Settings, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request(s, "POST", "/activities/upload_csv", { body: fd });
  },
  // Paginated: returns { activities, page }. The `page` block is the point — the queue
  // used to truncate at 500 rows with nothing saying so, so clearing a page looked
  // exactly like clearing the queue.
  reviewQueue: (s: Settings, limit = 100, offset = 0): Promise<ReviewPage> =>
    request<ReviewPage>(s, "GET", "/mappings/review", { params: { limit, offset } }),
  approve: (s: Settings, activityId: number) =>
    request(s, "POST", `/mappings/${activityId}/approve`),
  override: (s: Settings, activityId: number, factorId: number) =>
    request(s, "POST", `/mappings/${activityId}/override`, { params: { factor_id: factorId } }),
  // Returns { factors, page }. `q` searches category/subcategory/source/geography; the
  // dropdown used to receive the first 200 catalogue rows with no filter and no way to
  // reach the rest, so most of the catalogue could not be chosen at all.
  factors: (s: Settings,
            opts: { category?: string; q?: string; limit?: number } = {}): Promise<FactorsPage> =>
    request<FactorsPage>(s, "GET", "/factors",
            { params: { category: opts.category, q: opts.q, limit: opts.limit ?? 1000 } }),
  run: (s: Settings, gwpSet: string) =>
    request(s, "POST", "/calculate/run", { params: { gwp_set: gwpSet } }),
  runs: (s: Settings, limit = 100): Promise<RunsPage> =>
    request<RunsPage>(s, "GET", "/runs", { params: { limit } }),
  seedDemo: (s: Settings) => request(s, "POST", "/demo/seed"),
  sectors: (s: Settings) => request(s, "GET", "/sectors"),
  setProfile: (s: Settings, params: Record<string, string | number | undefined>) =>
    request(s, "POST", "/organisations/profile", { params }),
  applicability: (s: Settings) => request(s, "GET", "/applicability"),
  applicabilityVocab: (s: Settings) => request(s, "GET", "/applicability/vocabulary"),
  summary: (s: Settings, runId?: number) =>
    request(s, "GET", "/results/summary", { params: { run_id: runId } }),
  // Returns { run, exclusions, page, line_items }. Paginated: this response was
  // unbounded and reached 65.5 MB on a large run.
  lineage: (s: Settings, runId: number, limit = 200,
            offset = 0): Promise<LineagePage> =>
    request<LineagePage>(s, "GET", `/runs/${runId}/lineage`,
                         { params: { limit, offset } }),
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
