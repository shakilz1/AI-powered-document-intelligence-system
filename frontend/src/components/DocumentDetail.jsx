import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { ErrorBox, Progress, StatusBadge } from "./common.jsx";
import InvoiceEditor from "./InvoiceEditor.jsx";

export default function DocumentDetail({ id, onBack, onChanged }) {
  const [doc, setDoc] = useState(null);
  const [view, setView] = useState("invoices");
  const [text, setText] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try { setDoc(await api.getDocument(id)); setError(null); } catch (e) { setError(e); }
  }, [id]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!doc || !["queued", "processing"].includes(doc.status)) return;
    const t = setInterval(load, 1000);
    return () => clearInterval(t);
  }, [doc, load]);
  useEffect(() => {
    if (view === "text" && !text) api.getText(id).then(setText).catch(setError);
  }, [view, id, text]);

  if (!doc) return <div><button onClick={onBack}>← Back</button><ErrorBox error={error} /></div>;

  return (
    <div>
      <div className="toolbar">
        <button onClick={onBack}>← Back</button>
        <h2 className="title">{doc.filename}</h2>
        <StatusBadge status={doc.status} />
        <div className="spacer" />
        <a className="button" href={api.fileUrl(doc.id)} target="_blank" rel="noreferrer">Open original</a>
        <button disabled={["queued", "processing"].includes(doc.status)}
          onClick={async () => { await api.reprocess(doc.id); setText(null); load(); onChanged?.(); }}>Reprocess</button>
      </div>
      <ErrorBox error={error} />
      <div className="meta">
        <span>Type: <b>{doc.file_type}</b></span>
        <span>Pages/sheets: <b>{doc.page_count}</b></span>
        <span>OCR: <b>{doc.ocr_used ? `yes (${doc.ocr_confidence ?? "?"})` : "no"}</b></span>
        <span>Extraction: <b>{doc.extraction_method || "—"}</b></span>
        {doc.models?.length > 0 && <span>Model: <b>{doc.models.join(", ")}</b></span>}
        <span>Processing time: <b>{doc.processing_ms ?? "—"} ms</b></span>
        <span>Attempts: <b>{doc.attempts}</b></span>
      </div>
      {["queued", "processing"].includes(doc.status) && (
        <div className="card"><Progress value={doc.progress} status={doc.status} /><div className="small muted">{doc.stage}</div></div>
      )}
      {doc.error && <div className="error">Processing failed: {doc.error}</div>}

      <div className="subtabs">
        {[["invoices", `Invoices (${doc.invoices.length})`], ["text", "Extracted text"], ["history", "Processing history"]].map(([v, l]) => (
          <button key={v} className={view === v ? "tab active" : "tab"} onClick={() => setView(v)}>{l}</button>
        ))}
      </div>

      {view === "invoices" && (
        doc.invoices.length ? doc.invoices.map((inv) => (
          <InvoiceEditor key={inv.id + inv.updated_at} invoice={inv} onSaved={() => { load(); onChanged?.(); }} />
        )) : <p className="muted">No invoice data extracted{doc.status === "failed" ? " (processing failed)" : ""}.</p>
      )}

      {view === "text" && (
        text ? text.pages.map((p) => (
          <div className="card" key={p.page}>
            <div className="small muted">Page/sheet {p.page} · source: {p.source}{p.ocr_confidence != null ? ` · OCR confidence ${p.ocr_confidence}` : ""}</div>
            <pre className="pagetext">{p.text || "(no text)"}</pre>
          </div>
        )) : <p className="muted">Loading…</p>
      )}

      {view === "history" && (
        <table className="grid">
          <thead><tr><th>Time</th><th>Stage</th><th>Status</th><th>Message</th><th className="num">ms</th></tr></thead>
          <tbody>
            {doc.events.map((e, i) => (
              <tr key={i}>
                <td className="small">{new Date(e.created_at + (e.created_at.endsWith("Z") || e.created_at.includes("+") ? "" : "Z")).toLocaleString()}</td>
                <td>{e.stage}</td>
                <td><span className={`badge ev-${e.status}`}>{e.status}</span></td>
                <td className="small pre">{e.message}</td>
                <td className="num small">{e.duration_ms ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
