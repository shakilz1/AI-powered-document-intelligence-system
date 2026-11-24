"""Central configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=value lines). Real environment variables take precedence."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'data' / 'docintel.db').as_posix()}")
    )
    storage_dir: Path = field(default_factory=lambda: Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "data" / "uploads"))))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "200")))
    max_files_per_request: int = field(default_factory=lambda: int(os.getenv("MAX_FILES_PER_REQUEST", "50")))
    worker_mode: str = field(default_factory=lambda: os.getenv("WORKER_MODE", "process"))  # process|thread
    worker_threads: int =field(default_factory=lambda: int(os.getenv("WORKER_THREADS", str(max(2, (os.cpu_count() or 2) // 2)))))
    # Security: when API_KEY is set every /api request (except /api/health) needs header X-API-Key.
    api_key: str | None = field(default_factory=lambda: os.getenv("API_KEY") or None)
    cors_origins: list[str] = field(
        default_factory=lambda: [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
    )
    # LLM extraction
    llm_enabled: bool = field(default_factory=lambda: _bool("LLM_ENABLED", True))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-opus-5-5"))
    llm_effort: str = field(default_factory=lambda: os.getenv("LLM_EFFORT", "low"))
    llm_concurrency: int = field(default_factory=lambda: int(os.getenv("LLM_CONCURRENCY", "4")))
    llm_vision: bool = field(default_factory=lambda: _bool("LLM_VISION", True))  # send page images of OCR pages to Claude
    llm_max_images: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_IMAGES", "8")))
    llm_max_chars: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_CHARS", "60000")))
    # Provider: auto (Claude if ANTHROPIC_API_KEY, else Gemini if GEMINI_API_KEY, else Ollama if OLLAMA_MODEL)
    #           | anthropic | gemini | ollama
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "auto").strip().lower())
    # Optional different provider for photos/scans (hybrid): anthropic | gemini | ollama; empty = same as above
    vision_provider_setting: str = field(default_factory=lambda: os.getenv("VISION_PROVIDER", "").strip().lower())
    # Google Gemini (free tier available at aistudio.google.com). Aliases track the current Flash models.
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-flash-latest"))
    gemini_fallback_model: str = field(default_factory=lambda: os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest"))
    # Free-tier quotas are small: by default only photos/scans go to Gemini; typed PDFs use the rules.
    gemini_text: bool = field(default_factory=lambda: _bool("GEMINI_TEXT", False))
    # Local LLM via Ollama (no key, no quota, data never leaves the machine). LLM_PROVIDER=ollama selects it.
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen3.5:4b"))
    ollama_text: bool = field(default_factory=lambda: _bool("OLLAMA_TEXT", True))  # also typed PDFs/spreadsheets
    ollama_timeout: float = field(default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT", "600")))  # CPU is slow
    ollama_num_ctx: int = field(default_factory=lambda: int(os.getenv("OLLAMA_NUM_CTX", "16384")))
    ollama_think: bool = field(default_factory=lambda: _bool("OLLAMA_THINK", False))  # reasoning: slower, rarely needed
    # OCR
    ocr_min_text_chars: int = field(default_factory=lambda: int(os.getenv("OCR_MIN_TEXT_CHARS", "40")))
    ocr_threads: int = field(default_factory=lambda: int(os.getenv("OCR_THREADS", "0")))  # 0 = cpu_count / workers
    ocr_dpi: int =field(default_factory=lambda: int(os.getenv("OCR_DPI", "200")))
    ocr_low_confidence: float = field(default_factory=lambda: float(os.getenv("OCR_LOW_CONFIDENCE", "0.80")))
    # Embeddings: "auto" -> fastembed if installed else hashing; or force "hashing" / "fastembed"
    embedding_backend: str = field(default_factory=lambda: os.getenv("EMBEDDING_BACKEND", "auto"))
    # Validation tolerance for money comparisons
    money_abs_tolerance: float = field(default_factory=lambda: float(os.getenv("MONEY_ABS_TOLERANCE", "0.02")))
    money_rel_tolerance: float = field(default_factory=lambda: float(os.getenv("MONEY_REL_TOLERANCE", "0.0")))
    process_inline: bool = field(default_factory=lambda: _bool("PROCESS_INLINE", False))

    @staticmethod
    def gemini_key() -> str | None:
        return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None

    @property
    def provider(self) -> str | None:
        """The LLM provider in use, or None (rule-based only)."""
        if not self.llm_enabled:
            return None
        if self.local_only:
            return "ollama" if self._usable("ollama") else None
        has_claude =bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
        has_gemini = bool(self.gemini_key())
        if self.llm_provider in ("anthropic", "gemini", "ollama"):
            return self.llm_provider if self._usable(self.llm_provider) else None
        if has_claude:
            return "anthropic"
        if has_gemini:
            return "gemini"
        # auto: a local model is used only when explicitly configured (no surprise slow CPU runs)
        return "ollama" if os.getenv("OLLAMA_MODEL") else None

    # ---- runtime switch (UI toggle), shared with worker processes through a small JSON file
    @property
    def runtime_file(self) -> Path:
        return self.storage_dir.parent / "runtime_settings.json"

    def _runtime(self) -> dict:
        p = self.runtime_file
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return {}
        cache = getattr(self, "_rt_cache", None)
        if cache and cache[0] == (str(p), mtime):
            return cache[1]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self._rt_cache = ((str(p), mtime), data if isinstance(data, dict) else {})
        return self._rt_cache[1]

    @property
    def local_only(self) -> bool:
        """UI switch: send everything (typed text and photos) to the local Ollama model."""
        return bool(self._runtime().get("local_only"))

    def set_local_only(self, value: bool) -> None:
        data = dict(self._runtime(), local_only=bool(value))
        p = self.runtime_file
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)  # atomic: workers never read a half-written file

    def _usable(self, name: str) -> bool:
        if name == "anthropic":
            return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
        if name == "gemini":
            return bool(self.gemini_key())
        if name == "ollama":
            return bool(self.ollama_model)
        return False

    @property
    def vision_provider(self) -> str | None:
        """Provider for photos/scans. VISION_PROVIDER lets images use a different model than typed text
        (hybrid, e.g. Gemini for handwriting + local Ollama for everything else). Unset or unusable:
        same as `provider`."""
        if not self.llm_enabled:
            return None
        if self.local_only:
            return self.provider
        if self.vision_provider_setting and self._usable(self.vision_provider_setting):
            return self.vision_provider_setting
        return self.provider

    @property
    def anthropic_available(self) -> bool:
        return self.provider == "anthropic"

    @property
    def llm_available(self) -> bool:
        """A vision-capable LLM can be called (photos/scans are sent as images)."""
        return self.vision_provider is not None

    def model_for(self, provider: str | None) -> str | None:
        return {"anthropic": self.llm_model, "gemini": self.gemini_model,
                "ollama": self.ollama_model}.get(provider or "")

    @property
    def vision_model(self) -> str | None:
        return self.model_for(self.vision_provider)

    @property
    def text_llm_available(self) -> bool:
        """LLM is also used for text documents (digital PDFs, OCR text without images)."""
        p = self.provider
        return p == "anthropic" or (p == "gemini" and self.gemini_text) or (p == "ollama" and self.ollama_text)

    @property
    def active_model(self) -> str | None:
        return self.model_for(self.provider)


settings = Settings()
