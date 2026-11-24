import os
import random
import sys
import tempfile
from pathlib import Path

# Configure the app BEFORE it is imported: isolated DB/storage, synchronous processing, no LLM.
_TMP = Path(tempfile.mkdtemp(prefix="docintel_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "uploads")
os.environ["PROCESS_INLINE"] = "true"
os.environ["LLM_ENABLED"] = "false"
os.environ["EMBEDDING_BACKEND"] = "hashing"
os.environ.pop("API_KEY", None)
# Empty values win over backend/.env (the loader never overrides set variables): no real keys in tests.
for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ[_k] = ""
os.environ["LLM_PROVIDER"] = "auto"
os.environ["VISION_PROVIDER"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from eval import synth  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    from app.config import settings
    from app.db import init_db

    init_db()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def rng():
    return random.Random(1234)


@pytest.fixture
def make_invoice(rng):
    counter = iter(range(1, 10_000))

    def _make(prefix="TST"):
        return synth.random_invoice(rng, next(counter), prefix)

    return _make


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
