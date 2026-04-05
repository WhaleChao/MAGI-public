"""MAGI Server — slim entry point.

This file was refactored from a 9,463-line monolith into a ~700-line
coordinator that wires together extracted modules:

  api/osc/           — OSC utility functions & judicial/draft helpers
  api/blueprints/osc_cases.py — OSC CRUD API routes (Flask Blueprint)
  api/webhooks/telegram.py    — Telegram webhook & messaging
  api/webhooks/line.py        — LINE messaging & attachment jobs
  api/startup.py              — Background threads & tunnel management
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import threading
import logging
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Ensure MAGI root is in sys.path (needed before importing skills.*)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from api.model_config import TEXT_PRIMARY_MODEL
from api.runtime_paths import ensure_path_on_sys_path, get_config_path, get_orch_dir
from api.product_runtime import PRODUCT_RUNTIME_PATH, product_profile_report, update_product_runtime
from api.case_path_mapper import (
    local_synology_path_candidates,
    preferred_case_roots,
    preferred_synology_share_roots,
    translate_local_path_to_canonical,
)

_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

# Auto-reap zombie children (cloudflared, skill subprocesses, etc.)
import signal as _signal


def _sigchld_handler(_signum, _frame):
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break


_signal.signal(_signal.SIGCHLD, _sigchld_handler)

# Load Env — always use explicit path to guarantee .env is found regardless of cwd
load_dotenv(os.path.join(_MAGI_ROOT, ".env"))

# Validate required config before anything else
from skills.ops.config import validate_config

validate_config()

from logging.handlers import RotatingFileHandler
from flask import (
    request, abort, render_template, redirect, url_for, flash,
    jsonify, Response, send_file, send_from_directory,
)
from api.line_compat import (
    AudioMessage, FileMessage, ImageMessage, ImageSendMessage,
    InvalidSignatureError, LINE_SDK_AVAILABLE, LineBotApiError,
    MessageEvent, TextMessage, TextSendMessage,
    build_line_clients, line_feature_enabled,
)
from api.app_factory import (
    create_base_app, init_login_manager, install_csrf,
    install_security_headers, register_core_blueprints,
)
from api.blueprints.web_runtime import create_web_runtime_blueprint
from api.blueprints.admin_runtime import create_admin_runtime_blueprint
from api.request_guards import install_request_guards

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_agent_dir_for_logs = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".agent"))
os.makedirs(_agent_dir_for_logs, exist_ok=True)
_server_log_path = os.path.join(_agent_dir_for_logs, "server.log")

from skills.ops.structured_log import JSONFormatter, HybridFormatter, RequestContextFilter

_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.addFilter(RequestContextFilter())
_SERVER_STARTUP_HOOKS_DISABLED = (
    os.environ.get("MAGI_DISABLE_SERVER_STARTUP_HOOKS", "").strip().lower()
    in {"1", "true", "yes", "on"}
)

_file = RotatingFileHandler(
    _server_log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_file.setFormatter(JSONFormatter())
_root.addHandler(_file)

if not _SERVER_STARTUP_HOOKS_DISABLED:
    _console = logging.StreamHandler()
    _console.setFormatter(HybridFormatter())
    _root.addHandler(_console)

logger = logging.getLogger("Server")

from api.thread_pools import channel_pool as _CHANNEL_BG_EXECUTOR, io_pool as _ATTACHMENT_BG_EXECUTOR

_CHANNEL_DELIVERY_AUDIT_FILE = os.path.join(_agent_dir_for_logs, "channel_delivery_audit.jsonl")
_channel_audit_lock = threading.Lock()

# Stability-first default: avoid distributed inference unless explicitly enabled.
os.environ.setdefault("MAGI_AVOID_DISTRIBUTED", "1")

from api.orchestrator import Orchestrator

try:
    from api.tw_output_guard import normalize_output_text as _normalize_output_text
except Exception:
    _normalize_output_text = None

# Auth Modules
import mysql.connector
from api.mysql_connector_guard import patch_mysql_connector_for_stability
from flask_login import UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# DB connector stability guard
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
if patch_mysql_connector_for_stability():
    logger.info(
        "MySQL connector guard enabled (MAGI_MYSQL_USE_PURE=%s)",
        os.environ.get("MAGI_MYSQL_USE_PURE", "1"),
    )

# ---------------------------------------------------------------------------
# App Creation
# ---------------------------------------------------------------------------
_SERVER_START_TIME = time.time()
app = create_base_app()
install_security_headers(app)
install_csrf(app, logger=logger)
install_request_guards(app, logger=logger)
register_core_blueprints(app)
login_manager = init_login_manager(app)

# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------
_rate_limit_store: dict = {}
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = {"webhook": 120, "api": 60}


def _check_rate_limit(category: str = "webhook") -> bool:
    """Return True if request should be rejected (rate exceeded)."""
    ip = request.remote_addr or "unknown"
    key = f"{category}:{ip}"
    now = time.time()
    limit = _RATE_LIMIT_MAX.get(category, 60)
    entry = _rate_limit_store.get(key)
    if entry:
        count, window_start = entry
        if now - window_start < _RATE_LIMIT_WINDOW:
            if count >= limit:
                return True
            _rate_limit_store[key] = (count + 1, window_start)
        else:
            _rate_limit_store[key] = (1, now)
    else:
        _rate_limit_store[key] = (1, now)
    if len(_rate_limit_store) > 500:
        cutoff = now - _RATE_LIMIT_WINDOW * 2
        stale = [k for k, (_, ws) in _rate_limit_store.items() if ws < cutoff]
        for k in stale:
            _rate_limit_store.pop(k, None)
    return False


# ---------------------------------------------------------------------------
# Database & Runtime Config
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "user": os.environ.get("DB_USER", "casper_service"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": "magi_brain",
}
if not DB_CONFIG["password"]:
    logger.error("Missing required env var: DB_PASSWORD. Set it in .env")


def _load_runtime_config():
    cfg = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.json"))
    try:
        if os.path.exists(cfg):
            with open(cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        logger.warning("Failed to load runtime config from %s", cfg)
    return {}


RUNTIME_CONFIG = _load_runtime_config()
SKILLS_ROOT = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills")))
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NERV_PRODUCT_NAMES = ("file_review", "transcript", "laf")
AGENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".agent"))
os.makedirs(AGENT_DIR, exist_ok=True)

# Export/static dirs
EXPORTS_DIR = os.path.join(_MAGI_ROOT, "exports")
os.makedirs(EXPORTS_DIR, exist_ok=True)
EXPORT_LONG_TEXT = os.environ.get("MAGI_EXPORT_LONG_TEXT", "1").strip().lower() in {"1", "true", "yes", "on"}
EXPORT_TEXT_THRESHOLD = int(os.environ.get("MAGI_EXPORT_TEXT_THRESHOLD", "1800") or "1800")

# ---------------------------------------------------------------------------
# Skill helpers
# ---------------------------------------------------------------------------


def _skill_doc_path(skill_name: str) -> Path:
    name = str(skill_name or "").strip()
    if not SKILL_NAME_RE.fullmatch(name):
        raise ValueError("invalid_skill_name")
    path = (SKILLS_ROOT / name / "SKILL.md").resolve()
    if SKILLS_ROOT.resolve() not in path.parents:
        raise ValueError("invalid_skill_path")
    return path


def _skill_action_path(skill_name: str) -> Path:
    name = str(skill_name or "").strip()
    if not SKILL_NAME_RE.fullmatch(name):
        raise ValueError("invalid_skill_name")
    path = (SKILLS_ROOT / name / "action.py").resolve()
    if SKILLS_ROOT.resolve() not in path.parents:
        raise ValueError("invalid_skill_path")
    return path


def _skill_summary(content: str) -> str:
    for line in str(content or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return ""


def _nerv_product_runtime_payload() -> dict:
    reports = {name: product_profile_report(name) for name in NERV_PRODUCT_NAMES}
    return {
        "ok": True,
        "runtime_path": str(PRODUCT_RUNTIME_PATH),
        "can_edit": bool(getattr(current_user, "is_admin", False)),
        "products": reports,
    }


def _list_skill_docs() -> list[dict]:
    items: list[dict] = []
    root = SKILLS_ROOT.resolve()
    if not root.exists():
        return items
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_doc = child / "SKILL.md"
        action_file = child / "action.py"
        if not skill_doc.exists() and not action_file.exists():
            continue
        content = ""
        if skill_doc.exists():
            try:
                content = skill_doc.read_text(encoding="utf-8")
            except Exception:
                content = ""
        stat_target = skill_doc if skill_doc.exists() else action_file
        updated_at = ""
        try:
            updated_at = datetime.fromtimestamp(stat_target.stat().st_mtime).isoformat()
        except Exception:
            pass
        items.append({
            "name": child.name,
            "path": str(child),
            "skill_doc_path": str(skill_doc),
            "has_skill_doc": skill_doc.exists(),
            "has_action": action_file.exists(),
            "summary": _skill_summary(content),
            "updated_at": updated_at,
        })
    return items


def _nerv_skill_interview_user_id() -> str:
    return f"nerv:{getattr(current_user, 'id', '') or 'unknown'}"


def _extract_interview_skill_name(message: str) -> str:
    match = re.search(r"資料夾：`([^`]+)`", str(message or ""))
    if match:
        return str(match.group(1) or "").strip()
    match = re.search(r"資料夾：([A-Za-z0-9._-]+)", str(message or ""))
    if match:
        return str(match.group(1) or "").strip()
    return ""


def _json_auth_error(status_code: int, error: str):
    return jsonify({"ok": False, "error": error}), status_code


def _require_json_auth(admin: bool = False):
    if not getattr(current_user, "is_authenticated", False):
        return _json_auth_error(401, "auth_required")
    if admin and not current_user.is_admin():
        return _json_auth_error(403, "admin_required")
    return None


# ---------------------------------------------------------------------------
# User Model
# ---------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role

    def is_admin(self):
        return self.role == "admin"


@login_manager.user_loader
def load_user(user_id):
    try:
        from api.db_helper import get_cursor
        with get_cursor(config=DB_CONFIG, dictionary=True) as (_conn, cursor):
            cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            user_data = cursor.fetchone()
            if user_data:
                return User(user_data["id"], user_data["username"], user_data["role"])
        return None
    except Exception as e:
        logger.error("DB Error: %s", e)
        return None


# ---------------------------------------------------------------------------
# LINE SDK Init
# ---------------------------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = (
    os.environ.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN")
    or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    or ""
).strip()
LINE_CHANNEL_SECRET = (
    os.environ.get("MAGI_LINE_CHANNEL_SECRET")
    or os.environ.get("LINE_CHANNEL_SECRET")
    or ""
).strip()
line_bot_api, handler, LINE_BOT_ENABLED, _line_bot_reason = build_line_clients(
    LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET,
)
if not LINE_BOT_ENABLED:
    if not line_feature_enabled():
        logger.info("LINE webhook disabled by MAGI_ENABLE_LINE.")
    else:
        logger.warning("LINE webhook disabled: %s", _line_bot_reason)

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
orchestrator = Orchestrator()

# Web Notifications Buffer
WEB_NOTIFICATIONS = defaultdict(list)

# ---------------------------------------------------------------------------
# Blueprint Registration
# ---------------------------------------------------------------------------

# Web runtime (dashboard, NERV)
app.register_blueprint(
    create_web_runtime_blueprint(
        orchestrator=orchestrator,
        logger=logger,
        web_notifications=WEB_NOTIFICATIONS,
        normalize_output_text=_normalize_output_text,
        magi_root=_MAGI_ROOT,
    )
)

# Iron Dome Sync
try:
    from skills.ops.iron_dome_sync import register_iron_dome_routes
    register_iron_dome_routes(app)
    logger.info("Iron Dome Sync routes registered")
except Exception as e:
    logger.warning("Iron Dome Sync routes not registered: %s", e)

# Auto-Skill
try:
    from skills.management.auto_skill import AutoSkill
    auto_skill = AutoSkill()
    logger.info("Auto-Skill Engine Online")
except Exception as e:
    logger.error("Auto-Skill Init Failed: %s", e)

# OSC Cases Blueprint
from api.blueprints.osc_cases import osc_bp
app.register_blueprint(osc_bp)

# Telegram Blueprint
from api.webhooks.telegram import telegram_bp
app.register_blueprint(telegram_bp)

# Admin Runtime Blueprint
def _cloudflared_alive():
    try:
        from api.startup import _is_cloudflared_alive
        return _is_cloudflared_alive()
    except Exception:
        return False

def _safe_remove_tmp(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

EXPECTED_MAGI_API_KEY = (os.environ.get("MAGI_API_KEY") or "").strip()

try:
    from api.webhooks.line import _read_attachment_job, _list_attachment_job_ids
except ImportError:
    _read_attachment_job = lambda *a, **k: None
    _list_attachment_job_ids = lambda: []

try:
    from skills.memory import job_queue as _jq
except Exception:
    _jq = None

app.register_blueprint(
    create_admin_runtime_blueprint(
        orchestrator=orchestrator,
        logger=logger,
        require_json_auth=_require_json_auth,
        list_skill_docs=_list_skill_docs,
        nerv_skill_interview_user_id=_nerv_skill_interview_user_id,
        extract_interview_skill_name=_extract_interview_skill_name,
        skill_doc_path=_skill_doc_path,
        skill_action_path=_skill_action_path,
        skill_summary=_skill_summary,
        nerv_product_runtime_payload=_nerv_product_runtime_payload,
        nerv_product_names=NERV_PRODUCT_NAMES,
        update_product_runtime=update_product_runtime,
        cloudflared_alive=_cloudflared_alive,
        server_start_time=_SERVER_START_TIME,
        attachment_job_queue=_jq,
        list_attachment_job_ids=_list_attachment_job_ids,
        read_attachment_job=_read_attachment_job,
        expected_magi_api_key=EXPECTED_MAGI_API_KEY,
        db_config=DB_CONFIG,
        mysql_connector=mysql.connector,
        safe_remove_tmp=_safe_remove_tmp,
        magi_root=_MAGI_ROOT,
    )
)

# ---------------------------------------------------------------------------
# Admin Allowlist
# ---------------------------------------------------------------------------
try:
    from api.admin_allowlist import get_line_admin_user_ids
except Exception:
    def get_line_admin_user_ids():  # type: ignore
        return {
            uid.strip()
            for uid in os.environ.get("MAGI_ADMIN_LINE_IDS", "").split(",")
            if uid.strip()
        }

ADMIN_LINE_USER_IDS = set(get_line_admin_user_ids() or set())
if not ADMIN_LINE_USER_IDS:
    logger.warning(
        "No LINE admin allowlist configured (MAGI_ADMIN_LINE_IDS). "
        "LINE users will default to non-admin."
    )

# ---------------------------------------------------------------------------
# Initialize LINE messaging module
# ---------------------------------------------------------------------------
try:
    from api.webhooks.line import init_line_module
    init_line_module(
        app=app,
        orch=orchestrator,
        bot_api=line_bot_api,
        hdlr=handler,
        admin_line_user_ids=ADMIN_LINE_USER_IDS,
        normalize_output_text=_normalize_output_text,
        channel_bg_executor=_CHANNEL_BG_EXECUTOR,
        attachment_bg_executor=_ATTACHMENT_BG_EXECUTOR,
    )
    logger.info("LINE messaging module initialized")
except Exception as e:
    logger.warning("LINE messaging module init failed: %s", e)


# ---------------------------------------------------------------------------
# Core Routes
# ---------------------------------------------------------------------------


@app.route("/osc")
@login_required
def osc_interface():
    return render_template("osc.html", user=current_user)


@app.route("/osc/debt")
@login_required
def osc_debt_interface():
    return render_template("osc_debt.html", user=current_user)


@app.route("/exports/<path:filename>")
@login_required
def serve_exports(filename):
    """Serve generated documents from the exports directory."""
    export_dir = os.path.join(_MAGI_ROOT, "exports")
    return send_from_directory(export_dir, filename)


# ---------------------------------------------------------------------------
# Tools API Proxy
# ---------------------------------------------------------------------------
_TOOLSAPI_COMPAT_ALLOW_PREFIX = ("api/audit_log", "health")


@app.route(
    "/toolsapi/<path:subpath>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def toolsapi_compat_proxy(subpath):
    """Backward-compatible proxy for legacy dashboard paths."""
    target = str(subpath or "").strip().lstrip("/")
    if not target:
        return jsonify({"success": False, "error": "missing target path"}), 404
    if not any(
        target == p or target.startswith(p + "/")
        for p in _TOOLSAPI_COMPAT_ALLOW_PREFIX
    ):
        return jsonify({"success": False, "error": "toolsapi path not allowed"}), 404

    from api.routing.service_registry import get_service_url
    try:
        base = get_service_url("tools_api")
    except KeyError:
        base = (os.environ.get("MAGI_TOOLS_API") or "http://127.0.0.1:5003").rstrip("/")
    qs = request.query_string.decode("utf-8", errors="ignore")
    url = f"{base}/{target}" + (f"?{qs}" if qs else "")

    fwd_headers = {}
    ctype = (request.headers.get("Content-Type") or "").strip()
    if ctype:
        fwd_headers["Content-Type"] = ctype

    data = request.get_data() if request.method in {"POST", "PUT", "PATCH", "DELETE"} else None
    req_obj = urllib.request.Request(url, data=data or None, headers=fwd_headers, method=request.method)
    try:
        with urllib.request.urlopen(req_obj, timeout=20) as resp:
            body = resp.read()
            status = int(getattr(resp, "status", 200))
            resp_ct = resp.headers.get("Content-Type", "application/json; charset=utf-8")
        return Response(body, status=status, content_type=resp_ct)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()
        except Exception:
            err_body = b""
        err_ct = (
            getattr(e, "headers", {}).get("Content-Type", "application/json; charset=utf-8")
            if getattr(e, "headers", None)
            else "application/json; charset=utf-8"
        )
        return Response(
            err_body or json.dumps({"success": False, "error": f"toolsapi_http_{getattr(e, 'code', 500)}"}).encode(),
            status=int(getattr(e, "code", 500)),
            content_type=err_ct,
        )
    except Exception as e:
        return jsonify({"success": False, "error": f"toolsapi_proxy_failed: {type(e).__name__}: {e}"}), 502


# ---------------------------------------------------------------------------
# 404 Fallback → Tools API
# ---------------------------------------------------------------------------
_TOOLS_API_FALLBACK_PATHS = {
    "health", "summarize", "search", "research", "fetch", "vision",
    "melchior", "skills", "collab", "council", "remember", "recall",
    "clients", "meetings", "legal", "alert", "definitions", "laf",
    "iron-dome", "code", "connections", "sages", "osc/external",
}


@app.errorhandler(404)
def _fallback_to_tools_api(error):
    """Forward unmatched routes to Tools API."""
    path = request.path.lstrip("/")
    first_seg = path.split("/")[0] if path else ""
    first_two = "/".join(path.split("/")[:2]) if "/" in path else ""
    if first_seg not in _TOOLS_API_FALLBACK_PATHS and first_two not in _TOOLS_API_FALLBACK_PATHS:
        return jsonify({"error": "not_found", "path": request.path}), 404

    from api.routing.service_registry import get_service_url
    try:
        base = get_service_url("tools_api")
    except KeyError:
        base = (os.environ.get("MAGI_TOOLS_API") or "http://127.0.0.1:5003").rstrip("/")
    qs = request.query_string.decode("utf-8", errors="ignore")
    url = f"{base}/{path}" + (f"?{qs}" if qs else "")

    fwd_headers = {}
    for hdr in ("Content-Type", "Authorization", "X-API-Key"):
        val = (request.headers.get(hdr) or "").strip()
        if val:
            fwd_headers[hdr] = val

    data = request.get_data() if request.method in {"POST", "PUT", "PATCH", "DELETE"} else None
    req_obj = urllib.request.Request(url, data=data or None, headers=fwd_headers, method=request.method)
    try:
        with urllib.request.urlopen(req_obj, timeout=30) as resp:
            body = resp.read()
            status = int(getattr(resp, "status", 200))
            resp_ct = resp.headers.get("Content-Type", "application/json; charset=utf-8")
        return Response(body, status=status, content_type=resp_ct)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()
        except Exception:
            err_body = b""
        err_ct = "application/json; charset=utf-8"
        if getattr(e, "headers", None):
            err_ct = e.headers.get("Content-Type", err_ct)
        return Response(
            err_body or json.dumps({"success": False, "error": f"tools_api_http_{getattr(e, 'code', 500)}"}).encode(),
            status=int(getattr(e, "code", 500)),
            content_type=err_ct,
        )
    except Exception as e:
        logger.warning("tools_api fallback proxy failed: %s", e)
        return jsonify({"success": False, "error": f"tools_api_unreachable: {type(e).__name__}"}), 502


# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        return callback()
    return redirect(url_for("dashboard_pages.dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        try:
            from api.db_helper import get_cursor
            with get_cursor(config=DB_CONFIG, dictionary=True) as (_conn, cursor):
                cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
                user_data = cursor.fetchone()
                if user_data and check_password_hash(user_data["password_hash"], password):
                    user = User(user_data["id"], user_data["username"], user_data["role"])
                    login_user(user)
                    return redirect(url_for("dashboard_pages.dashboard"))
                else:
                    flash("Invalid username or password")
        except Exception as e:
            flash(f"Login Error: {str(e)}")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        hashed_pw = generate_password_hash(password)
        try:
            from api.db_helper import get_cursor
            with get_cursor(config=DB_CONFIG) as (conn, cursor):
                cursor.execute("SELECT COUNT(*) FROM users")
                count = cursor.fetchone()[0]
                role = "admin" if count == 0 else "user"
                cursor.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
                    (username, hashed_pw, role),
                )
                conn.commit()
            flash(f"Registration successful! You are now an {role}. Please login.")
            return redirect(url_for("login"))
        except mysql.connector.Error as err:
            flash(f"Error: {err}")
    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# LINE Webhook
# ---------------------------------------------------------------------------


@app.route("/callback", methods=["GET", "POST"])
@app.route("/line/webhook", methods=["GET", "POST"])
def callback():
    if request.method == "GET":
        return "OK", 200
    if _check_rate_limit("webhook"):
        logger.warning("Rate limit exceeded for LINE webhook from %s", request.remote_addr)
        return "Too Many Requests", 429
    if not LINE_BOT_ENABLED:
        return "LINE webhook disabled: missing credentials", 503

    try:
        from api.startup import _record_last_public_base_url
        _record_last_public_base_url()
    except Exception:
        pass

    signature = request.headers.get("X-Line-Signature")
    if not signature:
        logger.error("Missing X-Line-Signature header.")
        abort(400)

    body = request.get_data(as_text=True)
    try:
        ua = (request.headers.get("User-Agent") or "").strip()
        xff = (request.headers.get("X-Forwarded-For") or "").strip()
        path = (request.path or "").strip()
        logger.info("LINE callback received (%d bytes) path=%r ua=%r xff=%r", len(body), path, ua, xff)
    except Exception:
        logger.info("LINE callback received (%d bytes).", len(body))

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. Check LINE_CHANNEL_SECRET / MAGI_LINE_CHANNEL_SECRET.")
        abort(400)

    try:
        from api.webhooks.line import _record_last_line_callback
        _record_last_line_callback(request.path or "/callback")
    except Exception:
        pass

    return "OK"


# ---------------------------------------------------------------------------
# Startup Hooks
# ---------------------------------------------------------------------------
if not _SERVER_STARTUP_HOOKS_DISABLED:
    try:
        from api.startup import run_startup_hooks
        run_startup_hooks(app, orchestrator)
        logger.info("Startup hooks completed")
    except Exception as e:
        logger.error("Startup hooks failed: %s", e)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False, threaded=True)
