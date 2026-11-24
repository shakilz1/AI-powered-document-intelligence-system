// Thin API client. The optional API key is kept in localStorage and sent as X-API-Key.
const BASE = import.meta.env.VITE_API_BASE || "";

export function getApiKey() {
  try { return localStorage.getItem("docintel_api_key") || ""; } catch { return ""; }
}
export function setApiKey(k) {
  try { localStorage.setItem("docintel_api_key", k); } catch { /* ignore */ }
}

async function request(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch(BASE + path, { ...opts, headers });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    throw new Error(`${res.status}: ${detail}`);
  }
  return data;
}

const qs = (params) =>
  new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined)).toString();

export const api = {
  health: () => request("/api/health"),
  stats: () => request("/api/stats"),
  llmSettings: () => request("/api/settings/llm"),
  setLocalOnly: (local_only) => request("/api/settings/llm", { method: "PUT", json: { local_only } }),
  listDocuments: (params = {}) => request(`/api/documents?${qs(params)}`),
  getDocument: (id) => request(`/api/documents/${id}`),
  getText: (id) => request(`/api/documents/${id}/text`),
  reprocess: (id) => request(`/api/documents/${id}/reprocess`, { method: "POST" }),
  reprocessFailed: () => request(`/api/jobs/reprocess-failed`, { method: "POST" }),
  deleteDocument: (id) => request(`/api/documents/${id}`, { method: "DELETE" }),
  listInvoices: (params = {}) => request(`/api/invoices?${qs(params)}`),
  getInvoice: (id) => request(`/api/invoices/${id}`),
  correctInvoice: (id, body) => request(`/api/invoices/${id}`, { method: "PATCH", json: body }),
  search: (q, mode) => request(`/api/search?${qs({ q, mode })}`),
  crossVerify: (left, right) =>
    request(`/api/crossverify`, { method: "POST", json: { left_document_ids: left, right_document_ids: right } }),
  fileUrl: (id) => `${BASE}/api/documents/${id}/file`,

  // Upload with progress events (fetch has no upload progress).
  upload(files, onProgress) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      for (const f of files) fd.append("files", f, f.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", BASE + "/api/documents");
      const key = getApiKey();
      if (key) xhr.setRequestHeader("X-API-Key", key);
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        xhr.status < 300 ? resolve(data) : reject(new Error(`${xhr.status}: ${data.detail || xhr.statusText}`));
      };
      xhr.onerror = () => reject(new Error("Network error during upload"));
      xhr.send(fd);
    });
  },
};
