import React from "react";

export const STATUS_LABEL = {
  queued: "Queued", processing: "Processing", completed: "Completed", needs_review: "Needs review", failed: "Failed",
};

export function StatusBadge({ status }) {
  return <span className={`badge s-${status}`}>{STATUS_LABEL[status] || status}</span>;
}

export function Progress({ value, status }) {
  const pct = Math.round((value || 0) * 100);
  return (
    <div className={`progress ${status}`} title={`${pct}%`}>
      <div className="bar" style={{ width: `${pct}%` }} />
    </div>
  );
}

export function money(v, cur) {
  if (v === null || v === undefined) return <span className="muted">—</span>;
  return `${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${cur ? " " + cur : ""}`;
}

export function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function ErrorBox({ error }) {
  if (!error) return null;
  return <div className="error">{String(error.message || error)}</div>;
}

export function InvoiceTable({ items, onOpen, showScore }) {
  if (!items?.length) return <p className="muted">No invoices.</p>;
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>Invoice #</th><th>Date</th><th>Supplier</th><th>Customer</th><th className="num">Total</th>
          <th>File</th><th>Status</th>{showScore && <th className="num">Score</th>}
        </tr>
      </thead>
      <tbody>
        {items.map((i) => (
          <tr key={i.id} className="clickable" onClick={() => onOpen(i.document_id)}>
            <td>{i.invoice_number || <span className="muted">missing</span>}</td>
            <td>{i.invoice_date || <span className="muted">—</span>}</td>
            <td>{i.supplier_name || <span className="muted">—</span>}</td>
            <td>{i.customer_name || <span className="muted">—</span>}</td>
            <td className="num">{money(i.total, i.currency)}</td>
            <td className="muted small">{i.filename}{i.page_start ? ` p.${i.page_start}` : ""}</td>
            <td>{i.needs_review ? <span className="badge s-needs_review">Review</span> : <span className="badge s-completed">OK</span>}</td>
            {showScore && <td className="num small">{i.score ?? "—"}</td>}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
