"""Test fixtures.

The API tests run the real FastAPI app and the real demo paper site
(``papersite/server.py``) in-process, so the "late short path" and the
"first request fails" behaviours are exercised end to end.
"""

import importlib
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

CRAWLER_DIR = Path(__file__).resolve().parents[1]
PAPERSITE_DIR = CRAWLER_DIR.parent / "papersite"

# so ``import app...`` works no matter where pytest is invoked from
if str(CRAWLER_DIR) not in sys.path:
    sys.path.insert(0, str(CRAWLER_DIR))


def _load_papersite_server():
    spec = importlib.util.spec_from_file_location(
        "papersite_server", PAPERSITE_DIR / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def papersite_server():
    return _load_papersite_server()


@pytest.fixture
def site(papersite_server, monkeypatch):
    """The demo corpus on an ephemeral port, with the delays shortened.

    The relative order is what matters: slow.html answers at 0.5s while the
    long chain has already claimed X, and X answers at 1.5s -- so X's request
    is still in flight when the short path lowers X's depth.  This is the
    exact interleaving of the bug.
    """
    monkeypatch.setattr(
        papersite_server,
        "DELAY_SECONDS",
        {"/papers/slow.html": 0.5, "/papers/X.html": 1.5},
    )
    monkeypatch.setattr(
        papersite_server, "FLAKY_PATHS", {"/papers/broken.html"}
    )
    papersite_server._already_failed.clear()

    httpd = papersite_server.Server(("127.0.0.1", 0), papersite_server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/papers/p0.html"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        papersite_server._already_failed.clear()


@pytest.fixture
def app_module(site, tmp_path, monkeypatch):
    """A freshly imported app bound to a throw-away SQLite file.

    ``site`` is a dependency so the paper site's port is fixed before workers
    start; the fixture itself only controls persistence and module state.
    """
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "crawler.db"))
    import app.main

    module = importlib.reload(app.main)
    yield module
