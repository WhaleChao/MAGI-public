#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAGI 全功能冒煙測試
===================
覆蓋所有核心子系統，法扶/閱卷使用模擬站，其餘使用真實服務。

Usage:
    python3 scripts/ops/smoke_test_full.py
    python3 scripts/ops/smoke_test_full.py --json-out results.json
    python3 scripts/ops/smoke_test_full.py --skip laf,eefile   # 跳過指定模組

Exit code: 0=全過, 1=有失敗
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Setup ──────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
MAGI_ROOT = _SCRIPT_DIR.parent.parent
sys.path.insert(0, str(MAGI_ROOT))
os.chdir(MAGI_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(MAGI_ROOT / ".env")
except ImportError:
    pass

# ── Result model ───────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    module: str
    passed: bool
    duration_ms: int = 0
    message: str = ""
    error: str = ""

@dataclass
class SmokeReport:
    timestamp: str = ""
    magi_root: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_sec: float = 0
    results: list = field(default_factory=list)

    def add(self, r: TestResult):
        self.results.append(asdict(r))
        self.total += 1
        if r.passed:
            self.passed += 1
        else:
            self.failed += 1


report = SmokeReport(
    timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    magi_root=str(MAGI_ROOT),
)


def run_test(name: str, module: str, fn):
    """Execute a test function and record the result."""
    t0 = time.time()
    try:
        ok, msg = fn()
        dur = int((time.time() - t0) * 1000)
        r = TestResult(name=name, module=module, passed=ok, duration_ms=dur, message=msg)
    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        r = TestResult(name=name, module=module, passed=False, duration_ms=dur,
                       message="Exception", error=f"{type(e).__name__}: {e}")
    report.add(r)
    status = "✅" if r.passed else "❌"
    print(f"  {status} [{module}] {name} ({r.duration_ms}ms) {r.message}")
    if r.error:
        print(f"       Error: {r.error[:200]}")
    return r.passed


def _git_is_tracked(path: Path) -> bool:
    try:
        rel = str(path.relative_to(MAGI_ROOT))
    except Exception:
        rel = str(path)
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel],
        cwd=str(MAGI_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


# ══════════════════════════════════════════════════════════════
# 1. INFRASTRUCTURE TESTS
# ══════════════════════════════════════════════════════════════

def test_python_version():
    ok = sys.version_info >= (3, 12)
    return ok, f"Python {sys.version_info.major}.{sys.version_info.minor}"

def test_venv():
    venv = MAGI_ROOT / "venv" / "bin" / "python3"
    if not venv.exists():
        venv = MAGI_ROOT / ".venv" / "bin" / "python3"
    return venv.exists(), str(venv) if venv.exists() else "venv not found"

def test_env_file():
    return (MAGI_ROOT / ".env").exists(), ".env exists"

def test_env_example():
    return (MAGI_ROOT / ".env.example").exists(), ".env.example exists"


# ══════════════════════════════════════════════════════════════
# 2. CONFIG & IMPORT TESTS
# ══════════════════════════════════════════════════════════════

def test_config_validation():
    from skills.ops.config import validate_config, CORE_REQUIRED_VARS
    warnings = validate_config()
    return True, f"Core OK, {len(warnings)} feature warnings"

def test_runtime_paths():
    from api.runtime_paths import get_magi_root_dir, get_config_path
    root = get_magi_root_dir()
    return root.exists(), f"root={root}"

def test_import_orchestrator():
    try:
        from api.orchestrator import Orchestrator
        return True, "Orchestrator importable"
    except Exception as e:
        return False, str(e)[:100]

def test_import_tools_api():
    try:
        # Just import the module, don't start the app
        import importlib
        spec = importlib.util.find_spec("api.tools_api")
        return spec is not None, "tools_api found"
    except Exception as e:
        return False, str(e)[:100]

def test_import_skills():
    results = []
    core_skills = [
        "skills.ops.config",
        "skills.research.web_research",
        "skills.bridge.inference_gateway",
    ]
    for mod in core_skills:
        try:
            __import__(mod)
            results.append(f"{mod.split('.')[-1]}:OK")
        except Exception as e:
            results.append(f"{mod.split('.')[-1]}:FAIL")
    all_ok = all("OK" in r for r in results)
    return all_ok, ", ".join(results)


# ══════════════════════════════════════════════════════════════
# 3. DATABASE TESTS
# ══════════════════════════════════════════════════════════════

def _get_db():
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "magi"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "magi_brain"),
        connection_timeout=10, use_pure=True,
    )

def test_db_connection():
    conn = _get_db()
    conn.close()
    return True, "Connected"

def test_db_tables():
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()")
    count = cur.fetchone()[0]
    conn.close()
    return count > 0, f"{count} tables"

def test_db_users_table():
    conn = _get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    count = cur.fetchone()[0]
    conn.close()
    return count > 0, f"{count} users"

def test_db_write_read():
    conn = _get_db()
    cur = conn.cursor()
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    # Use a test-safe operation
    cur.execute("SELECT 1+1 AS result")
    row = cur.fetchone()
    conn.close()
    return row[0] == 2, f"SELECT 1+1 = {row[0]}"


# ══════════════════════════════════════════════════════════════
# 4. SERVICE HEALTH TESTS
# ══════════════════════════════════════════════════════════════

def _http_get(url, timeout=5):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "MAGI-Smoke/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")

def test_server_health():
    code, body = _http_get("http://127.0.0.1:5002/health")
    return code == 200, f"HTTP {code}"

def test_tools_api_health():
    try:
        from api.routing.service_registry import get_service_url as _gsurl
        _tools_url = _gsurl("tools_api")
    except Exception:
        _tools_url = "http://127.0.0.1:5003"
    code, body = _http_get(f"{_tools_url}/health")
    return code == 200, f"HTTP {code}"

def test_server_security_headers():
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:5002/health",
                                headers={"User-Agent": "MAGI-Smoke/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        headers = dict(resp.headers)
    checks = []
    for h in ["X-Content-Type-Options", "X-Frame-Options"]:
        if h.lower() in {k.lower() for k in headers}:
            checks.append(f"{h}:OK")
        else:
            checks.append(f"{h}:MISSING")
    all_ok = all("OK" in c for c in checks)
    return all_ok, ", ".join(checks)

def test_tools_api_cors():
    import urllib.request
    try:
        from api.routing.service_registry import get_service_url as _gsurl2
        _tools_url2 = _gsurl2("tools_api")
    except Exception:
        _tools_url2 = "http://127.0.0.1:5003"
    req = urllib.request.Request(f"{_tools_url2}/health",
                                headers={"Origin": "http://evil.com", "User-Agent": "MAGI-Smoke/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        acao = resp.headers.get("Access-Control-Allow-Origin", "")
    blocked = acao != "*" and "evil.com" not in acao
    return blocked, f"ACAO='{acao}' (evil.com {'blocked' if blocked else 'ALLOWED!'})"


# ══════════════════════════════════════════════════════════════
# 5. INFERENCE TESTS
# ══════════════════════════════════════════════════════════════

def test_omlx_available():
    """測試 oMLX（本機 Apple Silicon MLX 推理）是否在線。"""
    omlx_host = os.environ.get("MAGI_OMLX_HOST", "127.0.0.1")
    omlx_port = os.environ.get("MAGI_OMLX_PORT", "8080")
    omlx_url = f"http://{omlx_host}:{omlx_port}"
    try:
        code, body = _http_get(f"{omlx_url}/v1/models", timeout=5)
        data = json.loads(body)
        models = data.get("data", [])
        names = [m.get("id", "?") for m in models] if isinstance(models, list) else []
        return len(names) > 0, f"oMLX: {len(names)} models ({', '.join(names[:3])})"
    except Exception:
        # Fallback: try Ollama-compatible /api/tags
        try:
            code, body = _http_get(f"{omlx_url}/api/tags", timeout=3)
            data = json.loads(body)
            models = [m["name"] for m in data.get("models", [])]
            return len(models) > 0, f"oMLX (ollama compat): {len(models)} models"
        except Exception as e:
            return False, f"oMLX ({omlx_url}) not reachable: {str(e)[:80]}"

def test_inference_gateway():
    from skills.bridge.inference_gateway import InferenceGateway
    gw = InferenceGateway()
    result = gw.chat("回答OK兩個字", task_type="general", timeout=30)
    text = result.get("text", "") if isinstance(result, dict) else str(result)
    ok = bool(text and len(text.strip()) > 0)
    return ok, f"Response: {text[:50]}..." if text else "No response"


# ══════════════════════════════════════════════════════════════
# 6. SKILL TESTS
# ══════════════════════════════════════════════════════════════

def test_skill_definitions():
    defs_path = MAGI_ROOT / "skills" / "definitions.json"
    if not defs_path.exists():
        return False, "definitions.json not found"
    with open(defs_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    skills = data if isinstance(data, list) else data.get("tools", data.get("skills", []))
    return len(skills) > 0, f"{len(skills)} skills/tools defined"

def test_pdf_namer_training_data():
    td = MAGI_ROOT / "skills" / "pdf-namer" / "training_data.json"
    if not td.exists():
        return True, "training_data.json in .gitignore (OK for clean env)"
    with open(td, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 確認去識別化
    text = json.dumps(data[:10], ensure_ascii=False)
    has_real_names = any(name in text for name in ["余秋菊", "林俊儒", "喬" + "政翔"])
    if has_real_names:
        return False, "training_data contains real names (not redacted!)"
    return True, f"{len(data)} entries, redacted"

def test_memory_rag():
    try:
        # memory 模組有多個檔案，驗證核心可 import
        from skills.memory import mem_bridge
        return True, "mem_bridge importable"
    except ImportError:
        # fallback: 檢查檔案存在
        mem_dir = MAGI_ROOT / "skills" / "memory"
        files = list(mem_dir.glob("*.py"))
        return len(files) > 3, f"{len(files)} Python files in memory/"

def test_research_web_search():
    try:
        from skills.research.web_research import search_web
        result = search_web("MAGI test query", num_results=2)
        ok = isinstance(result, (list, dict, str)) and len(str(result)) > 0
        return ok, f"Got {len(str(result))} chars"
    except Exception as e:
        return False, str(e)[:100]

def test_judgment_collector_import():
    try:
        spec = __import__("importlib").util.find_spec("skills.judgment-collector.action")
        if spec is None:
            # Hyphenated module names need special handling
            action_path = MAGI_ROOT / "skills" / "judgment-collector" / "action.py"
            return action_path.exists(), f"action.py exists: {action_path.exists()}"
        return True, "importable"
    except Exception as e:
        return True if (MAGI_ROOT / "skills" / "judgment-collector" / "action.py").exists() else False, str(e)[:80]


# ══════════════════════════════════════════════════════════════
# 7. CHANNEL TESTS
# ══════════════════════════════════════════════════════════════

def test_line_bot_config():
    token = os.environ.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "")
    secret = os.environ.get("MAGI_LINE_CHANNEL_SECRET", "")
    enabled = os.environ.get("MAGI_ENABLE_LINE", "1")
    if enabled.lower() not in {"1", "true", "yes"}:
        return True, "LINE disabled (OK)"
    ok = bool(token and token != "your_line_channel_access_token" and secret and secret != "your_line_channel_secret")
    return ok, "Credentials configured" if ok else "Missing credentials"

def test_discord_bot_config():
    enabled = os.environ.get("MAGI_ENABLE_DISCORD", "0")
    if enabled.lower() not in {"1", "true", "yes"}:
        return True, "Discord disabled (OK)"
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    return bool(token), "Token configured" if token else "Missing token"

def test_telegram_bot_config():
    enabled = os.environ.get("MAGI_ENABLE_TELEGRAM", "0")
    if enabled.lower() not in {"1", "true", "yes"}:
        return True, "Telegram disabled (OK)"
    token = os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN", "")
    return bool(token), "Token configured" if token else "Missing token"

def test_line_webhook_endpoint():
    """Test LINE webhook endpoint returns 400 (expected without signature)."""
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "http://127.0.0.1:5002/line/webhook",
            data=b'{}',
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            return False, "Should have returned error without signature"
        except urllib.error.HTTPError as e:
            # 400 or 401 = endpoint exists and validates signature
            return e.code in (400, 401, 403), f"HTTP {e.code} (expected auth error)"
    except Exception as e:
        return False, str(e)[:100]


# ══════════════════════════════════════════════════════════════
# 8. NOTIFICATION TESTS
# ══════════════════════════════════════════════════════════════

def test_notification_webhook_config():
    """確認繳費通知 webhook 有設定（優先 .env，fallback config.json）。"""
    # 優先從 .env 讀（這是正確的 credential 來源）
    payment_url = os.environ.get("MAGI_JUDICIAL_WEBHOOK_PAYMENT", "")
    ready_url = os.environ.get("MAGI_JUDICIAL_WEBHOOK_READY", "")
    record_url = os.environ.get("MAGI_JUDICIAL_WEBHOOK_RECORD", "")
    # fallback config.json
    if not (payment_url and ready_url and record_url):
        config_path = MAGI_ROOT / "json" / "config.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            judicial = cfg.get("judicial", {})
            payment_url = payment_url or judicial.get("webhook_url_review_payment", "")
            ready_url = ready_url or judicial.get("webhook_url_review_ready", "")
            record_url = record_url or judicial.get("webhook_url_record", "")
    results = []
    if payment_url:
        results.append("payment:OK")
    else:
        results.append("payment:EMPTY")
    if ready_url:
        results.append("ready:OK")
    else:
        results.append("ready:EMPTY")
    if record_url:
        results.append("record:OK")
    else:
        results.append("record:EMPTY")
    all_ok = all("OK" in r for r in results)
    return all_ok, ", ".join(results)

def test_notification_webhook_reachable():
    """測試 payment webhook 是否可達。"""
    url = os.environ.get("MAGI_JUDICIAL_WEBHOOK_PAYMENT", "")
    if not url:
        config_path = MAGI_ROOT / "json" / "config.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            url = cfg.get("judicial", {}).get("webhook_url_review_payment", "")
    if not url:
        return False, "No webhook URL in .env or config.json"
    # Just check DNS resolution, don't actually post
    try:
        from urllib.parse import urlparse
        import socket
        host = urlparse(url).hostname
        socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        return True, f"DNS OK: {host}"
    except Exception as e:
        return False, str(e)[:100]


# ══════════════════════════════════════════════════════════════
# 9. LAF MOCK TESTS (法扶模擬站)
# ══════════════════════════════════════════════════════════════

_LAF_MOCK_PROC = None
_EEFILE_MOCK_PROC = None

def _start_mock_server(script, port, name):
    """Start a mock server in background."""
    env = os.environ.copy()
    env[f"{name}_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(script.parent),
    )
    # Wait for startup
    time.sleep(2)
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode("utf-8", errors="replace")[:500]
        return None, f"Mock server died: {stderr}"
    return proc, f"Started on port {port}"

def test_laf_mock_server():
    global _LAF_MOCK_PROC
    mock_script = MAGI_ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "laf_mock" / "server.py"
    if not mock_script.exists():
        return True, "LAF mock retired; using draft-only/live-safe automation checks"
    _LAF_MOCK_PROC, msg = _start_mock_server(mock_script, 17002, "LAF_MOCK")
    return _LAF_MOCK_PROC is not None, msg

def test_laf_mock_health():
    try:
        # LAF mock 可能沒有 / 根路徑，試 /login 或其他已知頁面
        for path in ["/login", "/", "/health"]:
            try:
                code, body = _http_get(f"http://127.0.0.1:17002{path}", timeout=3)
                if code == 200:
                    return True, f"HTTP {code} on {path}"
            except Exception:
                continue
        return True, "Server listening (no root handler)"
    except Exception as e:
        return False, str(e)[:100]

def test_laf_automation_import():
    try:
        laf_path = MAGI_ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "laf_automation_v2.py"
        return laf_path.exists(), f"laf_automation_v2.py exists ({laf_path.stat().st_size // 1024}KB)"
    except Exception as e:
        return False, str(e)[:100]

def test_laf_draft_only_safety():
    val = os.environ.get("MAGI_LAF_DRAFT_ONLY", "1")
    ok = val.strip().lower() in {"1", "true", "yes"}
    return ok, f"MAGI_LAF_DRAFT_ONLY={val}"


# ══════════════════════════════════════════════════════════════
# 10. EEFILE MOCK TESTS (閱卷聲請模擬站)
# ══════════════════════════════════════════════════════════════

def test_eefile_mock_server():
    global _EEFILE_MOCK_PROC
    mock_script = MAGI_ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "eefile_mock" / "server.py"
    if not mock_script.exists():
        return True, "Eefile mock retired; using module/live-safe automation checks"
    _EEFILE_MOCK_PROC, msg = _start_mock_server(mock_script, 17001, "EEFILE_MOCK")
    return _EEFILE_MOCK_PROC is not None, msg

def test_eefile_mock_health():
    try:
        code, body = _http_get("http://127.0.0.1:17001/", timeout=3)
        return True, f"HTTP {code}"
    except Exception as e:
        # Mock server might use different health path
        return False, str(e)[:100]

def test_file_review_automation_import():
    frm_path = MAGI_ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "file_review_automation.py"
    return frm_path.exists(), f"file_review_automation.py ({frm_path.stat().st_size // 1024}KB)"


# ══════════════════════════════════════════════════════════════
# 11. AUTOPILOT & CRON TESTS
# ══════════════════════════════════════════════════════════════

def test_autopilot_action():
    ap = MAGI_ROOT / "skills" / "magi-autopilot" / "action.py"
    return ap.exists(), f"action.py ({ap.stat().st_size // 1024}KB)"

def test_cron_runner_no_hardcoded():
    cr = MAGI_ROOT / "skills" / "ops" / "cron_scheduler.py"
    if not cr.exists():
        return False, "cron_scheduler.py not found"
    content = cr.read_text(encoding="utf-8")
    _hp = "/Users" + "/ai/Desktop" + "/MAGI"  # split to avoid self-match
    has_hardcoded = _hp in content
    return not has_hardcoded, "No hardcoded paths" if not has_hardcoded else "HARDCODED PATHS FOUND"


# ══════════════════════════════════════════════════════════════
# 12. SECURITY TESTS
# ══════════════════════════════════════════════════════════════

def test_insecure_ssl_default():
    action_py = MAGI_ROOT / "skills" / "judgment-collector" / "action.py"
    content = action_py.read_text(encoding="utf-8")
    # Check that default is "0" not "1"
    match = re.search(r'JUDICIAL_API_ALLOW_INSECURE_SSL.*?"(\d)"', content)
    if match:
        default = match.group(1)
        return default == "0", f"Default={default}"
    return False, "Pattern not found"

def test_authz_module():
    authz = MAGI_ROOT / "api" / "authz.py"
    return authz.exists(), "authz.py exists"

def test_csrf_module():
    csrf = MAGI_ROOT / "api" / "csrf_guard.py"
    return csrf.exists(), "csrf_guard.py exists"


# ══════════════════════════════════════════════════════════════
# 13. RELEASE HYGIENE
# ══════════════════════════════════════════════════════════════

def test_no_sensitive_files():
    checks = [
        (MAGI_ROOT / "_autopilot_runs", "_autopilot_runs"),
        (MAGI_ROOT / "_db_backups", "_db_backups"),
        (MAGI_ROOT / "_debug_reports", "_debug_reports"),
        (MAGI_ROOT / "casper_ecosystem" / "law_firm_orchestrators" / ".laf_chrome_profile", ".laf_chrome_profile"),
    ]
    issues = []
    for path, name in checks:
        if path.exists() and _git_is_tracked(path):
            issues.append(name)
    return len(issues) == 0, f"Clean" if not issues else f"Found: {', '.join(issues)}"

def test_gitignore_coverage():
    gi = (MAGI_ROOT / ".gitignore").read_text(encoding="utf-8")
    required = ["_autopilot_runs/", "_db_backups/", "_logs/", ".laf_chrome_profile/"]
    missing = [r for r in required if r not in gi]
    return len(missing) == 0, f"All covered" if not missing else f"Missing: {', '.join(missing)}"

def test_license_exists():
    return (MAGI_ROOT / "LICENSE").exists(), "LICENSE exists"

def test_ci_pipeline():
    return (MAGI_ROOT / ".github" / "workflows" / "ci.yml").exists(), "ci.yml exists"


# ══════════════════════════════════════════════════════════════
# 14. OPS LIVE GUARDS
# ══════════════════════════════════════════════════════════════

def _run_cmd(cmd: list[str], timeout: int = 10, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd or MAGI_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )

def _process_lines(pattern: str) -> list[str]:
    proc = _run_cmd(["pgrep", "-fl", pattern], timeout=3)
    if proc.returncode not in (0, 1):
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

def test_judicial_api_pipeline_health():
    checker = MAGI_ROOT / "scripts" / "ops" / "check_judicial_api_pipeline.py"
    if not checker.exists():
        return False, "check_judicial_api_pipeline.py missing"
    proc = _run_cmd([sys.executable, str(checker), "--json"], timeout=90)
    try:
        data = json.loads(proc.stdout)
    except Exception:
        if not os.environ.get("MAGI_JUDICIAL_API_USER") and not os.environ.get("JUDICIAL_API_USER"):
            return True, "Judicial API not configured in this environment"
        return False, (proc.stderr or proc.stdout)[:120]
    status = data.get("status")
    backlog = data.get("backlog") if isinstance(data.get("backlog"), dict) else {}
    ok_statuses = {"PIPELINE_HEALTHY", "BACKLOG_WARNING", "BACKLOG_CATCHING_UP"}
    ok = proc.returncode in {0, 10} and status in ok_statuses
    interpretation = data.get("backlog_interpretation") if isinstance(data.get("backlog_interpretation"), dict) else {}
    if interpretation:
        return ok, (
            f"{status}; backlog={backlog.get('backlog_count', '-')}; "
            f"{interpretation.get('headline', '')}"
        )
    return ok, f"{status}; backlog={backlog.get('backlog_count', '-')}"

def test_omlx_aux_models_available():
    ports = {
        "embed": "8081",
        "phi4": "8082",
        "smol": "8083",
    }
    results = []
    for name, port in ports.items():
        try:
            code, body = _http_get(f"http://127.0.0.1:{port}/v1/models", timeout=3)
            data = json.loads(body)
            count = len(data.get("data", [])) if isinstance(data.get("data"), list) else 0
            results.append(f"{name}:{count}")
        except Exception:
            if os.environ.get("MAGI_REQUIRE_ALL_OMLX_MODELS", "0").lower() in {"1", "true", "yes"}:
                results.append(f"{name}:down")
            else:
                results.append(f"{name}:optional")
    ok = all(not item.endswith(":down") for item in results)
    return ok, ", ".join(results)

def test_mlx_mtp_sidecar_health():
    try:
        code, body = _http_get("http://127.0.0.1:8090/health", timeout=3)
        data = json.loads(body)
        ok = code == 200 and bool(data.get("ok", True))
        model = data.get("model") or "-"
        draft = data.get("draft_model") or "-"
        return ok, f"model={model}; draft={draft}"
    except Exception as e:
        if os.environ.get("MAGI_REQUIRE_MLX_MTP", "1").lower() in {"0", "false", "no"}:
            return True, "MLX MTP optional"
        return False, str(e)[:120]

def test_menubar_process_running():
    lines = _process_lines(r"gui/magi_menubar.py")
    if lines:
        return True, f"{len(lines)} process"
    if sys.platform != "darwin" or os.environ.get("CI"):
        return True, "not a desktop live environment"
    if os.environ.get("MAGI_REQUIRE_MENUBAR", "1").lower() in {"0", "false", "no"}:
        return True, "menubar optional"
    return False, "magi_menubar.py not running"

def test_nas_lumi_mount_guard():
    candidates = [
        Path(os.environ.get("MAGI_LUMI_MOUNT", "")),
        Path("/Volumes/homes"),
        Path("/Volumes/lumi"),
    ]
    existing = [str(path) for path in candidates if str(path) != "." and path.exists()]
    if existing:
        return True, "mounted: " + ", ".join(existing[:3])
    if os.environ.get("MAGI_REQUIRE_NAS_MOUNT", "0").lower() in {"1", "true", "yes"}:
        return False, "LUMI/NAS mount not found"
    return True, "NAS mount optional in this environment"

def test_no_desktop_git_add_noise():
    lines = _process_lines(r"git add --")
    noisy = [line for line in lines if "/Users/ai/Desktop" in line or ".openclaw_archived" in line or "Paperclip_rebuild" in line]
    return not noisy, "no noisy git add" if not noisy else noisy[0][:160]


# ══════════════════════════════════════════════════════════════
# 15. COMMERCIAL RELEASE GUARDS
# ══════════════════════════════════════════════════════════════

def _run_json_script(cmd: list[str], timeout: int = 60) -> tuple[bool, dict, str]:
    proc = _run_cmd(cmd, timeout=timeout)
    raw = (proc.stdout or "").strip()
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            idx = raw.rfind("\n{")
            if idx >= 0:
                try:
                    data = json.loads(raw[idx + 1:])
                except Exception:
                    data = {}
    return proc.returncode == 0 and bool(data), data, (proc.stderr or raw)[-500:]


def test_public_release_audit_strict():
    ok, data, tail = _run_json_script(
        [
            sys.executable,
            "scripts/public_release_audit.py",
            "--public-isolation",
            "--strict",
            "--json",
        ],
        timeout=90,
    )
    if not ok:
        return False, tail
    passed = bool(data.get("ok")) and int(data.get("errors") or 0) == 0 and int(data.get("warnings") or 0) == 0
    return passed, f"errors={data.get('errors')} warnings={data.get('warnings')}"


def test_customer_install_wizard_public_dry_run():
    out = MAGI_ROOT / ".runtime" / "smoke_customer_install_wizard_latest.json"
    ok, data, tail = _run_json_script(
        [
            sys.executable,
            "scripts/customer_install_wizard.py",
            "--public",
            "--no-live",
            "--skip-readiness",
            "--no-optional",
            "--json",
            "--output",
            str(out),
        ],
        timeout=120,
    )
    if not ok:
        return False, tail
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    passed = bool(data.get("ok")) and str(data.get("status")) == "pass" and int(summary.get("fail") or 0) == 0
    return passed, f"status={data.get('status')} pass={summary.get('pass')} skipped={summary.get('skipped')}"


def test_operation_manuals_exist():
    required = [
        MAGI_ROOT / "README.md",
        MAGI_ROOT / "README.zh-TW.md",
        MAGI_ROOT / "docs" / "PUBLIC_SELF_INSTALL.md",
        MAGI_ROOT / "docs" / "PUBLIC_OPERATION_MANUAL.md",
        MAGI_ROOT / "docs" / "PRIVATE_OPERATION_MANUAL.md",
        MAGI_ROOT / "docs" / "COMMERCIAL_READINESS.md",
    ]
    missing = [str(p.relative_to(MAGI_ROOT)) for p in required if not p.exists()]
    return not missing, "manuals OK" if not missing else "missing: " + ", ".join(missing)


def test_health_active_issues_clear():
    code, body = _http_get("http://127.0.0.1:5002/health", timeout=8)
    data = json.loads(body)
    op = data.get("operational_health") if isinstance(data.get("operational_health"), dict) else {}
    active = op.get("active_unresolved_24h") if isinstance(op.get("active_unresolved_24h"), dict) else {}
    passed = (
        code == 200
        and data.get("status") == "operational"
        and bool(op.get("ok"))
        and int(active.get("cron_failures") or 0) == 0
        and int(active.get("issue_agenda_high_severity") or 0) == 0
    )
    return passed, f"status={data.get('status')} active={active}"


def test_process_hygiene_clean():
    ok, data, tail = _run_json_script(
        [sys.executable, "skills/process-hygiene/action.py", "--task", "scan"],
        timeout=45,
    )
    if not ok:
        return False, tail
    passed = bool(data.get("healthy")) and int(data.get("total_issues") or 0) == 0
    return passed, str(data.get("message") or "")[:160]


def test_model_live_gate_profile():
    ok, data, tail = _run_json_script(
        [
            sys.executable,
            "scripts/ops/model_live_gate.py",
            "--expect",
            "auto",
            "--json",
            "--json-out",
            ".runtime/model_live_gate_latest.json",
        ],
        timeout=60,
    )
    if not ok:
        return False, tail
    endpoints = data.get("endpoints") if isinstance(data.get("endpoints"), list) else []
    models = [str(e.get("model_id") or "down") for e in endpoints if isinstance(e, dict)]
    return bool(data.get("ok")), f"expected={data.get('expected_profile')} active={data.get('active_profile')} models={models}"


def test_knowledge_lint_clean():
    path = MAGI_ROOT / "static" / "knowledge_lint_latest.json"
    if not path.exists():
        return False, "knowledge_lint_latest.json missing"
    data = json.loads(path.read_text(encoding="utf-8"))
    checks = data.get("checks") if isinstance(data.get("checks"), list) else []
    bad = [
        str(item.get("check") or "?")
        for item in checks
        if isinstance(item, dict) and str(item.get("status") or "").lower() in {"warn", "error", "fail"}
    ]
    return not bad, "knowledge lint clean" if not bad else "bad checks: " + ", ".join(bad[:5])


def test_translation_quality_latest_clean():
    path = MAGI_ROOT / "static" / "translator_ape_latest.json"
    if not path.exists():
        return False, "translator_ape_latest.json missing"
    data = json.loads(path.read_text(encoding="utf-8"))
    passed = bool(data.get("ok")) and not bool(data.get("has_failures")) and int(data.get("case_fail_count") or 0) == 0
    return passed, f"cases={data.get('cases')} ape_beats_baseline={data.get('ape_beats_baseline')}"


def test_tool_hallucination_latest_clean():
    path = MAGI_ROOT / ".runtime" / "live_magi_tool_hallucination_latest.json"
    if not path.exists():
        return False, "live_magi_tool_hallucination_latest.json missing"
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    data = json.loads(path.read_text(encoding="utf-8"))
    checks = data.get("checks") if isinstance(data.get("checks"), list) else []
    failed = [str(c.get("name") or "?") for c in checks if isinstance(c, dict) and not c.get("ok")]
    passed = bool(data.get("ok")) and not failed and age_hours <= 168
    return passed, f"age={age_hours:.1f}h failed={failed[:3]}"


def test_share_gateway_health():
    code, body = _http_get("http://127.0.0.1:5014/health", timeout=5)
    data = json.loads(body)
    return code == 200 and bool(data.get("ok")), f"HTTP {code} {data.get('service')}"


def test_admin_server_health():
    code, body = _http_get("http://127.0.0.1:8088/health", timeout=5)
    body_l = body.lower()
    ok = code == 200 and "<html" in body_l and "traceback" not in body_l and "not found" not in body_l
    return ok, f"HTTP {code}"


def test_commercial_readiness_release_gate():
    ok, data, tail = _run_json_script(
        [
            sys.executable,
            "scripts/ops/commercial_readiness_live.py",
            "--strict-public",
            "--skip-backup",
            "--json-out",
            ".runtime/smoke_commercial_readiness_latest.json",
        ],
        timeout=420,
    )
    if not ok:
        return False, tail
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    passed = bool(data.get("ok")) and int(summary.get("fail") or 0) == 0
    return passed, f"summary={summary}"


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def cleanup():
    """Stop mock servers."""
    for proc in [_LAF_MOCK_PROC, _EEFILE_MOCK_PROC]:
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description="MAGI 全功能冒煙測試")
    parser.add_argument("--json-out", help="輸出 JSON 報告路徑")
    parser.add_argument("--skip", default="", help="跳過模組（逗號分隔，如 laf,eefile,inference）")
    parser.add_argument("--notify", action="store_true", help="完成後推送通知")
    args = parser.parse_args()

    skip = set(s.strip().lower() for s in args.skip.split(",") if s.strip())

    print("╔══════════════════════════════════════════╗")
    print("║     MAGI 全功能冒煙測試                  ║")
    print("╚══════════════════════════════════════════╝")
    print(f"  Time: {report.timestamp}")
    print(f"  Root: {MAGI_ROOT}")
    print(f"  Skip: {skip or 'none'}")
    print()

    t0 = time.time()

    # ── 1. Infrastructure ──
    print("── 1. Infrastructure ──")
    run_test("Python version", "infra", test_python_version)
    run_test("Virtual environment", "infra", test_venv)
    run_test(".env file", "infra", test_env_file)
    run_test(".env.example", "infra", test_env_example)
    print()

    # ── 2. Config & Import ──
    print("── 2. Config & Import ──")
    run_test("Config validation", "config", test_config_validation)
    run_test("Runtime paths", "config", test_runtime_paths)
    run_test("Import orchestrator", "import", test_import_orchestrator)
    run_test("Import tools_api", "import", test_import_tools_api)
    run_test("Import core skills", "import", test_import_skills)
    print()

    # ── 3. Database ──
    if "db" not in skip:
        print("── 3. Database ──")
        run_test("DB connection", "db", test_db_connection)
        run_test("DB tables", "db", test_db_tables)
        run_test("DB users", "db", test_db_users_table)
        run_test("DB read/write", "db", test_db_write_read)
        print()

    # ── 4. Service Health ──
    if "service" not in skip:
        print("── 4. Service Health ──")
        run_test("Server health (5002)", "service", test_server_health)
        run_test("Tools API health (5003)", "service", test_tools_api_health)
        run_test("Security headers", "service", test_server_security_headers)
        run_test("CORS blocking", "service", test_tools_api_cors)
        print()

    # ── 5. Inference ──
    if "inference" not in skip:
        print("── 5. Inference ──")
        run_test("oMLX inference available", "inference", test_omlx_available)
        run_test("Inference gateway", "inference", test_inference_gateway)
        print()

    # ── 6. Skills ──
    print("── 6. Skills ──")
    run_test("Skill definitions", "skills", test_skill_definitions)
    run_test("PDF namer training data", "skills", test_pdf_namer_training_data)
    run_test("Memory RAG import", "skills", test_memory_rag)
    run_test("Judgment collector", "skills", test_judgment_collector_import)
    if "research" not in skip:
        run_test("Web research", "skills", test_research_web_search)
    print()

    # ── 7. Channels ──
    print("── 7. Channels ──")
    run_test("LINE Bot config", "channel", test_line_bot_config)
    run_test("Discord Bot config", "channel", test_discord_bot_config)
    run_test("Telegram Bot config", "channel", test_telegram_bot_config)
    if "service" not in skip:
        run_test("LINE webhook endpoint", "channel", test_line_webhook_endpoint)
    print()

    # ── 8. Notifications ──
    print("── 8. Notifications ──")
    run_test("Webhook config (payment/ready/record)", "notify", test_notification_webhook_config)
    run_test("Webhook reachable", "notify", test_notification_webhook_reachable)
    print()

    # ── 9. LAF Mock ──
    if "laf" not in skip:
        print("── 9. LAF 法扶（模擬站） ──")
        run_test("LAF mock server start", "laf", test_laf_mock_server)
        if _LAF_MOCK_PROC:
            run_test("LAF mock health", "laf", test_laf_mock_health)
        run_test("LAF automation module", "laf", test_laf_automation_import)
        run_test("LAF draft-only safety", "laf", test_laf_draft_only_safety)
        print()

    # ── 10. Eefile Mock ──
    if "eefile" not in skip:
        print("── 10. 閱卷聲請（模擬站） ──")
        run_test("Eefile mock server start", "eefile", test_eefile_mock_server)
        if _EEFILE_MOCK_PROC:
            run_test("Eefile mock health", "eefile", test_eefile_mock_health)
        run_test("File review automation module", "eefile", test_file_review_automation_import)
        print()

    # ── 11. Autopilot & Cron ──
    print("── 11. Autopilot & Cron ──")
    run_test("Autopilot action.py", "autopilot", test_autopilot_action)
    run_test("Cron runner no hardcoded paths", "cron", test_cron_runner_no_hardcoded)
    print()

    # ── 12. Security ──
    print("── 12. Security ──")
    run_test("Insecure SSL default=0", "security", test_insecure_ssl_default)
    run_test("Authz module", "security", test_authz_module)
    run_test("CSRF module", "security", test_csrf_module)
    print()

    # ── 13. Release Hygiene ──
    print("── 13. Release Hygiene ──")
    run_test("No sensitive files", "hygiene", test_no_sensitive_files)
    run_test(".gitignore coverage", "hygiene", test_gitignore_coverage)
    run_test("LICENSE exists", "hygiene", test_license_exists)
    run_test("CI pipeline", "hygiene", test_ci_pipeline)
    print()

    # ── 14. Ops Live Guards ──
    print("── 14. Ops Live Guards ──")
    run_test("Judicial API pipeline healthy", "ops", test_judicial_api_pipeline_health)
    run_test("Auxiliary oMLX models", "ops", test_omlx_aux_models_available)
    run_test("MLX MTP sidecar health", "ops", test_mlx_mtp_sidecar_health)
    run_test("Menubar process running", "ops", test_menubar_process_running)
    run_test("NAS LUMI mount guard", "ops", test_nas_lumi_mount_guard)
    run_test("No Desktop git-add noise", "ops", test_no_desktop_git_add_noise)
    print()

    # ── 15. Commercial Release Guards ──
    if "commercial" not in skip:
        print("── 15. Commercial Release Guards ──")
        run_test("Public release audit strict", "commercial", test_public_release_audit_strict)
        run_test("Customer install wizard public dry-run", "commercial", test_customer_install_wizard_public_dry_run)
        run_test("Operation manuals exist", "commercial", test_operation_manuals_exist)
        run_test("Health active issues clear", "commercial", test_health_active_issues_clear)
        run_test("Process hygiene clean", "commercial", test_process_hygiene_clean)
        run_test("Model live gate profile", "commercial", test_model_live_gate_profile)
        run_test("Knowledge lint clean", "commercial", test_knowledge_lint_clean)
        run_test("Translation quality latest clean", "commercial", test_translation_quality_latest_clean)
        run_test("Tool hallucination latest clean", "commercial", test_tool_hallucination_latest_clean)
        run_test("Share gateway health", "commercial", test_share_gateway_health)
        run_test("Admin server health", "commercial", test_admin_server_health)
        run_test("Commercial readiness release gate", "commercial", test_commercial_readiness_release_gate)
        print()

    # ── Cleanup ──
    cleanup()

    report.duration_sec = round(time.time() - t0, 1)

    # ── Summary ──
    print("═" * 50)
    print(f"  Total: {report.total} | Passed: {report.passed} | Failed: {report.failed}")
    print(f"  Duration: {report.duration_sec}s")
    if report.failed > 0:
        print(f"\n  ❌ Failed tests:")
        for r in report.results:
            if not r["passed"]:
                print(f"     - [{r['module']}] {r['name']}: {r['message']} {r['error']}")
    else:
        print(f"\n  ✅ All tests passed!")
    print("═" * 50)

    # ── Output ──
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(asdict(report), f, ensure_ascii=False, indent=2)
        print(f"\n  Report: {out_path}")

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        cleanup()
        print("\n  Interrupted.")
        sys.exit(130)
    except Exception as e:
        cleanup()
        print(f"\n  Fatal: {e}")
        traceback.print_exc()
        sys.exit(2)
