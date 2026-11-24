import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { ErrorBox, money } from "./common.jsx";

function Picker({ title, docs, selected, setSelected }) {
  const toggle = (id) => setSelected(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  return (
    <div className="card picker">
      <h3>{title}</h3>
      <div className="picklist">
        {docs.map((d) => (
          <label key={d.id} className="pick">
            <input type="checkbox" checked={selected.includes(d.id)} onChange={() => toggle(d.id)} />
            {d.filename} <span className="muted small">({d.file_type}, {d.invoice_count} inv.)</span>
          </label>
        ))}
      </div>
    </div>
  );
}

const fmt = (field, v) => (["subtotal", "tax", "total"].includes(field) ? money(v) : v ?? "—");

export default function CrossVerify() {
  const [docs, setDocs] = useState([]);
  const [left, setLeft] = useState([]);
  const [right, setRight] = useState([]);
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.listDocuments({ limit: 500 }).then((r) => setDocs(r.items.filter((d) => d.invoice_count > 0))).catch(setError);
  }, []);

  async function run() {
    setError(null);
    try { setReport(await api.crossVerify(left, right)); } catch (e) { setError(e); }
  }

  return (
    <div>
      <p className="muted">Compare invoices across documents — e.g. PDF vs PDF, or PDF vs Excel/CSV ledger. Invoices are
        matched by invoice number (fallback: supplier + date + total) and every differing field is reported.
        The same check also runs automatically when an invoice number appears in both a PDF/image and a spreadsheet.</p>
      <div className="two-col">
        <Picker title="Left side (e.g. PDFs)" docs={docs} selected={left} setSelected={setLeft} />
        <Picker title="Right side (e.g. Excel ledger)" docs={docs} selected={right} setSelected={setRight} />
      </div>
      <div className="toolbar"><button className="primary" disabled={!left.length || !right.length} onClick={run}>Run cross-verification</button></div>
      <ErrorBox error={error} />
      {report && (
        <>
          <section className="statsrow">
            {Object.entries(report.summary).map(([k, v]) => (
              <div key={k} className={`stat ${k === "with_differences" && v ? "bad" : ""}`}><div className="stat-value">{v}</div><div className="stat-label">{k.replace(/_/g, " ")}</div></div>
            ))}
          </section>
          <table className="grid">
            <thead><tr><th>Invoice #</th><th>Matched by</th><th>Result</th><th>Differences (left → right)</th></tr></thead>
            <tbody>
              {report.matches.map((m, i) => (
                <tr key={i}>
                  <td>{m.left.invoice_number}<div className="small muted">{m.left.filename} ↔ {m.right.filename}</div></td>
                  <td className="small">{m.matched_by}</td>
                  <td>{m.consistent ? <span className="badge s-completed">Consistent</span> : <span className="badge s-failed">Mismatch</span>}</td>
                  <td className="small">{m.differences.map((d) => (
                    <div key={d.field}><b>{d.field}</b>: {fmt(d.field, d.left)} → {fmt(d.field, d.right)}</div>
                  ))}</td>
                </tr>
              ))}
              {report.unmatched_left.map((u) => (
                <tr key={"l" + u.id}><td>{u.invoice_number}<div className="small muted">{u.filename}</div></td><td /><td><span className="badge s-needs_review">Only left</span></td><td /></tr>
              ))}
              {report.unmatched_right.map((u) => (
                <tr key={"r" + u.id}><td>{u.invoice_number}<div className="small muted">{u.filename}</div></td><td /><td><span className="badge s-needs_review">Only right</span></td><td /></tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
