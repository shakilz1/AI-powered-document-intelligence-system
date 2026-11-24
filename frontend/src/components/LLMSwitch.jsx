import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Top-bar switch: "Local only" sends every document (typed and photos) to the local Ollama model.
// Off = the server's .env setup (e.g. hybrid: local for typed text, Gemini for photos).
export default function LLMSwitch({ onChanged }) {
  const [s, setS] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const load = () => api.llmSettings().then(setS).catch(() => setS(null));
  useEffect(() => { load(); }, []);

  if (!s) return null;

  const toggle = async () => {
    setBusy(true);
    setError(null);
    try {
      setS(await api.setLocalOnly(!s.local_only));
      onChanged?.();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const label = (p, m) => (p ? `${m}${p === "ollama" ? " (local)" : ""}` : "rules only");
  const route = s.text_provider === s.vision_provider
    ? `All documents: ${label(s.text_provider, s.text_model)}`
    : `Typed: ${label(s.text_provider, s.text_model)} · Photos: ${label(s.vision_provider, s.vision_model)}`;
  const warn = s.local_only && !s.ollama.running
    ? "Ollama is not running: documents will fall back to the rules. Start Ollama."
    : s.local_only && !s.ollama.model_installed
      ? `Model ${s.local_model} is not installed: run "ollama pull ${s.local_model}".`
      : null;

  return (
    <div className="llmswitch" title={`${route}\nApplies to documents processed from now on; use Reprocess to re-read older ones.`}>
      <label className="switch">
        <input type="checkbox" role="switch" checked={s.local_only} disabled={busy} onChange={toggle}
          aria-label="Use the local LLM only" />
        <span className="slider" aria-hidden="true" />
        <span className="switch-label">Local LLM only</span>
      </label>
      <span className={`small ${warn ? "err-text" : "muted"}`}>{warn || route}</span>
      {error && <span className="small err-text">{error}</span>}
    </div>
  );
}
