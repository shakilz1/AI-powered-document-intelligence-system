import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { ErrorBox, fmtBytes, Progress, StatusBadge } from "./common.jsx";

const ACCEPT = ".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv";

export default function Documents({ onOpen, onChanged }) {
  const [docs, setDocs] = useState([]);
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");
  const [error, setError] = useState(null);
  const [upload, setUpload] = useState(null); // {progress, names}
  const [rejected, setRejected] = useState([]);
  const [drag, setDrag] = useState(false);
  const inputRef = useRef();

  const load = useCallback(async () => {
    try {
      const r = await api.listDocuments({ status: filter, q, limit: 200 });
      setDocs(r.items);
      setError(null);
    } catch (e) { setError(e); }
  }, [filter, q]);

  useEffect(() => { load(); }, [load]);
  // Poll while anything is still in flight.
  useEffect(() => {
    const busy = docs.some((d) => d.status === "queued" || d.status === "processing");
    const t = setInterval(load, busy ? 1200 : 6000);
    return () => clearInterval(t);
  }, [docs, load]);

  async function doUpload(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    setUpload({ progress: 0, names: files.map((f) => f.name) });
    setRejected([]);
    try {
      const r = await api.upload(files, (p) => setUpload((u) => ({ ...u, progress: p })));
      setRejected(r.rejected || []);
      await load();
      onChanged?.();
    } catch (e) { setError(e); }
    setUpload(null);
  }

  async function act(fn) {
    try { await fn(); await load(); onChanged?.(); } catch (e) { setError(e); }
  }

  return (
    <div>
      <div
        className={`dropzone ${drag ? "drag" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); doUpload(e.dataTransfer.files); }}
        onClick={() => inputRef.current?.click()}
      >
        <input ref={inputRef} type="file" multiple accept={ACCEPT} hidden onChange={(e) => { doUpload(e.target.files); e.target.value = ""; }} />
        {upload ? (
          <div>
            <div>Uploading {upload.names.length} file(s)… {Math.round(upload.progress * 100)}%</div>
            <Progress value={upload.progress} status="processing" />
          </div>
        ) : (
          <>
            <div className="drop-title">Drop invoices here or click to choose files</div>
            <div className="muted">PDF (digital or scanned), JPG, PNG, TIFF, XLSX, XLS, CSV — multiple files at once</div>
          </>
        )}
      </div>
      {rejected.length > 0 && (
        <div className="error">Rejected: {rejected.map((r) => `${r.filename} (${r.error})`).join("; ")}</div>
      )}
      <ErrorBox error={error} />

      <div className="toolbar">
        <input placeholder="Filter by filename…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">All statuses</option>
          <option value="queued">Queued</option>
          <option value="processing">Processing</option>
          <option value="completed">Completed</option>
          <option value="needs_review">Needs review</option>
          <option value="failed">Failed</option>
        </select>
        <div className="spacer" />
        <button onClick={() => act(api.reprocessFailed)} disabled={!docs.some((d) => d.status === "failed")}>
          Reprocess all failed
        </button>
      </div>

      <table className="grid">
        <thead>
          <tr>
            <th>File</th><th>Type</th><th>Size</th><th>Status</th><th style={{ width: 180 }}>Progress</th>
            <th className="num">Pages</th><th className="num">Invoices</th><th className="num">Review</th><th className="num">Time</th><th />
          </tr>
        </thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id}>
              <td><button className="link" onClick={() => onOpen(d.id)}>{d.filename}</button>
                {d.error && <div className="small err-text">{d.error}</div>}</td>
              <td>{d.file_type}{d.ocr_used ? <span className="tag">OCR</span> : null}</td>
              <td className="small">{fmtBytes(d.size_bytes)}</td>
              <td><StatusBadge status={d.status} /></td>
              <td><Progress value={d.progress} status={d.status} /><div className="small muted">{d.stage}{d.stage === "done" && d.models?.length ? ` · ${d.models.join(", ")}` : ""}</div></td>
              <td className="num">{d.page_count || "—"}</td>
              <td className="num">{d.invoice_count}</td>
              <td className="num">{d.review_count || ""}</td>
              <td className="num small">{d.processing_ms != null ? `${d.processing_ms} ms` : ""}</td>
              <td className="actions">
                <button className="small" disabled={d.status === "queued" || d.status === "processing"} onClick={() => act(() => api.reprocess(d.id))}>Reprocess</button>
                <button className="small danger" disabled={d.status === "processing"}
                  onClick={() => window.confirm(`Delete ${d.filename} and its results?`) && act(() => api.deleteDocument(d.id))}>Delete</button>
              </td>
            </tr>
          ))}
          {!docs.length && <tr><td colSpan={10} className="muted center">No documents yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
