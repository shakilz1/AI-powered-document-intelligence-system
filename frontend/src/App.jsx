import React, { useCallback, useEffect, useState } from "react";
import { api, getApiKey, setApiKey } from "./api.js";
import Documents from "./components/Documents.jsx";
import DocumentDetail from "./components/DocumentDetail.jsx";
import ReviewQueue from "./components/ReviewQueue.jsx";
import Search from "./components/Search.jsx";
import CrossVerify from "./components/CrossVerify.jsx";
import LLMSwitch from "./components/LLMSwitch.jsx";

const TABS = [
  ["documents", "Upload & documents"],
  ["review", "Review queue"],
  ["search", "Search"],
  ["crossverify", "Cross-verify"],
];

export default function App() {
  const [tab, setTab] = useState("documents");
  const [openDoc, setOpenDoc] = useState(null);
  const [health, setHealth] = useState(null);
  const [stats, setStats] = useState(null);
  const [key, setKey] = useState(getApiKey());
  const [showKey, setShowKey] = useState(false);

  const refreshStats = useCallback(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.stats().then(setStats).catch(() => setStats(null));
  }, []);
  useEffect(() => {
    refreshStats();
    const t = setInterval(refreshStats, 5000);
    return () => clearInterval(t);
  }, [refreshStats]);

  const open = (id) => setOpenDoc(id);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">▣</span> Document Intelligence
        </div>
        <nav className="tabs">
          {TABS.map(([id, label]) => (
            <button key={id} className={tab === id && !openDoc ? "tab active" : "tab"}
              onClick={() => { setTab(id); setOpenDoc(null); }}>
              {label}
              {id === "review" && stats?.invoices_needing_review ? <span className="count">{stats.invoices_needing_review}</span> : null}
            </button>
          ))}
        </nav>
        <div className="sys">
          {health && <LLMSwitch onChanged={refreshStats} />}
          {health ? (
            <span className="pill" title={`OCR: ${health.ocr_engine} · embeddings: ${health.embedding_backend}`}>
              {health.llm ? `AI: ${health.llm_model}` : "AI: rule-based"} · queue {health.queue_inflight}
            </span>
          ) : <span className="pill bad">API offline</span>}
          <button className="link" onClick={() => setShowKey(!showKey)}>API key</button>
        </div>
      </header>
      {showKey && (
        <div className="keybar">
          <label>X-API-Key (only needed if the server sets API_KEY):</label>
          <input type="password" value={key} onChange={(e) => setKey(e.target.value)} />
          <button onClick={() => { setApiKey(key); setShowKey(false); refreshStats(); }}>Save</button>
        </div>
      )}
      {stats && (
        <section className="statsrow">
          <Stat label="Documents" value={Object.values(stats.documents || {}).reduce((a, b) => a + b, 0)} />
          <Stat label="Completed" value={stats.documents?.completed || 0} />
          <Stat label="Needs review" value={stats.documents?.needs_review || 0} tone="warn" />
          <Stat label="Failed" value={stats.documents?.failed || 0} tone="bad" />
          <Stat label="Invoices" value={stats.invoices} />
          <Stat label="Avg processing" value={`${Math.round(stats.avg_processing_ms)} ms`} />
        </section>
      )}
      <main>
        {openDoc ? (
          <DocumentDetail id={openDoc} onBack={() => setOpenDoc(null)} onChanged={refreshStats} />
        ) : tab === "documents" ? (
          <Documents onOpen={open} onChanged={refreshStats} />
        ) : tab === "review" ? (
          <ReviewQueue onOpen={open} />
        ) : tab === "search" ? (
          <Search onOpen={open} />
        ) : (
          <CrossVerify />
        )}
      </main>
    </div>
  );
}

function Stat({ label, value, tone }) {
  return (
    <div className={`stat ${tone || ""}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
