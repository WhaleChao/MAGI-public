"""
Shared fixtures for MAGI test suite.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Candidate certification may be invoked with a host Python rather than the
# release launcher.  Set the live interpreter flag before test collection can
# import ``magi_v3`` so the immutable candidate never gains __pycache__ files.
sys.dont_write_bytecode = True

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEST_TMP_ROOT = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()
_TEST_AGENT_STATUS_PUBLIC_PATH = str(
    _TEST_TMP_ROOT / f"magi_pytest_agent_status_{os.getpid()}.json"
)
_TEST_ENV_DEFAULTS = {
    "MAGI_TEST_MODE": "1",
    "MAGI_NO_DELETE": "1",
    "MAGI_ENABLE_LIVE_TESTS": "0",
    "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
    "MAGI_DISABLE_BACKGROUND_THREADS": "1",
    "MAGI_DISABLE_BACKGROUND_WORKERS": "1",
    "MAGI_DISABLE_SCHEDULERS": "1",
    # Agent shadow telemetry is a mutable artifact. Never let an ordinary test
    # overwrite the dashboard status consumed by a running MAGI instance.
    "MAGI_AGENT_STATUS_PUBLIC_PATH": _TEST_AGENT_STATUS_PUBLIC_PATH,
    "MAGI_DRIVE_SYNC_CREATE_ON_CASE_FOLDER": "0",
    "MAGI_DRIVE_SYNC_ENABLE_WRITE": "0",
    "MAGI_GMAIL_ENABLE_SEND": "0",
    "MAGI_LAF_PORTAL_ENABLE_WRITE": "0",
    "MAGI_PORTAL_ENABLE_WRITE": "0",
    "MAGI_NAS_ENABLE_WRITE": "0",
    "MAGI_ALLOW_SYNOLOGY_DRIVE_FOLDER_CREATE": "0",
    "MAGI_ENABLE_NAS_FSWATCHER": "0",
    "MAGI_LAF_PORTAL_RETRY_ON_START": "0",
    "MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK": "0",
}
for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)
# This writer is a mutable public dashboard artifact, so unlike ordinary test
# defaults it must never inherit a production destination during collection.
os.environ["MAGI_AGENT_STATUS_PUBLIC_PATH"] = _TEST_AGENT_STATUS_PUBLIC_PATH

if str(_REPO_ROOT) in sys.path:
    sys.path.remove(str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT))
for _module_name, _module in list(sys.modules.items()):
    if _module_name == "api" or _module_name.startswith("api."):
        _module_file = str(getattr(_module, "__file__", "") or "")
        if _module_file and not _module_file.startswith(str(_REPO_ROOT)):
            sys.modules.pop(_module_name, None)


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_addoption(parser):
    parser.addoption(
        "--magi-live",
        action="store_true",
        default=False,
        help="Run tests marked live; equivalent to MAGI_ENABLE_LIVE_TESTS=1 for pytest selection.",
    )


def _explicit_live_request(config) -> bool:
    markexpr = str(getattr(config.option, "markexpr", "") or "").strip().lower()
    if not markexpr:
        return False
    # "not live" is the normal safe selector. Expressions that actively select
    # live, such as "-m live" or "-m 'live and slow'", are treated as opt-in.
    return "live" in markexpr and "not live" not in markexpr


def _live_tests_enabled(config) -> bool:
    return (
        _env_truthy("MAGI_ENABLE_LIVE_TESTS")
        or bool(config.getoption("--magi-live"))
        or _explicit_live_request(config)
    )


def _file_declares_live_marker(path: Path) -> bool:
    if path.suffix != ".py" or not path.name.startswith("test_"):
        return False
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")[:20000]
    except OSError:
        return False
    in_pytestmark = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("@pytest.mark.live"):
            return True
        if stripped.startswith("pytestmark") and "pytest.mark.live" in stripped:
            return True
        if stripped.startswith("pytestmark") and "=" in stripped:
            in_pytestmark = True
        elif in_pytestmark and "pytest.mark.live" in stripped:
            return True
        elif in_pytestmark and stripped.startswith("]"):
            in_pytestmark = False
    return False


def _argv_requests_live(argv: list[str]) -> bool:
    lowered = [str(arg).strip().lower() for arg in argv]
    if "--magi-live" in lowered:
        return True
    for idx, arg in enumerate(lowered):
        if arg == "-m" and idx + 1 < len(lowered):
            expr = lowered[idx + 1]
            return "live" in expr and "not live" not in expr
        if arg.startswith("-m") and len(arg) > 2:
            expr = arg[2:].strip()
            return "live" in expr and "not live" not in expr
    return False


collect_ignore = []
if not _env_truthy("MAGI_ENABLE_LIVE_TESTS") and not _argv_requests_live(sys.argv):
    collect_ignore = [
        str(path)
        for path in (_REPO_ROOT / "tests").glob("test_*.py")
        if _file_declares_live_marker(path)
    ]


def pytest_ignore_collect(collection_path, config):
    if _live_tests_enabled(config):
        return False
    path = Path(str(collection_path))
    if _file_declares_live_marker(path):
        return True
    return False


def pytest_collection_modifyitems(config, items):
    if _live_tests_enabled(config):
        return
    kept = []
    deselected = []
    for item in items:
        if item.get_closest_marker("live"):
            deselected.append(item)
        else:
            kept.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """Ensure required env vars are set for all tests."""
    defaults = {
        **_TEST_ENV_DEFAULTS,
        "MAGI_LINE_CHANNEL_ACCESS_TOKEN": "test_token",
        "MAGI_LINE_CHANNEL_SECRET": "test_secret",
        "DB_HOST": "127.0.0.1",
        "DB_USER": "test_user",
        "DB_PASSWORD": "test_pass",
        "FLASK_SECRET_KEY": "test_flask_secret",
        # Disable remote health gate in all tests; gate opt-in tests override this
        # with their own monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1").
        "MAGI_USE_REMOTE_HEALTH_GATE": "0",
        # Unit tests must not contend with a live file-review Chromium worker.
        "MAGI_FILE_REVIEW_PORTAL_LOCK_PATH": str(
            _TEST_TMP_ROOT / f"magi_test_file_review_portal_{os.getpid()}.lock"
        ),
        "MAGI_SAAS_MODE": "0",
        "MAGI_DEPLOYMENT_MODE": "single_host",
        "MAGI_TENANT_ID": "",
        "MAGI_TENANT_NAME": "",
        "MAGI_FORCE_HTTPS": "0",
        "MAGI_PUBLIC_SOURCE_ROOT_DIR": "",
        "MAGI_SOURCE_ROOT_DIR": "",
        "MAGI_ALLOW_PUBLIC_REGISTRATION": "0",
        "MAGI_ALLOW_CLOUDFLARE_WEB_UI": "0",
        # Disable NVIDIA NIM by default; tests that explicitly test NIM behaviour
        # (e.g. test_inference_gateway_heavy_fast_path.py) set NVIDIA_NIM_ENABLE=1
        # in their own setup_method / monkeypatch.setenv, overriding this default.
        "NVIDIA_NIM_ENABLE": "0",
        # Disable strict-NIM retry loop by default so unit tests that patch
        # run_nim_chat see exactly 1 call, regardless of whether .env has been
        # loaded by a previous test importing api.handlers.summary_handler or
        # api.handlers.translation_handler (both call load_dotenv() on import).
        "MAGI_HEAVY_STRICT_NIM": "0",
        "MAGI_HEAVY_STRICT_NIM_RETRIES": "0",
        # Unit tests must never call the public TLR endpoint unless they opt in
        # and mock the adapter explicitly.
        "MAGI_TWLEGALRAG_ENABLE": "0",
    }
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def magi_side_effect_fuses(monkeypatch):
    """Patch high-risk live writers so ordinary pytest cannot touch real services."""
    from tests.support import side_effect_guard

    side_effect_guard.install(monkeypatch)


@pytest.fixture
def mock_omlx_response():
    """Mock a successful oMLX HTTP response."""
    def _make(text="mock response", model="gemma-4-e4b-it-4bit"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": text}}],
            "model": model,
        }
        return resp
    return _make


@pytest.fixture
def mock_ollama_response():
    """Mock a successful Ollama HTTP response."""
    def _make(text="mock response", model="gemma-4-e4b"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "response": text,
            "model": model,
            "done": True,
        }
        return resp
    return _make
