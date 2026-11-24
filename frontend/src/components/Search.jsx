import React, { useState } from "react";
import { api } from "../api.js";
import { ErrorBox, InvoiceTable } from "./common.jsx";

const EMPTY = { supplier: "", customer: "", invoice_number: "", currency: "", date_from: "", date_to: "", min_total: "", max_total: "", needs_review: "" };

export default function Search({ onOpen }) {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [filters, setFilters] = useState(EMPTY);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function run(e) {
    e?.preventDefault();
    setBusy(true); setError(null);
    try {
      const hasFilters = Object.values(filters).some((v) => v !== "");
      if (q && !hasFilters) {
        const r = await api.search(q, mode);
        setResult({ items: r.items, label: `${r.items.length} result(s) · ${mode}${r.embedding_backend ? ` (${r.embedding_backend} embeddings)` : ""}`, scored: mode !== "keyword" });
      } else {
        const r = await api.listInvoices({ q, ...filters, limit: 200 });
        setResult({ items: r.items, label: `${r.total} invoice(s) match the filters`, scored: false });
      }
    } catch (err) { setError(err); }
    setBusy(false);
  }

  const f = (k) => ({ value: filters[k], onChange: (e) => setFilters({ ...filters, [k]: e.target.value }) });

  return (
    <div>
      <form onSubmit={run}>
        <div className="toolbar">
          <input className="grow" placeholder='Search invoices, e.g. "steel bolts from Acme" or an invoice number' value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={mode} onChange={(e) => setMode(e.target.value)} title="Only used without filters">
            <option value="hybrid">Hybrid (keyword + semantic)</option>
            <option value="semantic">Semantic</option>
            <option value="keyword">Keyword</option>
          </select>
          <button className="primary" disabled={busy}>Search</button>
        </div>
        <details className="filters">
          <summary>Filters (keyword-based)</summary>
          <div className="filter-grid">
            <label>Supplier<input {...f("supplier")} /></label>
            <label>Customer<input {...f("customer")} /></label>
            <label>Invoice #<input {...f("invoice_number")} /></label>
            <label>Currency<input {...f("currency")} placeholder="USD" /></label>
            <label>Date from<input type="date" {...f("date_from")} /></label>
            <label>Date to<input type="date" {...f("date_to")} /></label>
            <label>Min total<input {...f("min_total")} inputMode="decimal" /></label>
            <label>Max total<input {...f("max_total")} inputMode="decimal" /></label>
            <label>Review state
              <select {...f("needs_review")}>
                <option value="">Any</option><option value="true">Needs review</option><option value="false">Valid</option>
              </select>
            </label>
          </div>
          <button type="button" className="small" onClick={() => setFilters(EMPTY)}>Clear filters</button>
        </details>
      </form>
      <ErrorBox error={error} />
      {result && (<><p className="muted">{result.label}</p><InvoiceTable items={result.items} onOpen={onOpen} showScore={result.scored} /></>)}
    </div>
  );
}
