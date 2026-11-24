import React, { useState } from "react";
import { api } from "../api.js";
import { ErrorBox } from "./common.jsx";

const FIELDS = [
  ["invoice_number", "Invoice number", "text"],
  ["invoice_date", "Invoice date", "date"],
  ["supplier_name", "Supplier", "text"],
  ["customer_name", "Customer", "text"],
  ["subtotal", "Subtotal", "number"],
  ["tax", "Tax", "number"],
  ["total", "Total", "number"],
  ["currency", "Currency", "text"],
];

function confClass(c, uncertain) {
  if (uncertain) return "conf-low";
  if (c === undefined || c === null) return "";
  if (c >= 0.85) return "conf-high";
  if (c >= 0.7) return "conf-mid";
  return "conf-low";
}

export default function InvoiceEditor({ invoice, onSaved }) {
  const [form, setForm] = useState(() => Object.fromEntries(FIELDS.map(([k]) => [k, invoice[k] ?? ""])));
  const [items, setItems] = useState(() => invoice.line_items.map((li) => ({ ...li })));
  const [itemsDirty, setItemsDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [by, setBy] = useState("");

  const fieldIssues = (f) => invoice.issues.filter((i) => i.field === f && i.severity !== "info");
  const dirty = FIELDS.some(([k]) => String(form[k] ?? "") !== String(invoice[k] ?? "")) || itemsDirty;

  async function save(markReviewed) {
    setSaving(true);
    setError(null);
    const body = { mark_reviewed: markReviewed, corrected_by: by || undefined };
    for (const [k, , type] of FIELDS) {
      if (String(form[k] ?? "") === String(invoice[k] ?? "")) continue;
      const v = form[k];
      body[k] = v === "" ? null : type === "number" ? Number(v) : v;
      if (type === "number" && v !== "" && Number.isNaN(Number(v))) { setError(`${k} must be a number`); setSaving(false); return; }
    }
    if (itemsDirty) {
      body.line_items = items.map((li) => ({
        description: li.description || null,
        quantity: li.quantity === "" || li.quantity == null ? null : Number(li.quantity),
        unit_price: li.unit_price === "" || li.unit_price == null ? null : Number(li.unit_price),
        discount_pct: li.discount_pct === "" || li.discount_pct == null ? null : Number(li.discount_pct),
        amount: li.amount === "" || li.amount == null ? null : Number(li.amount),
      }));
    }
    try { await api.correctInvoice(invoice.id, body); onSaved?.(); } catch (e) { setError(e); }
    setSaving(false);
  }

  // Subtotal not printed on the document (or filled by the system): keep it = sum of the line amounts while editing.
  const autoSubtotal = (invoice.derived_fields || []).includes("subtotal") || invoice.subtotal == null;
  const updateItems = (next) => {
    setItems(next);
    setItemsDirty(true);
    const nums = next.map((li) => (li.amount === "" || li.amount == null ? NaN : Number(li.amount)));
    if (autoSubtotal && nums.length && nums.every((n) => !Number.isNaN(n))) {
      setForm((f) => ({ ...f, subtotal: String(Math.round(nums.reduce((a, b) => a + b, 0) * 100) / 100) }));
    }
  };
  const setItem = (i, k, v) => updateItems(items.map((li, j) => (j === i ? { ...li, [k]: v } : li)));
  const errors = invoice.issues.filter((i) => i.severity === "error");
  const warnings = invoice.issues.filter((i) => i.severity === "warning");
  const infos = invoice.issues.filter((i) => i.severity === "info");

  return (
    <div className={`card invoice ${invoice.needs_review ? "review" : ""}`}>
      <div className="invoice-head">
        <h3>{invoice.invoice_number || "Invoice (number missing)"}</h3>
        <span className="small muted">pages {invoice.page_start}{invoice.page_end !== invoice.page_start ? `–${invoice.page_end}` : ""} · {invoice.extraction_method}{invoice.model ? ` · ${invoice.model}` : ""}</span>
        {invoice.needs_review ? <span className="badge s-needs_review">Needs review</span> : <span className="badge s-completed">Valid</span>}
        {invoice.reviewed && <span className="badge s-reviewed">Reviewed</span>}
      </div>

      {(errors.length > 0 || warnings.length > 0 || infos.length > 0) && (
        <ul className="issues">
          {errors.map((i, k) => <li key={"e" + k} className="sev-error"><b>{i.code}</b> {i.message}</li>)}
          {warnings.map((i, k) => <li key={"w" + k} className="sev-warning"><b>{i.code}</b> {i.message}</li>)}
          {infos.map((i, k) => <li key={"i" + k} className="sev-info"><b>{i.code}</b> {i.message}</li>)}
        </ul>
      )}

      <div className="fields">
        {FIELDS.map(([k, label, type]) => {
          const c = invoice.confidence?.[k];
          const unc = invoice.uncertain_fields?.includes(k);
          const iss = fieldIssues(k);
          return (
            <label key={k} className={`field ${confClass(c, unc)} ${iss.length ? "has-issue" : ""}`}>
              <span className="flabel">{label}
                <span className="conf" title="extraction confidence">{c !== undefined ? ` ${Math.round(c * 100)}%` : ""}{unc ? " · uncertain" : ""}</span>
                {invoice.derived_fields?.includes(k) && (
                  <span className="auto" title={k === "tax" ? "No tax on the document, so tax is 0" : "Not printed on the document: sum of the line amounts"}>
                    {k === "tax" ? " · not on document → 0" : " · = sum of amounts"}</span>
                )}
              </span>
              <input type={type === "number" ? "text" : type} inputMode={type === "number" ? "decimal" : undefined}
                value={form[k] ?? ""} placeholder="not found" onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
            </label>
          );
        })}
      </div>

      <table className="grid compact">
        <thead><tr><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Disc. %</th><th className="num">Amount</th><th /></tr></thead>
        <tbody>
          {items.map((li, i) => (
            <tr key={i}>
              <td><input value={li.description ?? ""} onChange={(e) => setItem(i, "description", e.target.value)} /></td>
              <td><input className="num" value={li.quantity ?? ""} onChange={(e) => setItem(i, "quantity", e.target.value)} /></td>
              <td><input className="num" value={li.unit_price ?? ""} onChange={(e) => setItem(i, "unit_price", e.target.value)} /></td>
              <td><input className="num" value={li.discount_pct ?? ""} onChange={(e) => setItem(i, "discount_pct", e.target.value)} /></td>
              <td><input className="num" value={li.amount ?? ""} onChange={(e) => setItem(i, "amount", e.target.value)} /></td>
              <td><button className="small" onClick={() => updateItems(items.filter((_, j) => j !== i))}>✕</button></td>
            </tr>
          ))}
          {!items.length && <tr><td colSpan={6} className="muted center">No line items found</td></tr>}
        </tbody>
      </table>
      <div className="toolbar">
        <button className="small" onClick={() => { setItems([...items, { description: "", quantity: "", unit_price: "", discount_pct: "", amount: "" }]); setItemsDirty(true); }}>+ Line item</button>
        <div className="spacer" />
        <input className="by" placeholder="Your name (audit log)" value={by} onChange={(e) => setBy(e.target.value)} />
        <button disabled={!dirty || saving} onClick={() => save(false)}>Save corrections</button>
        <button className="primary" disabled={saving} onClick={() => save(true)}>{dirty ? "Save & mark reviewed" : "Mark reviewed"}</button>
      </div>
      <ErrorBox error={error} />
    </div>
  );
}
