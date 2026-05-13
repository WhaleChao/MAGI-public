#!/usr/bin/env python3
"""
judgment-collector -- 案由判決收集器
===================================
根據案件案由，自動收集最高法院判決（行政案件放寬到高等行政法院），
摘要後存入 DB、通知律師。

Usage (CLI):
    python action.py --task 'collect {"case_reason":"詐欺"}'
    python action.py --task 'daily_crawl'
    python action.py --task 'help'

Usage (Skill API via MAGI Tools):
    POST /skills/run  { "skill": "judgment-collector", "task": "collect {...}" }
"""
import argparse
import difflib
import hashlib
import json
import logging
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import re
import socket
import ssl
import sys
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib import request as _urlrequest
from urllib import error as _urlerror
from urllib import parse as _urlparse

# Add current directory to path for local imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from api.runtime_paths import get_orch_dir, get_skill_python
from api.case_path_mapper import preferred_case_roots, translate_case_path_to_local
from api.domains.judicial_api_backlog import build_backlog_interpretation, format_backlog_notice
from api.domains.judgment_value_filter import SKIP_SUMMARY, classify_judgment_record
from api.osc.insight_filters import is_non_extractable_legal_insight
try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None  # type: ignore
try:
    from api.tw_output_guard import normalize_output_text as _normalize_output_text
except Exception:
    _normalize_output_text = None
try:
    from skills.bridge.inference_gateway import InferenceGateway
except Exception:
    InferenceGateway = None  # type: ignore

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CODE_DIR = str(get_orch_dir())
CACHE_ROOT = os.path.expanduser("~/.cache/judgment_collector")
os.makedirs(CACHE_ROOT, exist_ok=True)
if load_dotenv is not None:
    try:
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)
    except Exception as _e:
        logging.getLogger("judgment-collector").debug("load_dotenv skipped: %s", _e)

# Cache run directory retention (days)
CACHE_RETENTION_DAYS = int(os.environ.get("JUDGMENT_CACHE_RETENTION_DAYS", "14"))


def _cleanup_old_cache_runs() -> int:
    """Remove cache run directories older than CACHE_RETENTION_DAYS. Returns count removed."""
    import shutil as _shutil
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=CACHE_RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y%m%d")
    removed = 0
    try:
        for name in os.listdir(CACHE_ROOT):
            full = os.path.join(CACHE_ROOT, name)
            if not os.path.isdir(full) or name == "judicial_api":
                continue
            date_part = name[:8]
            if date_part.isdigit() and date_part < cutoff_str:
                _shutil.rmtree(full, ignore_errors=True)
                removed += 1
    except Exception as _e:
        logging.getLogger("judgment-collector").warning("cache cleanup error: %s", _e)
    return removed


_JUDGMENTS_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "judgments.json")

_JUNK_KEYWORDS = (
    "系統降級回覆", "降級摘要", "摘要失敗", "逾時", "timeout",
    "模型忙碌",
)

_SUMMARY_REJECT_MARKERS = (
    "原始資料未提供全文文字",
    "已存原始 JSON",
    "請提供您需要我摘要的判決書全文",
    "請您提供需要我處理的判決書全文",
    "請您提供需要分析的判決書全文",
    "請您提供原始的判決書片段",
    "請您提供判決書全文",
    "請提供完整的判決書",
    "請將判決書貼於此",
    "請您將判決書貼於下方",
    "請您現在貼上判決書",
    "判決書貼於下方",
    "輸出內容：嚴格依照",
    "語言規範：全程使用",
    "而非創設新的法律見解",
    "而非闡述某個具有高度爭議性",
    "若需擷取量刑考量因素",
)

_SUMMARY_PROMPT_ECHO_MARKERS = (
    "請您現在貼上",
    "請將判決書貼",
    "判決書貼於下方",
    "我已理解",
    "我將會",
    "我將立即",
    "我將為您",
    "輸出內容",
    "語言規範",
    "AI 助理",
    "AI助理",
    "作為 MAGI",
    "作為MAGI",
    "MAGI 系統",
    "MAGI系統",
)

_SUMMARY_PROMPT_ECHO_CONTEXT = (
    "判決書",
    "實務見解",
    "引用裁判",
    "適用法條",
    "逐字擷取",
    "嚴格依照",
    "輸出格式",
)


def _summary_is_prompt_echo(text: str) -> bool:
    s = re.sub(r"\s+", "", str(text or ""))
    if not s:
        return True
    if any(re.sub(r"\s+", "", marker) in s for marker in _SUMMARY_REJECT_MARKERS):
        return True
    has_echo = any(re.sub(r"\s+", "", marker) in s for marker in _SUMMARY_PROMPT_ECHO_MARKERS)
    has_context = any(re.sub(r"\s+", "", marker) in s for marker in _SUMMARY_PROMPT_ECHO_CONTEXT)
    return bool(has_echo and has_context)


def _summary_is_bad_storage_value(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    return _summary_is_prompt_echo(s) or is_non_extractable_legal_insight(s)


def _safe_summary_for_storage(summary: str, *, is_degraded: bool = False) -> tuple[str, bool]:
    s = str(summary or "").strip()
    bad = bool(is_degraded or _summary_is_bad_storage_value(s))
    return ("" if bad else s, bad)


def _upsert_judgments_json(
    title: str,
    summary: str,
    case_reason: str,
    *,
    url: str = "",
    source: str = "Judicial Yuan",
    max_entries: int = 2000,
) -> bool:
    """Append a quality-checked LLM summary to judgments.json (dedup by title)."""
    if not summary or len(summary) < 30:
        return False
    if any(kw in summary for kw in _JUNK_KEYWORDS) or _summary_is_prompt_echo(summary):
        return False
    try:
        existing = []
        if os.path.exists(_JUDGMENTS_JSON_PATH):
            with open(_JUDGMENTS_JSON_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        # Self-healing: remove junk on every save
        existing = [
            d for d in existing
            if not any(kw in str(d.get("summary", "")) for kw in _JUNK_KEYWORDS)
            and not _summary_is_prompt_echo(str(d.get("summary", "")))
            and d.get("summary_type") != "preview"
        ]
        # Dedup by normalized title
        _norm = lambda s: re.sub(r"\s+", "", s).replace("臺", "台").replace("　", "")
        title_norm = _norm(title)
        if any(_norm(d.get("title", "")) == title_norm for d in existing):
            return False
        existing.insert(0, {
            "title": title,
            "url": url,
            "summary": summary,
            "summary_type": "llm",
            "case_reason": case_reason,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
        })
        existing = existing[:max_entries]
        with open(_JUDGMENTS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logging.getLogger("judgment-collector").warning("_upsert_judgments_json failed: %s", e)
        return False


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# ── WFGY 增強推理（判決摘要用） ──────────────────────────────────
# 預設啟用；設定 MAGI_JUDGMENT_WFGY=0 可關閉。
_WFGY_ENABLED: Optional[bool] = None  # lazy-init


def _wfgy_enabled() -> bool:
    global _WFGY_ENABLED
    if _WFGY_ENABLED is None:
        _WFGY_ENABLED = _env("MAGI_JUDGMENT_WFGY", "1") in ("1", "true", "yes", "on")
        if _WFGY_ENABLED:
            try:
                from skills.reasoning.wfgy import apply_wfgy_logic  # noqa: F401
                logger.info("⚡ WFGY reasoning enabled for judgment summarization")
            except ImportError:
                logger.warning("⚠️ WFGY module not available — judgment summaries will use standard prompt")
                _WFGY_ENABLED = False
    return _WFGY_ENABLED


def _apply_wfgy(prompt: str) -> str:
    """Wrap prompt with WFGY 7-step reasoning chain if enabled."""
    if not _wfgy_enabled():
        return prompt
    try:
        from skills.reasoning.wfgy import apply_wfgy_logic
        return apply_wfgy_logic(prompt)
    except Exception:
        return prompt


def _get_db_config() -> dict:
    """
    以 Casper 本機 DB 為預設（主 DB 關機時仍可運作）。
    允許用環境變數覆蓋：
    - JUDGMENT_DB_HOST/PORT/USER/PASSWORD/NAME
    - 若未提供，回退 OSC_DB_*（與其他 headless 模組一致）
    """
    host = _env("JUDGMENT_DB_HOST")
    port = _env("JUDGMENT_DB_PORT")
    user = _env("JUDGMENT_DB_USER")
    password = _env("JUDGMENT_DB_PASSWORD")
    name = _env("JUDGMENT_DB_NAME")
    if host and user and name:
        return {
            "host": host,
            "port": int(port or "3306"),
            "user": user,
            "password": password,
            "database": name,
        }

    def _is_reachable(host: str, port: int) -> bool:
        try:
            with socket.create_connection((str(host or "").strip(), int(port)), timeout=1.2):
                return True
        except Exception:
            return False

    def _from_config_json(prefer_local: bool) -> dict:
        # Prefer code/json/config.json -> mariadb_profiles
        try:
            cfg_path = os.path.join(CODE_DIR, "json", "config.json")
            if not os.path.exists(cfg_path):
                cfg_path = os.path.join(CODE_DIR, "config.json")
            if not os.path.exists(cfg_path):
                return {}
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            profiles = cfg.get("mariadb_profiles") or []
            want = (
                ["Home_Local_Test", "Studio_Local", "Studio_VPN_Remote"]
                if prefer_local
                else ["Studio_VPN_Remote", "Studio_Local", "Home_Local_Test"]
            )
            reachable_fallback: Optional[dict] = None
            for name in want:
                for p in profiles:
                    if (p.get("profile_name") or "").strip() != name:
                        continue
                    c = (p.get("config") or {})
                    if c.get("host") and c.get("user") and c.get("database"):
                        candidate = {
                            "host": c.get("host"),
                            "port": int(c.get("port") or 3306),
                            "user": c.get("user") or os.environ.get("OSC_DB_USER", "python_user"),
                            "password": c.get("password") or os.environ.get("OSC_DB_PASSWORD", ""),
                            "database": c.get("database"),
                        }
                        if _is_reachable(str(candidate["host"]), int(candidate["port"])):
                            return candidate
                        if reachable_fallback is None:
                            reachable_fallback = candidate
            if reachable_fallback:
                return reachable_fallback
        except Exception:
            return {}
        return {}

    # ── OSC_DB_* 環境變數（優先於 config.json，避免本機 DB 搶先） ──
    osc_host = _env("OSC_DB_HOST")
    osc_user = _env("OSC_DB_USER")
    osc_db = _env("OSC_DB_NAME", "law_firm_data")
    if osc_host and osc_user:
        return {
            "host": osc_host,
            "port": int(_env("OSC_DB_PORT", "3306") or "3306"),
            "user": osc_user,
            "password": _env("OSC_DB_PASSWORD"),
            "database": osc_db,
        }

    try:
        # Reuse osc_headless helper if available.
        _osc_paths = [CODE_DIR, os.path.join(_MAGI_ROOT, "skills", "osc-orchestrator")]
        for _p in _osc_paths:
            if _p not in sys.path:
                sys.path.insert(0, _p)
        from osc_headless.db import db_config_from_env

        c = db_config_from_env(prefix="OSC_DB_")
        if getattr(c, "host", None) and getattr(c, "user", None) and getattr(c, "database", None):
            return {
                "host": c.host,
                "port": int(c.port),
                "user": c.user,
                "password": c.password or "",
                "database": c.database,
            }
    except Exception as _e:
        logging.getLogger("judgment-collector").warning("DB config via osc_headless failed: %s", _e)

    # Prefer local DB when requested (Keeper/主 DB 關機時，避免卡在遠端連線)。
    prefer_local = _env("MAGI_PREFER_LOCAL_DB", "0").lower() in {"1", "true", "yes", "on"}

    # Final fallback: config.json profiles, otherwise empty password.
    c2 = _from_config_json(prefer_local=prefer_local)
    if c2:
        return c2
    return {
        "host": "127.0.0.1",
        "port": 3307,
        "user": "python_user",
        "password": "",
        "database": "law_firm_data",
    }

DEFAULT_MAX_RESULTS = int(_env("JUDGMENT_DEFAULT_MAX_RESULTS", "120") or "120")
DEFAULT_MAX_CHARS = int(_env("JUDGMENT_DEFAULT_MAX_CHARS", "300000") or "300000")
DEFAULT_TIMEOUT_SEC = 300
JUDGMENT_JY_FILL_MAX_RESULTS = int(_env("JUDGMENT_JY_FILL_MAX_RESULTS", "5000") or "5000")
JUDGMENT_DAILY_SCAN_FALLBACK_LIMIT = int(_env("JUDGMENT_DAILY_SCAN_FALLBACK_LIMIT", "8000") or "8000")
SUMMARY_RETRY_QUEUE_PATH = os.path.expanduser(
    _env("JUDGMENT_SUMMARY_RETRY_QUEUE_PATH", os.path.join(CACHE_ROOT, "summary_retry_queue.jsonl"))
)
SUMMARY_RETRY_MAX_ATTEMPTS = int(_env("JUDGMENT_SUMMARY_RETRY_MAX_ATTEMPTS", "4") or "4")
SUMMARY_RETRY_DB_UPDATE = (_env("JUDGMENT_SUMMARY_RETRY_UPDATE_DB", "1") or "1").lower() in {"1", "true", "yes", "on"}

SYNOLOGY_CASE_ROOTS = preferred_case_roots(include_closed=False)

# Judicial Data API（司法院裁判書開放 API）
JDG_API_BASE = _env("JUDICIAL_API_BASE", "https://data.judicial.gov.tw/jdg/api").rstrip("/")
JDG_API_WINDOW_START_HOUR = int(_env("JUDICIAL_API_WINDOW_START_HOUR", "0") or "0")
JDG_API_WINDOW_END_HOUR = int(_env("JUDICIAL_API_WINDOW_END_HOUR", "6") or "6")
JDG_API_NIGHT_MAX_JDOCS = int(_env("JUDICIAL_API_NIGHT_MAX_JDOCS", "25000") or "25000")
JDG_API_DAY_MAX_PROCESS = int(_env("JUDICIAL_API_DAY_MAX_PROCESS", "200") or "200")
JDG_API_DAY_SUMMARY_MAX = int(_env("JUDICIAL_API_DAY_SUMMARY_MAX", "80") or "80")
JDG_API_DAY_SUMMARY_TIMEOUT_SEC = int(_env("JUDICIAL_API_DAY_SUMMARY_TIMEOUT_SEC", "240") or "240")
JDG_API_DAY_VECTOR_MAX_CHARS = int(_env("JUDICIAL_API_DAY_VECTOR_MAX_CHARS", "12000") or "12000")
JDG_API_DAY_SUMMARY_MODE = _env("JUDICIAL_API_DAY_SUMMARY_MODE", "llm").lower() or "llm"
JDG_API_DAY_SKIP_ASSETS = _env("JUDICIAL_API_DAY_SKIP_ASSETS", "0").lower() in {"1", "true", "yes", "on"}
JDG_API_FAST_BACKLOG_THRESHOLD = int(_env("JUDICIAL_API_FAST_BACKLOG_THRESHOLD", "5000") or "5000")
JDG_API_ROOT = os.path.join(CACHE_ROOT, "judicial_api")
JDG_API_RAW_ROOT = os.path.join(JDG_API_ROOT, "raw")
JDG_API_NORMALIZED_ROOT = os.path.join(JDG_API_ROOT, "normalized")
JDG_API_PULL_STATE_PATH = os.path.join(JDG_API_ROOT, "pull_state.json")
JDG_API_PROCESS_STATE_PATH = os.path.join(JDG_API_ROOT, "process_state.json")
os.makedirs(JDG_API_RAW_ROOT, exist_ok=True)
os.makedirs(JDG_API_NORMALIZED_ROOT, exist_ok=True)

ADMIN_KEYWORDS = ["行政", "訴願", "行政訴訟", "稅捐", "環保", "都市計畫"]
CLOSED_KEYWORDS = ["結案", "歸檔", "封存"]


def _is_unlimited(n: int) -> bool:
    try:
        return int(n) <= 0
    except Exception:
        return False


def _is_offpeak_now() -> bool:
    """
    以本地時區判斷是否處於低峰時段。
    預設：22:00-06:59（可用 JUDGMENT_SUMMARY_RETRY_OFFPEAK_WINDOWS 覆寫）
    格式：`22-23,0-6` 或 `0-5,23-23`
    """
    now_h = datetime.now().hour
    raw = (_env("JUDGMENT_SUMMARY_RETRY_OFFPEAK_WINDOWS", "22-23,0-6") or "").strip()
    if not raw:
        return now_h >= 22 or now_h <= 6
    for part in raw.split(","):
        seg = (part or "").strip()
        if not seg:
            continue
        if "-" not in seg:
            try:
                if now_h == int(seg):
                    return True
            except Exception:
                continue
            continue
        a, b = seg.split("-", 1)
        try:
            s = int(a.strip())
            e = int(b.strip())
        except Exception:
            continue
        if s <= e:
            if s <= now_h <= e:
                return True
        else:
            # wrap-around (e.g. 23-4)
            if now_h >= s or now_h <= e:
                return True
    return False


def _next_offpeak_epoch(now_epoch: Optional[float] = None) -> float:
    """
    計算下一個離峰時段開始時間（fallback: 2 小時後）。
    """
    now_epoch = float(now_epoch or time.time())
    now_dt = datetime.fromtimestamp(now_epoch)
    raw = (_env("JUDGMENT_SUMMARY_RETRY_OFFPEAK_WINDOWS", "22-23,0-6") or "").strip()
    starts: list[int] = []
    for part in raw.split(","):
        seg = (part or "").strip()
        if not seg:
            continue
        if "-" in seg:
            a, _b = seg.split("-", 1)
            try:
                starts.append(int(a.strip()) % 24)
            except Exception:
                continue
        else:
            try:
                starts.append(int(seg.strip()) % 24)
            except Exception:
                continue
    if not starts:
        return now_epoch + 7200.0
    starts = sorted(set(starts))
    for h in starts:
        cand = now_dt.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand.timestamp() > now_epoch:
            return cand.timestamp()
    # next day first off-peak start
    tomorrow = now_dt.timestamp() + 86400.0
    tdt = datetime.fromtimestamp(tomorrow)
    cand = tdt.replace(hour=starts[0], minute=0, second=0, microsecond=0)
    return cand.timestamp()


def _retry_tier(attempts: int) -> str:
    a = int(max(0, attempts))
    if a <= 0:
        return "fast"
    if a <= 2:
        return "standard"
    return "deep"


def _timeout_for_tier(tier: str, *, offpeak: bool) -> int:
    t_fast = int(_env("JUDGMENT_SUMMARY_RETRY_TIMEOUT_FAST_SEC", "180") or "180")
    t_std = int(_env("JUDGMENT_SUMMARY_RETRY_TIMEOUT_STD_SEC", "360") or "360")
    t_deep = int(_env("JUDGMENT_SUMMARY_RETRY_TIMEOUT_DEEP_SEC", "540") or "540")
    base = t_fast if tier == "fast" else (t_std if tier == "standard" else t_deep)
    if offpeak:
        boost = int(_env("JUDGMENT_SUMMARY_RETRY_OFFPEAK_TIMEOUT_BOOST_SEC", "90") or "90")
        base = base + max(0, boost)
    return max(90, base)

logger = logging.getLogger("judgment-collector")
_INFERENCE_GATEWAY = None
_LAST_SUMMARY_META = {
    "is_degraded": True,
    "route": "init",
    "error": "",
}


def _get_inference_gateway():
    global _INFERENCE_GATEWAY
    if _INFERENCE_GATEWAY is None and InferenceGateway is not None:
        try:
            _INFERENCE_GATEWAY = InferenceGateway()
        except Exception:
            _INFERENCE_GATEWAY = None
    return _INFERENCE_GATEWAY


def _set_last_summary_meta(*, is_degraded: bool, route: str, error: str = "") -> None:
    _LAST_SUMMARY_META["is_degraded"] = bool(is_degraded)
    _LAST_SUMMARY_META["route"] = str(route or "").strip() or "unknown"
    _LAST_SUMMARY_META["error"] = str(error or "").strip()


def _get_last_summary_meta() -> dict:
    return dict(_LAST_SUMMARY_META)


def _tw(text: str) -> str:
    s = str(text or "")
    if not s:
        return s
    try:
        if _normalize_output_text:
            return _normalize_output_text(s, platform="JUDGMENT")
    except Exception as _e:
        logging.getLogger("judgment-collector").debug("_tw normalize skipped: %s", _e)
    return s
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    """
    Best-effort：將判決爬取/跳過原因寫入向量記憶，便於日後追溯。
    """
    try:
        if CODE_DIR not in sys.path:
            sys.path.insert(0, CODE_DIR)
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags=tags or {}, source="judgment_collector")
    except Exception:
        return


# ---------------------------------------------------------------------------
# Judicial API Helpers
# ---------------------------------------------------------------------------
def _load_json_file(path: str, default: Any) -> Any:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as _e:
        logger.warning("_load_json_file(%s) failed: %s", path, _e)
    return default


def _save_json_file(path: str, obj: Any) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _load_code_config() -> dict:
    cfg: dict = {}
    for p in (
        os.path.join(CODE_DIR, "json", "config.json"),
        os.path.join(CODE_DIR, "config.json"),
    ):
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    obj = json.load(f) or {}
                if isinstance(obj, dict):
                    cfg = obj
                    break
        except Exception:
            continue

    # OpenClaw workspace may hold dedicated official Judicial API credentials
    # even when MAGI's main config only contains ezlawyer record credentials.
    ai_cfg_path = (
        os.environ.get("OPENCLAW_AI_CONFIG_PATH")
        or os.path.expanduser("~/.openclaw/workspace/ai_config.json")
    )
    try:
        if os.path.exists(ai_cfg_path):
            with open(ai_cfg_path, "r", encoding="utf-8") as f:
                ai_cfg = json.load(f) or {}
            if isinstance(ai_cfg, dict):
                for key in ("judicial_api_user", "judicial_api_pass"):
                    if (not str(cfg.get(key) or "").strip()) and str(ai_cfg.get(key) or "").strip():
                        cfg[key] = str(ai_cfg.get(key) or "").strip()
    except Exception as _e:
        logger.debug("ai_cfg fallback skipped: %s", _e)
    return cfg


def _get_jdg_credentials() -> tuple[str, str, str]:
    """
    司法院官方 API 帳密來源（優先順序）：
    1) env: JUDICIAL_API_USER/JUDICIAL_API_PASSWORD
    2) env: JDG_API_USER/JDG_API_PASSWORD
    3) code/json/config.json -> judicial_api_user/judicial_api_pass
    4) 明示允許時才回退 judicial.record_username/record_password
    """
    user = _env("MAGI_JUDICIAL_API_USER") or _env("JUDICIAL_API_USER") or _env("JDG_API_USER")
    pwd = _env("MAGI_JUDICIAL_API_PASS") or _env("MAGI_JUDICIAL_API_PASSWORD") or _env("JUDICIAL_API_PASSWORD") or _env("JDG_API_PASSWORD")
    if user and pwd:
        return user, pwd, "env"

    cfg = _load_code_config()
    if isinstance(cfg, dict):
        user = str(cfg.get("judicial_api_user") or "").strip()
        pwd = str(cfg.get("judicial_api_pass") or "").strip()
        if user and pwd:
            return user, pwd, "config.judicial_api_*"

        judicial = cfg.get("judicial")
        if isinstance(judicial, dict):
            user = str(judicial.get("api_user") or "").strip()
            pwd = str(judicial.get("api_password") or "").strip()
            if user and pwd:
                return user, pwd, "config.judicial.api_*"

            allow_record_fallback = (_env("JUDICIAL_API_ALLOW_RECORD_FALLBACK", "0") or "0").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if allow_record_fallback:
                user = str(
                    _env("MAGI_JUDICIAL_RECORD_USERNAME") or judicial.get("record_username") or ""
                ).strip()
                pwd = str(
                    _env("MAGI_JUDICIAL_RECORD_PASSWORD") or judicial.get("record_password") or ""
                ).strip()
                if user and pwd:
                    return user, pwd, "env/config.judicial.record_*"
    return "", "", ""


def _is_jdg_service_window(dt: Optional[datetime] = None) -> bool:
    """
    依官方說明預設 00:00-06:00。end hour 採「不含」。
    """
    dt = dt or datetime.now()
    h = int(dt.hour)
    s = int(JDG_API_WINDOW_START_HOUR % 24)
    e = int(JDG_API_WINDOW_END_HOUR % 24)
    if s == e:
        return True
    if s < e:
        return s <= h < e
    return (h >= s) or (h < e)


# ★ SSL context for judicial API: Python 3.14 + OpenSSL 3.x enforces strict
#   X.509 checks (e.g. Subject Key Identifier).  The judicial.gov.tw cert chain
#   lacks SKI, so we build a verified-but-relaxed context using certifi CA bundle
#   and ~VERIFY_X509_STRICT.  Falls back to unverified only as last resort.
_jdg_ssl_ctx_cache: dict[str, Any] = {}  # {"ctx": ssl.SSLContext}


def _build_jdg_ssl_context() -> ssl.SSLContext:
    """Build a verified SSL context that tolerates missing SKI extension."""
    cached = _jdg_ssl_ctx_cache.get("ctx")
    if cached is not None:
        return cached
    try:
        import certifi
        ca_bundle = certifi.where()
    except ImportError:
        ca_bundle = None
    ctx = ssl.create_default_context(cafile=ca_bundle)
    # Relax X.509 strict mode — keeps chain/hostname validation but skips SKI check.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    _jdg_ssl_ctx_cache["ctx"] = ctx
    return ctx


def _jdg_post_json(path: str, payload: dict, timeout_sec: int = 25) -> Any:
    url = JDG_API_BASE + "/" + path.lstrip("/")
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = _urlrequest.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    allow_insecure_fallback = (_env("JUDICIAL_API_ALLOW_INSECURE_SSL", "0") or "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    ctx = _build_jdg_ssl_context()
    try:
        with _urlrequest.urlopen(req, timeout=max(5, int(timeout_sec)), context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw or "{}")
    except _urlerror.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return {"error": f"HTTP {getattr(e, 'code', 'ERR')}", "body": body[:800]}
    except Exception as e:
        msg = str(e)
        cert_err = ("CERTIFICATE_VERIFY_FAILED" in msg) or ("certificate verify failed" in msg.lower())
        if cert_err and allow_insecure_fallback:
            logger.warning("[AUDIT] SSL relaxed context 仍失敗，降級為不驗證模式（url=%s）", url[:120])
            try:
                unverified = ssl._create_unverified_context()
                _jdg_ssl_ctx_cache["ctx"] = unverified
                with _urlrequest.urlopen(req, timeout=max(5, int(timeout_sec)), context=unverified) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                obj = json.loads(raw or "{}")
                if isinstance(obj, dict):
                    obj.setdefault("_ssl_insecure_fallback", True)
                return obj
            except Exception as e2:
                return {"error": str(e2)[:240], "ssl_insecure_fallback": True}
        return {"error": msg[:240]}


def _jdg_download_file(url: str, dest_path: str, timeout_sec: int = 30) -> dict:
    src = str(url or "").strip()
    if not src:
        return {"ok": False, "error": "empty_url"}
    req = _urlrequest.Request(
        src,
        headers={"User-Agent": "MAGI/1.0"},
        method="GET",
    )
    ctx = _build_jdg_ssl_context()
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with _urlrequest.urlopen(req, timeout=max(5, int(timeout_sec)), context=ctx) as resp:
            data = resp.read()
        tmp = dest_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest_path)
        return {"ok": True, "path": dest_path, "bytes": len(data)}
    except _urlerror.HTTPError as e:
        return {"ok": False, "error": f"HTTP {getattr(e, 'code', 'ERR')}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:240]}


def _jid_slug(jid: str) -> str:
    s = str(jid or "").strip()
    if not s:
        return "jid_empty"
    head = hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:12]
    tail = re.sub(r"[^0-9A-Za-z_\-]+", "_", s)[:64].strip("_")
    if not tail:
        tail = "jid"
    return f"{head}_{tail}"


def _sanitize_filename(name: str, default: str = "file") -> str:
    s = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]+', "_", str(name or "").strip())
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return (s[:180] if s else default)


def _download_jdg_assets(*, jid: str, fields: dict, target_dir: str) -> dict:
    slug = _jid_slug(jid)
    out = {
        "pdf_path": "",
        "attachments_dir": "",
        "attachments": [],
        "downloaded": 0,
        "failed": [],
    }
    pdf_url = str(fields.get("full_pdf") or "").strip()
    if pdf_url:
        pdf_path = os.path.join(target_dir, slug + ".pdf")
        resp = _jdg_download_file(pdf_url, pdf_path, timeout_sec=45)
        if resp.get("ok"):
            out["pdf_path"] = pdf_path
            out["downloaded"] += 1
        else:
            out["failed"].append({"kind": "pdf", "url": pdf_url, "error": resp.get("error")})

    attachments = fields.get("attachments") or []
    if isinstance(attachments, list) and attachments:
        attach_dir = os.path.join(target_dir, slug + "_attachments")
        try:
            import shutil as _shutil
            if os.path.isdir(attach_dir):
                _shutil.rmtree(attach_dir, ignore_errors=True)
            os.makedirs(attach_dir, exist_ok=True)
        except Exception:
            os.makedirs(attach_dir, exist_ok=True)
        out["attachments_dir"] = attach_dir
        for idx, item in enumerate(attachments, start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("TITLE") or item.get("title") or "").strip()
            url = str(item.get("URL") or item.get("url") or "").strip()
            if not url:
                continue
            fallback_name = os.path.basename(_urlparse.urlparse(url).path) or f"attachment_{idx}"
            fname = _sanitize_filename(title or fallback_name, default=f"attachment_{idx}")
            dest = os.path.join(attach_dir, fname)
            resp = _jdg_download_file(url, dest, timeout_sec=45)
            if resp.get("ok"):
                out["attachments"].append({"title": title or fname, "path": dest, "url": url})
                out["downloaded"] += 1
            else:
                out["failed"].append({"kind": "attachment", "title": title or fname, "url": url, "error": resp.get("error")})
    return out


def _extractive_judgment_summary(full_text: str, case_reason: str = "", *, max_chars: int = 1400) -> str:
    """Fast, source-bound digest for backlog catch-up.  Main sections quote court text only."""
    text = re.sub(r"\r\n?", "\n", str(full_text or "")).strip()
    if len(text) < 120:
        return ""
    compact = re.sub(r"[ \t]+", " ", text)

    def _section(start_pat: str, end_pat: str = "", limit: int = 700) -> str:
        start = re.search(start_pat, compact)
        if not start:
            return ""
        chunk = compact[start.end():]
        if end_pat:
            end = re.search(end_pat, chunk)
            if end:
                chunk = chunk[: end.start()]
        chunk = re.sub(r"\n{2,}", "\n", chunk).strip(" ：:\n\t")
        return chunk[:limit].strip()

    holding = _section(r"主\s*文", r"(?:事實及理由|事實|理由|中\s*華\s*民\s*國)", 420)
    reason = ""
    reason_text = _section(r"(?:事實及理由|理由)", r"中\s*華\s*民\s*國", 1200)
    if reason_text:
        signals = (
            r"(?:本院認為|本院認|本院審酌|經查|惟查|按[，,]|次按|又按|查|準此|是以)"
            r".{30,260}?(?:。|；)"
        )
        picks = [m.group(0).strip() for m in re.finditer(signals, reason_text)]
        if picks:
            dedup: list[str] = []
            seen: set[str] = set()
            for p in picks:
                key = re.sub(r"\s+", "", p)[:80]
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(p)
                if len(dedup) >= 3:
                    break
            reason = "\n".join(f"- {p}" for p in dedup)
        else:
            first = reason_text[:520].strip()
            reason = f"- {first}" if first else ""

    statutes = sorted(set(re.findall(r"(?:民法|刑法|民事訴訟法|刑事訴訟法|行政訴訟法|家事事件法|消費者債務清理條例|非訟事件法)?第\d+(?:-\d+)?條(?:之\d+)?", compact)))
    statutes_line = "、".join(statutes[:12]) if statutes else "未明確抽得"
    title_line = next((ln.strip() for ln in compact.split("\n") if ln.strip()), "")

    parts = [
        "## 摘要類型",
        "抽取式快篩（主文與理由均取自裁判原文；未經 LLM 改寫）",
        "",
        "## 主文摘錄",
        holding or (reason_text[:360].strip() if reason_text else compact[:360].strip()),
        "",
        "## 理由摘錄",
        reason or "- 未抽得明確理由段落；請開啟全文確認。",
        "",
        "## 適用法條",
        statutes_line,
    ]
    if case_reason or title_line:
        parts.extend(["", "## 來源", f"{case_reason or '裁判書'}｜{title_line[:120]}"])
    out = "\n".join(parts).strip()
    return out[:max_chars].strip()


def _remove_jdg_material_by_jid(conn, jid: str) -> dict:
    j = str(jid or "").strip()
    if not j:
        return {"court_judgments_deleted": 0, "judgment_archive_deleted": 0, "artifacts_deleted": 0}
    slug = _jid_slug(j)
    text_paths: list[str] = []
    archive_deleted = 0
    court_deleted = 0
    if conn:
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, full_text_path FROM judgment_archive "
                "WHERE source_jid=%s OR (search_query=%s AND full_text_path LIKE %s)",
                (j, "[JDG API]", f"%{slug}%"),
            )
            rows = cur.fetchall() or []
            cur.close()
            text_paths = [str((r or {}).get("full_text_path") or "").strip() for r in rows if str((r or {}).get("full_text_path") or "").strip()]
        except Exception:
            rows = []
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM court_judgments WHERE jid=%s", (j,))
            court_deleted = int(cur.rowcount or 0)
            try:
                cur.execute(
                    "DELETE FROM judgment_archive WHERE source_jid=%s OR (search_query=%s AND full_text_path LIKE %s)",
                    (j, "[JDG API]", f"%{slug}%"),
                )
                archive_deleted = int(cur.rowcount or 0)
            except Exception as del_e:
                logger.warning("remove_jdg_material_by_jid: judgment_archive DELETE 失敗（可能無權限）: %s", del_e)
            conn.commit()
            cur.close()
        except Exception as e:
            logger.warning("remove_jdg_material_by_jid db delete failed: %s", e)

    deleted_paths = 0
    targets: set[str] = set()
    for p in text_paths:
        targets.add(p)
        base_dir = os.path.dirname(p)
        targets.add(os.path.join(base_dir, slug + ".pdf"))
        targets.add(os.path.join(base_dir, slug + "_attachments"))
    try:
        for p in Path(JDG_API_NORMALIZED_ROOT).glob(f"*/{slug}*"):
            targets.add(str(p))
    except Exception as _e:
        logger.debug("glob normalized root skipped for slug=%s: %s", slug, _e)
    for target in sorted(targets):
        try:
            if os.path.isdir(target):
                import shutil as _shutil
                _shutil.rmtree(target, ignore_errors=True)
                deleted_paths += 1
            elif os.path.isfile(target):
                os.remove(target)
                deleted_paths += 1
        except Exception:
            continue
    return {
        "court_judgments_deleted": court_deleted,
        "judgment_archive_deleted": archive_deleted,
        "artifacts_deleted": deleted_paths,
    }


def _jdg_court_name_from_jid(jid: str) -> str:
    prefix = (str(jid or "").split(",")[0] if jid else "").strip().upper()
    m = {
        "TPSM": "最高法院",
        "TPHM": "臺灣高等法院",
        "TPHV": "臺灣高等法院",
        "TPHA": "最高行政法院",
        "TPHP": "懲戒法院",
        "TCDA": "臺中高等行政法院",
        "TPBA": "臺北高等行政法院",
        "KSHA": "高雄高等行政法院",
    }
    return m.get(prefix, prefix or "法院")


def _parse_jdate_iso(jdate: str) -> Optional[str]:
    s = re.sub(r"[^0-9]", "", str(jdate or ""))
    if len(s) != 8:
        return None
    try:
        y = int(s[0:4])
        m = int(s[4:6])
        d = int(s[6:8])
        if 1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return None
    return None


def _compose_title_from_jdoc(jdoc: dict, jid: str) -> str:
    if not isinstance(jdoc, dict):
        return str(jid or "").strip()
    court = _jdg_court_name_from_jid(jid)
    y = str(jdoc.get("JYEAR") or "").strip()
    case_word = str(jdoc.get("JCASE") or "").strip()
    no = str(jdoc.get("JNO") or "").strip()
    reason = str(jdoc.get("JTITLE") or "").strip()
    case_no = ""
    if y and case_word and no:
        case_no = f"{y}年度{case_word}字第{no}號"
    if case_no and reason:
        return f"{court} {case_no}{reason}"
    if case_no:
        return f"{court} {case_no}"
    if reason:
        return f"{court} {reason}"
    return f"{court} {jid}"


def _read_text_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def _jdg_processed_map_from_state() -> dict[str, str]:
    proc_state = _load_json_file(JDG_API_PROCESS_STATE_PATH, {"processed": {}})
    processed_map = proc_state.get("processed") if isinstance(proc_state, dict) else {}
    return processed_map if isinstance(processed_map, dict) else {}


def _jdg_backlog_status(processed_map: Optional[dict[str, str]] = None) -> dict:
    processed = processed_map if isinstance(processed_map, dict) else _jdg_processed_map_from_state()
    files = _iter_jdg_raw_files()
    backlog = 0
    unreadable = 0
    oldest_mtime: Optional[float] = None
    newest_mtime: Optional[float] = None

    for raw_path in files:
        rel = os.path.relpath(raw_path, JDG_API_ROOT)
        raw_text = _read_text_file(raw_path, default="")
        is_pending = False
        if not raw_text:
            unreadable += 1
            is_pending = True
        else:
            raw_hash = hashlib.sha1(raw_text.encode("utf-8", errors="ignore")).hexdigest()
            if processed.get(rel) != raw_hash:
                is_pending = True
        if not is_pending:
            continue
        backlog += 1
        try:
            mt = os.path.getmtime(raw_path)
            oldest_mtime = mt if oldest_mtime is None else min(oldest_mtime, mt)
            newest_mtime = mt if newest_mtime is None else max(newest_mtime, mt)
        except Exception as _e:
            logger.debug("getmtime skipped for %s: %s", raw_path, _e)

    now_ts = time.time()
    oldest_age_hours = 0.0
    newest_age_hours = 0.0
    if oldest_mtime is not None:
        oldest_age_hours = max(0.0, (now_ts - oldest_mtime) / 3600.0)
    if newest_mtime is not None:
        newest_age_hours = max(0.0, (now_ts - newest_mtime) / 3600.0)

    return {
        "raw_total": len(files),
        "processed_entries": len(processed),
        "backlog_count": backlog,
        "unreadable_count": unreadable,
        "oldest_backlog_age_hours": round(oldest_age_hours, 2),
        "newest_backlog_age_hours": round(newest_age_hours, 2),
    }


def _remember_judgment_memory(content: str, source: str, is_degraded: bool = False) -> bool:
    """Write judgment summary to vector memory. Skips degraded (broken) summaries."""
    if is_degraded:
        return False
    try:
        from skills.memory import mem_bridge  # type: ignore
        return bool(mem_bridge.remember(content, source=source))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 判決趨勢分析
# ---------------------------------------------------------------------------
def trend_analysis(case_reason: str = "", top_n: int = 10) -> dict:
    """分析 judgments.json 中的判決趨勢：依案由/法院統計分布、見解趨勢。"""
    json_path = _JUDGMENTS_JSON_PATH
    if not os.path.exists(json_path):
        return {"success": False, "error": "judgments.json 不存在"}
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            judgments = json.load(f)
        if not isinstance(judgments, list):
            return {"success": False, "error": "judgments.json 格式異常"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    total = len(judgments)
    if total == 0:
        return {"success": True, "total": 0, "message": "見解庫為空。"}

    # 依案由統計
    reason_counts: dict = {}
    court_counts: dict = {}
    monthly_counts: dict = {}
    reason_courts: dict = {}  # reason -> {court -> count}

    for j in judgments:
        reason = str(j.get("case_reason") or "未分類").strip()
        title = str(j.get("title") or "")
        ts = str(j.get("timestamp") or "")

        # 從 title 解析法院名稱
        court = "未知"
        for candidate in ["最高法院", "最高行政法院", "臺灣高等法院", "臺北高等行政法院",
                          "高等行政法院", "臺中高等行政法院", "高雄高等行政法院",
                          "智慧財產及商業法院"]:
            if candidate in title:
                court = candidate
                break
        if court == "未知":
            # 嘗試通用模式
            m = re.search(r"(臺灣\S+法院|[\u4e00-\u9fff]+法院)", title)
            if m:
                court = m.group(1)

        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        court_counts[court] = court_counts.get(court, 0) + 1

        if reason not in reason_courts:
            reason_courts[reason] = {}
        reason_courts[reason][court] = reason_courts[reason].get(court, 0) + 1

        # 月份統計
        if ts:
            month = ts[:7]  # YYYY-MM
            monthly_counts[month] = monthly_counts.get(month, 0) + 1

    # 排序
    top_reasons = sorted(reason_counts.items(), key=lambda x: -x[1])[:top_n]
    top_courts = sorted(court_counts.items(), key=lambda x: -x[1])[:top_n]

    # 如有指定案由，做深入分析
    focused = None
    if case_reason:
        matching = [j for j in judgments
                    if case_reason.lower() in str(j.get("case_reason") or "").lower()]
        if matching:
            # 統計該案由的法院分布
            focus_courts: dict = {}
            focus_keywords: dict = {}
            for j in matching:
                title = str(j.get("title") or "")
                court = "未知"
                for candidate in ["最高法院", "最高行政法院", "臺灣高等法院",
                                  "臺北高等行政法院", "臺中高等行政法院",
                                  "高雄高等行政法院", "高等行政法院",
                                  "智慧財產及商業法院"]:
                    if candidate in title:
                        court = candidate
                        break
                if court == "未知":
                    m_court = re.search(r"(臺灣\S+法院|[\u4e00-\u9fff]+法院)", title)
                    if m_court:
                        court = m_court.group(1)
                if court != "未知":
                    focus_courts[court] = focus_courts.get(court, 0) + 1

                # 從 summary 抽取關鍵法條
                summary = str(j.get("summary") or "")
                statutes = re.findall(r"第\d+(?:-\d+)?條(?:之\d+)?", summary)
                for s in statutes:
                    focus_keywords[s] = focus_keywords.get(s, 0) + 1

            top_statutes = sorted(focus_keywords.items(), key=lambda x: -x[1])[:8]
            focused = {
                "case_reason": case_reason,
                "count": len(matching),
                "courts": dict(sorted(focus_courts.items(), key=lambda x: -x[1])),
                "top_statutes": top_statutes,
            }

    # 趨勢（月份走勢）
    sorted_months = sorted(monthly_counts.items())

    result = {
        "success": True,
        "total": total,
        "top_reasons": top_reasons,
        "top_courts": top_courts,
        "monthly_trend": sorted_months[-12:],  # 最近 12 個月
    }
    if focused:
        result["focused_analysis"] = focused

    return result


def format_trend_report(data: dict) -> str:
    """將趨勢分析結果格式化為人類可讀報告。"""
    if not data.get("success"):
        return f"❌ {data.get('error', '分析失敗')}"

    lines = [
        "📊 MAGI 判決趨勢分析",
        f"見解庫總量：{data.get('total', 0)} 筆",
        "",
    ]

    top_reasons = data.get("top_reasons", [])
    if top_reasons:
        lines.append("【案由分布 Top 10】")
        for reason, count in top_reasons:
            bar = "█" * min(count, 30)
            lines.append(f"  {reason}: {count} 筆 {bar}")
        lines.append("")

    top_courts = data.get("top_courts", [])
    if top_courts:
        lines.append("【法院分布 Top 10】")
        for court, count in top_courts:
            lines.append(f"  {court}: {count} 筆")
        lines.append("")

    monthly = data.get("monthly_trend", [])
    if monthly:
        lines.append("【月度收錄趨勢】")
        for month, count in monthly:
            bar = "▓" * min(count // 2, 30) if count > 0 else "·"
            lines.append(f"  {month}: {count:>4} 筆 {bar}")
        lines.append("")

    focused = data.get("focused_analysis")
    if focused:
        lines.append(f"【{focused['case_reason']}深入分析】")
        lines.append(f"  相關判決：{focused['count']} 筆")
        courts = focused.get("courts", {})
        if courts:
            lines.append("  法院分布：")
            for court, count in courts.items():
                lines.append(f"    {court}: {count}")
        statutes = focused.get("top_statutes", [])
        if statutes:
            lines.append("  常引法條：")
            for s, count in statutes:
                lines.append(f"    {s}: {count} 次")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 跨案件見解綜合分析
# ---------------------------------------------------------------------------
def synthesize_holdings(case_reason: str, statute: str = "", max_items: int = 20) -> str:
    """
    綜合分析特定案由/法條在不同法院的裁判見解。

    回傳：各法院見解比較、多數/少數見解歸納、法條適用趨勢。
    只使用通過品質檢查的 LLM 摘要，寧缺勿濫。
    """
    if not case_reason:
        return "❌ 請指定案由，例如：「綜合分析 詐欺」"

    json_path = _JUDGMENTS_JSON_PATH
    if not os.path.exists(json_path):
        return "❌ judgments.json 不存在"

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            judgments = json.load(f)
        if not isinstance(judgments, list):
            return "❌ judgments.json 格式異常"
    except Exception as e:
        return f"❌ 讀取失敗：{e}"

    reason_lower = case_reason.lower()
    statute_filter = statute.strip()

    # 篩選匹配案由的判決（只取有 LLM 摘要的）
    matched = []
    for j in judgments:
        if j.get("summary_type") not in (None, "llm"):
            continue
        j_reason = str(j.get("case_reason") or "").lower()
        if reason_lower not in j_reason and j_reason not in reason_lower:
            continue
        summary = str(j.get("summary") or "")
        if len(summary) < 50:
            continue
        # 降級標記過濾
        if any(m in summary for m in _JUNK_KEYWORDS):
            continue
        # 法條過濾（如有指定）
        if statute_filter and statute_filter not in summary:
            continue
        matched.append(j)

    if not matched:
        return f"❌ 找不到「{case_reason}」的可信判決見解。"

    # 按法院分組
    court_groups: dict = {}
    for j in matched[:max_items]:
        title = str(j.get("title") or "")
        court = "未知法院"
        for c in ["最高法院", "最高行政法院", "臺灣高等法院", "臺北高等行政法院",
                   "臺中高等行政法院", "高雄高等行政法院", "高等行政法院",
                   "智慧財產及商業法院"]:
            if c in title:
                court = c
                break
        if court == "未知法院":
            m = re.search(r"(臺灣\S+法院|[\u4e00-\u9fff]+法院)", title)
            if m:
                court = m.group(1)
        if court not in court_groups:
            court_groups[court] = []
        court_groups[court].append(j)

    # 抽取各判決的裁判要旨
    def _extract_holding(summary: str) -> str:
        m = re.search(r"(?:##\s*裁判要旨|裁判要旨)\s*\n(.*?)(?=\n##|\Z)", summary, re.DOTALL)
        if m:
            return m.group(1).strip().replace("\n", " ")[:200]
        return summary[:200].replace("\n", " ")

    # 抽取法條
    all_statutes: dict = {}
    for j in matched[:max_items]:
        summary = str(j.get("summary") or "")
        for s in re.findall(r"(?:民法|刑法|民事訴訟法|刑事訴訟法|行政訴訟法|勞動基準法|勞基法|公司法|消費者保護法|稅捐稽徵法|所得稅法|土地法)?\s*第\d+(?:-\d+)?條(?:之\d+)?", summary):
            s = s.strip()
            all_statutes[s] = all_statutes.get(s, 0) + 1

    # 格式化報告
    lines = [
        f"📚 MAGI 判決見解綜合分析",
        f"案由：{case_reason}" + (f"（法條：{statute_filter}）" if statute_filter else ""),
        f"分析對象：{len(matched)} 筆可信判決",
        f"涵蓋法院：{len(court_groups)} 個",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # 各法院見解
    for court, group in sorted(court_groups.items(), key=lambda x: -len(x[1])):
        lines.append(f"🏛 {court}（{len(group)} 筆）")
        for j in group[:5]:
            title = str(j.get("title") or "")
            holding = _extract_holding(str(j.get("summary") or ""))
            lines.append(f"  • [{title}]")
            lines.append(f"    {holding}")
        if len(group) > 5:
            lines.append(f"  ...另有 {len(group) - 5} 筆")
        lines.append("")

    # 常引法條
    top_statutes = sorted(all_statutes.items(), key=lambda x: -x[1])[:10]
    if top_statutes:
        lines.append("📜 常引法條：")
        for s, count in top_statutes:
            lines.append(f"  {s}: {count} 次")
        lines.append("")

    # 見解趨勢摘要
    if len(matched) >= 3:
        lines.append("📈 見解趨勢：")
        if len(court_groups) == 1:
            lines.append(f"  見解來源集中於 {list(court_groups.keys())[0]}，建議擴大搜集範圍。")
        else:
            main_court = max(court_groups.items(), key=lambda x: len(x[1]))
            lines.append(f"  主要見解來源：{main_court[0]}（{len(main_court[1])} 筆，佔 {len(main_court[1])*100//len(matched)}%）")
            if "最高法院" in court_groups or "最高行政法院" in court_groups:
                supreme_key = "最高法院" if "最高法院" in court_groups else "最高行政法院"
                lines.append(f"  {supreme_key}已有 {len(court_groups[supreme_key])} 筆見解可作為主要依據。")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _load_jsonish(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"case_reason": text}


def _is_admin_case(case_type: str, case_reason: str) -> bool:
    combined = (case_type or "") + " " + (case_reason or "")
    return any(kw in combined for kw in ADMIN_KEYWORDS)


def _get_courts(case_type: str, case_reason: str) -> list[str]:
    if _is_admin_case(case_type, case_reason):
        return [
            "最高行政法院(含改制前行政法院)",
            "臺北高等行政法院",
            "臺中高等行政法院",
            "高雄高等行政法院",
        ]
    return ["最高法院"]


def _get_court_display(case_type: str, case_reason: str) -> str:
    if _is_admin_case(case_type, case_reason):
        return "最高行政法院 + 高等行政法院"
    return "最高法院"


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def _get_db():
    try:
        import mysql.connector
        cfg = _get_db_config()
        # 防呆：避免 DB 連線在 Keeper/網路不通時無限卡住
        try:
            cfg.setdefault(
                "connection_timeout",
                int(_env("JUDGMENT_DB_CONNECT_TIMEOUT_SEC", "3") or "3"),
            )
        except Exception:
            cfg.setdefault("connection_timeout", 3)
        conn = mysql.connector.connect(**cfg)
        return conn
    except Exception as e:
        logger.warning("DB connect failed: %s", e)
        return None


def _ensure_table(conn):
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS judgment_archive ("
            "  id              INT AUTO_INCREMENT PRIMARY KEY,"
            "  case_number     VARCHAR(64) DEFAULT '',"
            "  case_reason     VARCHAR(128) DEFAULT '',"
            "  case_type       VARCHAR(32) DEFAULT '',"
            "  court_level     VARCHAR(64) DEFAULT '',"
            "  judgment_title  VARCHAR(512) NOT NULL,"
            "  judgment_url    VARCHAR(1024) DEFAULT '',"
            "  judgment_date   VARCHAR(32) DEFAULT '',"
            "  full_text_path  VARCHAR(1024) DEFAULT '',"
            "  summary_text    TEXT,"
            "  is_degraded     TINYINT(1) NOT NULL DEFAULT 0,"
            "  search_query    VARCHAR(512) DEFAULT '',"
            "  source          VARCHAR(64) DEFAULT 'judgment-collector',"
            "  source_jid      VARCHAR(100) DEFAULT '',"
            "  crawled_at      DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "  INDEX idx_reason (case_reason),"
            "  INDEX idx_case   (case_number),"
            "  INDEX idx_source_jid (source_jid)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        # Backward compatibility for old schema (column/index may already exist — that is OK).
        try:
            cur.execute("ALTER TABLE judgment_archive ADD COLUMN is_degraded TINYINT(1) NOT NULL DEFAULT 0")
            conn.commit()
        except Exception as _e:
            logger.debug("ALTER TABLE is_degraded skipped (likely already exists): %s", _e)
        try:
            cur.execute("ALTER TABLE judgment_archive ADD COLUMN source VARCHAR(64) DEFAULT 'judgment-collector'")
            conn.commit()
        except Exception as _e:
            logger.debug("ALTER TABLE source skipped (likely already exists): %s", _e)
        try:
            cur.execute("ALTER TABLE judgment_archive ADD COLUMN source_jid VARCHAR(100) DEFAULT ''")
            conn.commit()
        except Exception as _e:
            logger.debug("ALTER TABLE source_jid skipped (likely already exists): %s", _e)
        try:
            cur.execute("ALTER TABLE judgment_archive ADD INDEX idx_source_jid (source_jid)")
            conn.commit()
        except Exception as _e:
            logger.debug("ALTER TABLE idx_source_jid skipped (likely already exists): %s", _e)
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning("Table creation issue: %s", e)


def _ensure_court_judgments_table(conn):
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS court_judgments ("
            "  id INT AUTO_INCREMENT PRIMARY KEY,"
            "  jid VARCHAR(100) UNIQUE,"
            "  court_name VARCHAR(100) DEFAULT NULL,"
            "  case_number VARCHAR(200) DEFAULT NULL,"
            "  case_type VARCHAR(50) DEFAULT NULL,"
            "  judgment_date DATE DEFAULT NULL,"
            "  summary MEDIUMTEXT,"
            "  full_text LONGTEXT,"
            "  source_url TEXT,"
            "  crawled_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning("court_judgments ensure table failed: %s", e)


def _parse_judgment_date_from_text(text: str):
    s = str(text or "")
    m = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", s)
    if not m:
        return None
    try:
        y = int(m.group(1)) + 1911
        mo = int(m.group(2))
        d = int(m.group(3))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None
    return None


def _parse_court_case_from_title(title: str) -> tuple[str, str]:
    t = str(title or "").strip()
    if not t:
        return "", ""
    courts = [
        "最高法院",
        "最高行政法院",
        "臺北高等行政法院",
        "臺中高等行政法院",
        "高雄高等行政法院",
        "臺南高等行政法院",
        "臺灣高等法院",
    ]
    court = ""
    for c in courts:
        if t.startswith(c):
            court = c
            break
    markers = _extract_case_markers(t)
    case_no = sorted(markers, key=lambda x: len(x), reverse=True)[0] if markers else ""
    return court, case_no


def _upsert_court_judgment(
    conn,
    *,
    title: str,
    url: str,
    summary: str,
    full_text: str,
    case_type: str,
    commit: bool = True,
) -> Optional[str]:
    if not conn:
        return None
    jid_seed = (str(url or "").strip() or str(title or "").strip() or str(time.time()))
    jid = hashlib.sha1(jid_seed.encode("utf-8", errors="ignore")).hexdigest()[:40]
    court_name, case_no = _parse_court_case_from_title(title)
    judgment_date = _parse_judgment_date_from_text(full_text) or _parse_judgment_date_from_text(title)
    safe_summary, _ = _safe_summary_for_storage(summary)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO court_judgments
            (jid, court_name, case_number, case_type, judgment_date, summary, full_text, source_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              court_name=VALUES(court_name),
              case_number=VALUES(case_number),
              case_type=VALUES(case_type),
              judgment_date=VALUES(judgment_date),
              summary=VALUES(summary),
              full_text=VALUES(full_text),
              source_url=VALUES(source_url),
              crawled_at=CURRENT_TIMESTAMP
            """,
            (
                jid,
                court_name or None,
                case_no or None,
                (case_type or None),
                judgment_date,
                (safe_summary or None),
                (full_text or None),
                (url or None),
            ),
        )
        if commit:
            conn.commit()
        cur.close()
        return jid
    except Exception as e:
        logger.warning("court_judgments upsert failed: %s", e)
        return None


def _upsert_court_judgment_by_jid(
    conn,
    *,
    jid: str,
    court_name: str,
    case_number: str,
    case_type: str,
    judgment_date: Optional[str],
    summary: str,
    full_text: str,
    source_url: str,
    commit: bool = True,
) -> bool:
    if not conn:
        return False
    j = str(jid or "").strip()
    if not j:
        return False
    safe_summary, _ = _safe_summary_for_storage(summary)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO court_judgments
            (jid, court_name, case_number, case_type, judgment_date, summary, full_text, source_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              court_name=VALUES(court_name),
              case_number=VALUES(case_number),
              case_type=VALUES(case_type),
              judgment_date=VALUES(judgment_date),
              summary=VALUES(summary),
              full_text=VALUES(full_text),
              source_url=VALUES(source_url),
              crawled_at=CURRENT_TIMESTAMP
            """,
            (
                j,
                (court_name or None),
                (case_number or None),
                (case_type or None),
                judgment_date,
                (safe_summary or None),
                (full_text or None),
                (source_url or None),
            ),
        )
        if commit:
            conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.warning("court_judgments upsert-by-jid failed: %s", e)
        return False


def _backfill_court_judgments_from_archive(conn, limit: int = 0) -> dict:
    if not conn:
        return {"success": False, "error": "db_not_connected"}
    _ensure_table(conn)
    _ensure_court_judgments_table(conn)

    rows = []
    try:
        cur = conn.cursor(dictionary=True)
        sql = (
            "SELECT id, judgment_title, judgment_url, summary_text, full_text_path, case_type "
            "FROM judgment_archive "
            "ORDER BY id DESC"
        )
        params = ()
        if (not _is_unlimited(limit)) and int(limit) > 0:
            sql += " LIMIT %s"
            params = (int(limit),)
        cur.execute(sql, params)
        rows = cur.fetchall() or []
        cur.close()
    except Exception as e:
        return {"success": False, "error": f"query_failed: {str(e)[:200]}"}

    inserted = 0
    skipped = 0
    failed = 0
    for r in rows:
        title = str((r or {}).get("judgment_title") or "").strip()
        if not title:
            skipped += 1
            continue
        url = str((r or {}).get("judgment_url") or "").strip()
        summary = str((r or {}).get("summary_text") or "")
        full_text_path = str((r or {}).get("full_text_path") or "").strip()
        case_type = str((r or {}).get("case_type") or "").strip()
        full_text = ""
        if full_text_path and os.path.exists(full_text_path):
            try:
                with open(full_text_path, "r", encoding="utf-8", errors="replace") as f:
                    full_text = f.read()
            except Exception:
                full_text = ""
        jid = _upsert_court_judgment(
            conn,
            title=title,
            url=url,
            summary=summary,
            full_text=full_text,
            case_type=case_type,
        )
        if jid:
            inserted += 1
        else:
            failed += 1

    return {
        "success": True,
        "scanned": len(rows),
        "upserted": inserted,
        "skipped": skipped,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Backfill: scan archive text files → summarize → store in judgments.json
# ---------------------------------------------------------------------------
ARCHIVE_ROOT = os.path.join(PROJECT_ROOT, "archive", "judicial_search")

def _parse_archive_filename(name: str) -> dict:
    """Parse '001_最高法院 115 年度 台上 字第 267 號民事裁定.txt' → structured info."""
    m = re.match(
        r'\d+_(.+?)\s+(\d+)\s+年度\s+(.+?)\s+字第\s+(\d+)\s+號(.+?)\.txt',
        name,
    )
    if not m:
        return {}
    court = m.group(1).strip()
    year = m.group(2)
    case_code = m.group(3).strip()
    number = m.group(4)
    suffix = m.group(5).strip()
    # Extract case_type and judgment_type from suffix like "民事判決" or "刑事裁定"
    case_type = ""
    if "民事" in suffix:
        case_type = "民事"
    elif "刑事" in suffix:
        case_type = "刑事"
    elif "行政" in suffix:
        case_type = "行政"
    # Extract case_reason from suffix (everything before 判決/裁定)
    reason_match = re.match(r'(.*?)(判決|裁定)$', suffix)
    verdict_type = ""
    if reason_match:
        verdict_type = reason_match.group(2)
    title = f"{court} {year}年度{case_code}字第{number}號{suffix}"
    return {
        "court": court,
        "year": int(year),
        "case_code": case_code,
        "number": number,
        "case_type": case_type,
        "verdict_type": verdict_type,
        "title": title,
        "suffix": suffix,
    }


def backfill_archive_summaries(
    max_items: int = 50,
    min_text_bytes: int = 2000,
    timeout_per_item: int = 300,
    year_min: int = 0,
    year_max: int = 9999,
    notify: bool = False,
) -> dict:
    """
    掃描 archive/judicial_search/ 下的全文檔案，
    用 LLM 生成結構化見解摘要，存入 judgments.json。

    - 只處理 >min_text_bytes 的檔案（排除空檔）
    - 自動跳過 judgments.json 中已有的 title（去重）
    - 每筆呼叫 _summarize_judgment 取得結構化摘要
    - 摘要品質不合格（降級/幻覺）的不存入
    """
    if not os.path.isdir(ARCHIVE_ROOT):
        return {"success": False, "error": f"archive dir not found: {ARCHIVE_ROOT}"}

    # 1) Build list of candidate text files
    candidates = []
    for direntry in os.scandir(ARCHIVE_ROOT):
        if not direntry.is_dir():
            continue
        for f in os.scandir(direntry.path):
            if not f.name.endswith('.txt') or f.name == 'report.txt':
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size < min_text_bytes:
                continue
            info = _parse_archive_filename(f.name)
            if not info:
                continue
            yr = info.get("year", 0)
            if yr < year_min or yr > year_max:
                continue
            candidates.append({"path": f.path, "size": size, **info})

    # Sort by year desc (newest first), then size desc (longer = richer content)
    candidates.sort(key=lambda c: (-c["year"], -c["size"]))
    logger.info("backfill_archive_summaries: %d candidates found (year %d-%d)",
                len(candidates), year_min, year_max)

    # 2) Load existing judgments.json to check what we already have
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "judgments.json")
    existing = []
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing_titles = {d.get("title", "").replace(" ", "") for d in existing}

    # 3) Process candidates
    added = 0
    skipped_dup = 0
    skipped_degraded = 0
    failed = 0
    processed = 0
    import time as _time

    for cand in candidates:
        if added >= max_items:
            break
        # Dedup check
        title_norm = cand["title"].replace(" ", "")
        if title_norm in existing_titles:
            skipped_dup += 1
            continue

        # Read full text
        try:
            with open(cand["path"], "r", encoding="utf-8", errors="replace") as f:
                full_text = f.read()
        except Exception:
            failed += 1
            continue

        if len(full_text.strip()) < 500:
            failed += 1
            continue

        # Extract case_reason from the text itself (look for 裁判案由 or 案由 line)
        case_reason = ""
        reason_m = re.search(r'(?:裁判案由|案\s*由)[：:]\s*(.+)', full_text[:3000])
        if reason_m:
            case_reason = reason_m.group(1).strip()[:30]
        if not case_reason:
            # Fallback: use suffix minus case_type and verdict_type
            fallback = cand["suffix"]
            for remove in ("民事", "刑事", "行政", "判決", "裁定"):
                fallback = fallback.replace(remove, "")
            case_reason = fallback.strip() or cand["case_type"]

        # Summarize
        processed += 1
        logger.info("  [%d/%d] Summarizing: %s (%.1fKB, reason=%s)",
                     processed, max_items, cand["title"][:60], cand["size"]/1024, case_reason)
        _start = _time.monotonic()
        summary = _summarize_judgment(full_text, case_reason, timeout_sec=timeout_per_item)
        _elapsed = _time.monotonic() - _start
        meta = _get_last_summary_meta()

        # Quality check
        is_bad = bool(
            meta.get("is_degraded", False)
            or _is_degraded_summary(summary, case_reason)
            or len(summary.strip()) < 50
        )
        if is_bad:
            skipped_degraded += 1
            logger.info("    -> degraded (%.1fs), skipping", _elapsed)
            continue

        # Store in judgments.json
        entry = {
            "title": cand["title"],
            "url": "",
            "summary": _tw(summary),
            "summary_type": "llm",
            "case_reason": _tw(case_reason),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "archive_backfill",
        }
        existing.insert(0, entry)
        existing_titles.add(title_norm)
        added += 1
        logger.info("    -> OK (%.1fs, %d chars)", _elapsed, len(summary))

    # 4) Save (keep max 2000 to accommodate ongoing growth)
    existing = existing[:2000]
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"success": False, "error": f"save failed: {e}",
                "added": added, "processed": processed}

    result = {
        "success": True,
        "total_candidates": len(candidates),
        "processed": processed,
        "added": added,
        "skipped_duplicate": skipped_dup,
        "skipped_degraded": skipped_degraded,
        "failed": failed,
        "judgments_json_total": len(existing),
    }
    logger.info("backfill_archive_summaries done: %s", result)

    if notify and added > 0:
        _notify(
            f"📚 見解庫回填完成 — 新增 {added} 筆結構化摘要\n"
            f"掃描: {len(candidates)} | 處理: {processed} | 降級跳過: {skipped_degraded}",
            True,
        )
    return result


def _store_judgment(conn, row: dict, *, commit: bool = True) -> Optional[int]:
    if not conn:
        return None
    row = dict(row or {})
    safe_summary, is_bad = _safe_summary_for_storage(
        str(row.get("summary_text") or ""),
        is_degraded=bool(row.get("is_degraded", False)),
    )
    row["summary_text"] = safe_summary
    row["is_degraded"] = bool(row.get("is_degraded", False) or is_bad)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO judgment_archive"
            " (case_number, case_reason, case_type, court_level,"
            "  judgment_title, judgment_url, judgment_date,"
            "  full_text_path, summary_text, is_degraded, search_query, source, source_jid)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                row.get("case_number", ""),
                row.get("case_reason", ""),
                row.get("case_type", ""),
                row.get("court_level", ""),
                row.get("judgment_title", ""),
                row.get("judgment_url", ""),
                row.get("judgment_date", ""),
                row.get("full_text_path", ""),
                row.get("summary_text", ""),
                1 if bool(row.get("is_degraded", False)) else 0,
                row.get("search_query", ""),
                row.get("source", "judgment-collector"),
                row.get("source_jid", ""),
            ),
        )
        if commit:
            conn.commit()
        rid = cur.lastrowid
        cur.close()
        return rid
    except Exception as e:
        logger.warning("DB insert failed: %s", e)
        return None


def _upsert_judgment_archive_by_source_jid(conn, *, source_jid: str, row: dict, commit: bool = True) -> Optional[int]:
    sid = str(source_jid or "").strip()
    row = dict(row or {})
    safe_summary, is_bad = _safe_summary_for_storage(
        str(row.get("summary_text") or ""),
        is_degraded=bool(row.get("is_degraded", False)),
    )
    row["summary_text"] = safe_summary
    row["is_degraded"] = bool(row.get("is_degraded", False) or is_bad)
    if (not conn) or (not sid):
        return _store_judgment(conn, row, commit=commit)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id FROM judgment_archive WHERE source_jid=%s ORDER BY id DESC LIMIT 1",
            (sid,),
        )
        hit = cur.fetchone() or {}
        cur.close()
        if not hit:
            row2 = dict(row or {})
            row2["source_jid"] = sid
            row2.setdefault("source", "judicial_api")
            return _store_judgment(conn, row2, commit=commit)

        rid = int(hit.get("id") or 0)
        cur = conn.cursor()
        cur.execute(
            "UPDATE judgment_archive "
            "SET case_number=%s, case_reason=%s, case_type=%s, court_level=%s, "
            "judgment_title=%s, judgment_url=%s, judgment_date=%s, full_text_path=%s, "
            "summary_text=%s, is_degraded=%s, search_query=%s, source=%s, source_jid=%s, "
            "crawled_at=CURRENT_TIMESTAMP "
            "WHERE id=%s",
            (
                row.get("case_number", ""),
                row.get("case_reason", ""),
                row.get("case_type", ""),
                row.get("court_level", ""),
                row.get("judgment_title", ""),
                row.get("judgment_url", ""),
                row.get("judgment_date", ""),
                row.get("full_text_path", ""),
                row.get("summary_text", ""),
                1 if bool(row.get("is_degraded", False)) else 0,
                row.get("search_query", ""),
                row.get("source", "judicial_api"),
                sid,
                rid,
            ),
        )
        # 注意：casper_service 帳號無 DELETE 權限，跳過重複清理。
        # 同 source_jid 的舊列會保留但不影響查詢（SELECT 時已用 ORDER BY id DESC LIMIT 1）。
        if commit:
            conn.commit()
        cur.close()
        return rid
    except Exception as e:
        logger.warning("judgment_archive upsert-by-source_jid failed: %s", e)
        row2 = dict(row or {})
        row2["source_jid"] = sid
        row2.setdefault("source", "judicial_api")
        return _store_judgment(conn, row2, commit=commit)


def _update_summary_by_text_path(
    conn,
    *,
    full_text_path: str,
    summary_text: str,
    title: str = "",
    case_reason: str = "",
    is_degraded: Optional[bool] = None,
) -> int:
    """
    重試摘要成功後回寫 DB，避免佇列改善結果只留在記憶事件、不更新資料庫內容。
    """
    if (not conn) or (not full_text_path) or (not summary_text):
        return 0
    safe_summary, safe_bad = _safe_summary_for_storage(summary_text, is_degraded=bool(is_degraded))
    if safe_bad or not safe_summary:
        return 0
    summary_text = safe_summary
    try:
        cur = conn.cursor()
        # 先用 full_text_path 精準更新；若沒有命中，再 fallback title/case_reason。
        if is_degraded is None:
            cur.execute(
                "UPDATE judgment_archive SET summary_text=%s WHERE full_text_path=%s",
                (summary_text, full_text_path),
            )
        else:
            cur.execute(
                "UPDATE judgment_archive SET summary_text=%s, is_degraded=%s WHERE full_text_path=%s",
                (summary_text, 1 if is_degraded else 0, full_text_path),
            )
        affected = int(cur.rowcount or 0)
        if affected <= 0 and title:
            if is_degraded is None:
                cur.execute(
                    "UPDATE judgment_archive SET summary_text=%s WHERE judgment_title=%s AND case_reason=%s",
                    (summary_text, title, case_reason),
                )
            else:
                cur.execute(
                    "UPDATE judgment_archive SET summary_text=%s, is_degraded=%s WHERE judgment_title=%s AND case_reason=%s",
                    (summary_text, 1 if is_degraded else 0, title, case_reason),
                )
            affected = int(cur.rowcount or 0)
        conn.commit()
        cur.close()
        return affected
    except Exception as e:
        logger.warning("DB summary update failed: %s", e)
        return 0


def _lookup_case(conn, case_number: str) -> dict:
    if not conn or not case_number:
        return {}
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT case_reason, case_type, client_name, folder_path "
            "FROM cases WHERE case_number = %s LIMIT 1",
            (case_number,),
        )
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else {}
    except Exception as e:
        logger.warning("Case lookup failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# oMLX inference health gate
# ---------------------------------------------------------------------------
_OMLX_HEALTHY: Optional[bool] = None
_OMLX_HEALTHY_TS: float = 0.0
_OMLX_HEALTH_TTL: float = 120.0  # cache result for 2 min


def _omlx_inference_ok(timeout: int = 15) -> bool:
    """Probe oMLX with a tiny real inference request.  Returns True if alive."""
    global _OMLX_HEALTHY, _OMLX_HEALTHY_TS
    now = time.monotonic()
    if _OMLX_HEALTHY is not None and (now - _OMLX_HEALTHY_TS) < _OMLX_HEALTH_TTL:
        return _OMLX_HEALTHY

    # Also check watchdog state file if available
    state_path = os.path.expanduser(
        os.environ.get(
            "MAGI_OMLX_WATCHDOG_STATE_PATH",
            "~/Library/Application Support/MAGI/omlx_watchdog_state.json",
        )
    )
    try:
        if os.path.exists(state_path):
            with open(state_path, "r") as f:
                ws = json.load(f)
            status = ws.get("status", "")
            if status in ("restarting", "cooldown"):
                suspend_until = float(ws.get("suspend_until", 0) or 0)
                if suspend_until > time.time():
                    logger.info("oMLX watchdog says %s until %.0f, skipping probe", status, suspend_until)
                    _OMLX_HEALTHY = False
                    _OMLX_HEALTHY_TS = now
                    return False
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1995, exc_info=True)

    try:
        from api.routing.service_registry import get_service_url as _gsurl
        _omlx_def = _gsurl("omlx_inference")
    except Exception:
        _omlx_def = "http://127.0.0.1:8080"
    omlx_url = os.environ.get("MAGI_OMLX_CHAT_URL", _omlx_def)
    model = os.environ.get("MAGI_OMLX_GENERAL_MODEL", os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""))
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0.0,
    }).encode()
    req = _urlrequest.Request(
        omlx_url.rstrip("/") + "/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with _urlrequest.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            ok = bool(body.get("choices"))
    except Exception as e:
        logger.warning("oMLX inference probe failed: %s", e)
        ok = False

    _OMLX_HEALTHY = ok
    _OMLX_HEALTHY_TS = now
    return ok


# ---------------------------------------------------------------------------
# Sub-Skill Invocation
# ---------------------------------------------------------------------------
def _run_skill(skill: str, task: str, timeout_sec: int = 120, route_key: str = "") -> dict:
    import urllib.request
    import urllib.error

    try:
        from api.routing.service_registry import get_service_url as _gsurl2
        _tools_def = _gsurl2("tools_api")
    except Exception:
        _tools_def = "http://127.0.0.1:5003"
    tools_api = os.environ.get("MAGI_TOOLS_API", _tools_def).rstrip("/")
    payload = {
        "skill": skill,
        "task": task,
        "timeout_sec": int(timeout_sec),
        "auto_repair": False,
        "rollback_on_fail": True,
        "auto_install_deps": False,
        "route_key": route_key,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        tools_api + "/skills/run", data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=max(5, int(timeout_sec) + 30)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        # tools_api returns 400 when a skill fails; capture the JSON body for diagnosis.
        try:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw or "{}")
                if isinstance(obj, dict):
                    obj.setdefault("success", False)
                    obj.setdefault("http_status", int(getattr(e, "code", 400) or 400))
                    return obj
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2060, exc_info=True)
            return {
                "success": False,
                "http_status": int(getattr(e, "code", 400) or 400),
                "error": f"HTTP Error {getattr(e, 'code', '')}: {getattr(e, 'reason', '')}".strip(),
                "body": (raw or "")[:800],
            }
        except Exception:
            return {"success": False, "error": str(e)[:240]}
    except Exception as e:
        return {"success": False, "error": str(e)[:240]}


def _skill_json_task(command: str, payload: dict) -> str:
    return f"{str(command or '').strip()}{json.dumps(payload or {}, ensure_ascii=False, separators=(',', ':'))}"


def _parse_skill_output(run_result: dict) -> dict:
    if not isinstance(run_result, dict) or not run_result.get("success"):
        return {"success": False, "error": (run_result.get("error") if isinstance(run_result, dict) else "failed")}
    raw = (run_result.get("output") or "").strip()
    if not raw:
        return {"success": False, "error": "empty output"}
    try:
        obj = json.loads(raw)
    except Exception:
        return {"success": False, "error": "json parse fail", "raw": raw[:500]}
    return obj if isinstance(obj, dict) else {"success": False, "error": "not a dict"}


def _parse_json_from_stdout(stdout: str) -> dict:
    """
    子程序模式下，stdout 可能包含多段輸出；我們盡量抓最後一個 JSON 物件。
    """
    s = (stdout or "").strip()
    if not s:
        return {"success": False, "error": "empty stdout"}
    # Find the last JSON object start.
    last = s.rfind("{")
    if last < 0:
        return {"success": False, "error": "no json object in stdout", "stdout_tail": s[-500:]}
    try:
        obj = json.loads(s[last:])
        return obj if isinstance(obj, dict) else {"success": False, "error": "json not dict"}
    except Exception:
        # fallback: try whole stdout
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {"success": False, "error": "json not dict"}
        except Exception:
            return {"success": False, "error": "json parse fail", "stdout_tail": s[-800:]}


def _collect_with_hard_timeout(payload: dict, hard_timeout_sec: int) -> dict:
    """
    daily_crawl 的硬保護：把 collect() 放到子程序，用 subprocess timeout 保證不會卡死整輪 nightly。
    """
    try:
        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "--task",
            "collect " + json.dumps(payload or {}, ensure_ascii=False),
        ]
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(10, int(hard_timeout_sec)),
        )
        out = _parse_json_from_stdout(cp.stdout)
        if not isinstance(out, dict):
            out = {"success": False, "error": "collect returned non-dict json"}
        # preserve stderr for diagnosis (tail only)
        if cp.stderr:
            out.setdefault("stderr_tail", cp.stderr[-800:])
        return out
    except subprocess.TimeoutExpired:
        return {"success": False, "timeout": True, "error": f"collect timeout after {hard_timeout_sec}s"}
    except Exception as e:
        return {"success": False, "error": str(e)[:240]}


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------
def _notify(text: str, notify_flag: bool = True, *, topic_key: str = "judgment"):
    if not notify_flag:
        return
    try:
        magi_dir = os.environ.get("MAGI_DIR", _MAGI_ROOT)
        if magi_dir not in sys.path:
            sys.path.insert(0, magi_dir)
        from skills.ops.red_phone import send_telegram_push_with_status
        send_telegram_push_with_status(
            text,
            severity="info",
            source="judgment_collector",
            topic_key=topic_key,
            queue_on_fail=True,
        )
    except Exception:
        # Fallback to LAFNotifier if red_phone unavailable
        try:
            if CODE_DIR not in sys.path:
                sys.path.insert(0, CODE_DIR)
            from line_notifier import LAFNotifier
            n = LAFNotifier()
            n.notify_admin(text, topic_key=topic_key, source="judgment_collector")
        except Exception as e:
            logger.warning("Notification failed: %s", e)


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------
def _is_degraded_summary(text: str, expected_reason: str = "") -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    flags = [
        "（系統降級回覆）",
        "(系統降級回覆)",
        "（降級摘要）",
        "(降級摘要)",
        "摘要失敗，前 20 行預覽",
        "請稍後再試",
        "模型忙碌",
        "逾時",
        "timeout",
    ]
    if any(f in s for f in flags):
        return True
    if is_non_extractable_legal_insight(s):
        logger.warning("Non-extractable legal insight placeholder detected in summary (len=%d)", len(s))
        return True
    if _summary_is_prompt_echo(s):
        logger.warning("Prompt echo / missing-source response detected in summary (len=%d)", len(s))
        return True

    # ── Prompt leakage guard ──
    # LLM sometimes echoes back the system prompt instead of generating a summary.
    _prompt_leak_markers = [
        "你是資深法律研究助理",
        "專精司法見解分析",
        "【摘要格式要求】請嚴格按照",
        "必須忠實於判決原文，不得臆測",
        "EXECUTE WFGY PROTOCOL",
        "BBMC (Residue Cleanup)",
        "7-STEP REASONING CHAIN",
    ]
    if any(m in s for m in _prompt_leak_markers):
        logger.warning("Prompt leakage detected in summary (len=%d)", len(s))
        return True

    # ── Garbled text / encoding corruption guard ──
    # High ratio of replacement chars (U+FFFD) or control chars indicates corruption.
    _garbage_chars = sum(1 for c in s if c == '\ufffd' or (ord(c) < 32 and c not in '\n\r\t'))
    if len(s) > 50 and _garbage_chars / len(s) > 0.03:
        logger.warning("Garbled text detected in summary: %d/%d garbage chars", _garbage_chars, len(s))
        return True

    # ── Hallucination guard ──
    # If summary mentions a drastically different 裁判案由, flag as degraded.
    if expected_reason and len(s) > 100:
        _reason_norm = expected_reason.replace(" ", "")
        if _reason_norm not in s.replace(" ", ""):
            _other_reason = re.search(r"裁判案由[：:]\s*(.+)", s)
            if _other_reason:
                _found = _other_reason.group(1).strip()
                if _found and _reason_norm not in _found.replace(" ", ""):
                    logger.warning(
                        "Hallucination detected: expected=%s, found=%s",
                        expected_reason, _found,
                    )
                    return True
    return False


def _append_jsonl(path: str, payload: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        tmp = path + ".append.tmp"
        # Read existing content, append new line, atomic replace
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                existing = f.read()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(existing)
            f.write(line)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _load_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        if not os.path.exists(path):
            return out
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = (line or "").strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        out.append(obj)
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _write_jsonl(path: str, rows: list[dict]) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                if isinstance(r, dict):
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _enqueue_summary_retry(entry: dict) -> bool:
    payload = dict(entry or {})
    payload.setdefault("queued_at", datetime.now().isoformat())
    payload.setdefault("attempts", 0)
    payload.setdefault("next_retry_epoch", 0)
    ok = _append_jsonl(SUMMARY_RETRY_QUEUE_PATH, payload)
    if ok:
        _eventlog(
            "judgment:summary_retry:queued",
            ok=True,
            payload={
                "case_reason": payload.get("case_reason", ""),
                "title": payload.get("title", ""),
                "full_text_path": payload.get("full_text_path", ""),
            },
        )
    return ok


def retry_summary_queue(
    max_items: int = 3,
    timeout_sec: int = 420,
    notify: bool = False,
    *,
    offpeak_only: bool = False,
    auto_tiered: bool = True,
) -> dict:
    rows = _load_jsonl(SUMMARY_RETRY_QUEUE_PATH)
    if not rows:
        return {"success": True, "queue_size": 0, "processed": 0, "improved": 0, "remaining": 0, "offpeak": _is_offpeak_now()}

    # ── oMLX health gate: skip entire queue if inference is down ──
    if not _omlx_inference_ok(timeout=15):
        logger.warning("oMLX inference unhealthy — deferring retry queue (%d items)", len(rows))
        return {
            "success": True,
            "queue_size": len(rows),
            "processed": 0,
            "improved": 0,
            "remaining": len(rows),
            "offpeak": _is_offpeak_now(),
            "skipped_reason": "omlx_unhealthy",
        }

    offpeak = _is_offpeak_now()
    if offpeak_only and (not offpeak):
        return {
            "success": True,
            "queue_size": len(rows),
            "processed": 0,
            "improved": 0,
            "remaining": len(rows),
            "offpeak": False,
            "skipped_reason": "not_offpeak",
        }

    if auto_tiered:
        if offpeak:
            max_items = int(_env("JUDGMENT_SUMMARY_RETRY_NIGHT_MAX_ITEMS", str(max_items)) or str(max_items))
            timeout_sec = int(_env("JUDGMENT_SUMMARY_RETRY_NIGHT_TIMEOUT_SEC", str(timeout_sec)) or str(timeout_sec))
        else:
            max_items = int(_env("JUDGMENT_SUMMARY_RETRY_DAY_MAX_ITEMS", "1") or "1")
            timeout_sec = int(_env("JUDGMENT_SUMMARY_RETRY_DAY_TIMEOUT_SEC", "240") or "240")

    processed = 0
    improved = 0
    db_updates = 0
    deferred_to_offpeak = 0
    remain: list[dict] = []
    now_epoch = float(time.time())
    retry_reports: list[str] = []
    queue_hard_timeout = int(_env("JUDGMENT_SUMMARY_RETRY_HARD_TIMEOUT_SEC", "1800") or "1800")
    queue_started = time.monotonic()
    conn = _get_db() if SUMMARY_RETRY_DB_UPDATE else None

    for row in rows:
        if not isinstance(row, dict):
            continue
        if queue_hard_timeout > 0 and (time.monotonic() - queue_started) >= float(queue_hard_timeout):
            remain.append(row)
            continue
        if processed >= int(max_items):
            remain.append(row)
            continue
        if float(row.get("next_retry_epoch", 0) or 0) > now_epoch:
            remain.append(row)
            continue
        attempts = int(row.get("attempts", 0) or 0)
        if attempts >= SUMMARY_RETRY_MAX_ATTEMPTS:
            continue
        tier = _retry_tier(attempts)
        if auto_tiered and (not offpeak) and tier in {"standard", "deep"}:
            row["next_retry_epoch"] = _next_offpeak_epoch(now_epoch)
            row["last_error"] = "deferred_to_offpeak"
            remain.append(row)
            deferred_to_offpeak += 1
            continue

        processed += 1
        full_text_path = str(row.get("full_text_path") or "").strip()
        case_reason = str(row.get("case_reason") or "").strip()
        title = str(row.get("title") or "").strip()
        if (not full_text_path) or (not os.path.exists(full_text_path)):
            row["attempts"] = attempts + 1
            row["next_retry_epoch"] = now_epoch + 3600
            row["last_error"] = "missing_full_text"
            if row["attempts"] < SUMMARY_RETRY_MAX_ATTEMPTS:
                remain.append(row)
            continue

        try:
            with open(full_text_path, "r", encoding="utf-8", errors="replace") as f:
                full_text = f.read()
        except Exception:
            full_text = ""
        if not full_text:
            row["attempts"] = attempts + 1
            row["next_retry_epoch"] = now_epoch + 3600
            row["last_error"] = "empty_full_text"
            if row["attempts"] < SUMMARY_RETRY_MAX_ATTEMPTS:
                remain.append(row)
            continue

        per_item_timeout = _timeout_for_tier(tier, offpeak=offpeak) if auto_tiered else int(timeout_sec)
        summary = _summarize_judgment(full_text, case_reason, timeout_sec=max(90, int(per_item_timeout)))
        meta = _get_last_summary_meta()
        is_degraded = bool(meta.get("is_degraded", _is_degraded_summary(summary)) or _is_degraded_summary(summary))
        if summary and (not is_degraded):
            improved += 1
            retry_reports.append(f"- {title[:50]} ✅")
            if conn:
                db_updates += int(
                    _update_summary_by_text_path(
                        conn,
                        full_text_path=full_text_path,
                        summary_text=summary,
                        title=title,
                        case_reason=case_reason,
                        is_degraded=False,
                    )
                )
            _eventlog(
                "judgment:summary_retry:improved",
                ok=True,
                payload={
                    "case_reason": case_reason,
                    "title": title,
                    "attempts": attempts + 1,
                    "tier": tier,
                    "offpeak": bool(offpeak),
                },
            )
        else:
            row["attempts"] = attempts + 1
            row["last_error"] = str(meta.get("error") or "still_degraded")
            if auto_tiered and (not offpeak):
                # 高峰時段把失敗重試推到下一個離峰窗，降低對主流程與模型服務的競爭。
                row["next_retry_epoch"] = _next_offpeak_epoch(now_epoch)
            else:
                backoff_sec = min(12 * 3600, max(600, 600 * (2 ** min(5, row["attempts"]))))
                row["next_retry_epoch"] = now_epoch + float(backoff_sec)
            if row["attempts"] < SUMMARY_RETRY_MAX_ATTEMPTS:
                remain.append(row)

    _write_jsonl(SUMMARY_RETRY_QUEUE_PATH, remain)
    if conn:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2456, exc_info=True)

    if notify and processed > 0:
        _notify(
            "📚 判決摘要重試完成\n"
            f"處理: {processed}\n"
            f"改善: {improved}\n"
            f"剩餘: {len(remain)}\n"
            + ("\n".join(retry_reports[:8]) if retry_reports else ""),
            True,
        )
    return {
        "success": True,
        "queue_size": len(rows),
        "processed": processed,
        "improved": improved,
        "remaining": len(remain),
        "db_updates": db_updates,
        "deferred_to_offpeak": deferred_to_offpeak,
        "offpeak": bool(offpeak),
        "mode": "tiered" if auto_tiered else "basic",
    }


def retry_summary_queue_auto(notify: bool = False) -> dict:
    """
    自動模式：
    - 高峰時段：只跑少量 fast 重試，standard/deep 直接延到離峰。
    - 離峰時段：跑較大批次並提高 timeout。
    """
    return retry_summary_queue(
        max_items=int(_env("JUDGMENT_SUMMARY_RETRY_MAX_ITEMS", "3") or "3"),
        timeout_sec=int(_env("JUDGMENT_SUMMARY_RETRY_TIMEOUT_SEC", "420") or "420"),
        notify=notify,
        offpeak_only=False,
        auto_tiered=True,
    )


def resummary_all(
    *,
    batch_size: int = 20,
    timeout_sec: int = 420,
    dry_run: bool = False,
    notify: bool = True,
    case_reason_filter: str = "",
) -> dict:
    """
    從 court_judgments 讀取所有有全文的判決，用改善後的 prompt 重新摘要。

    Parameters
    ----------
    batch_size : int
        每次執行處理幾筆（避免長時間佔用 oMLX）。
    timeout_sec : int
        每筆摘要的 timeout。
    dry_run : bool
        True 時只列出待處理筆數，不實際執行。
    notify : bool
        完成後是否發通知。
    case_reason_filter : str
        若指定，只重新摘要該案由的判決。

    Returns
    -------
    dict with keys: success, processed, improved, failed, skipped, total
    """
    conn = _get_db()
    if not conn:
        return {"success": False, "error": "db_not_connected"}

    try:
        cur = conn.cursor(dictionary=True)
        sql = (
            "SELECT jid, court_name, case_number, case_type, judgment_date, "
            "summary, full_text, source_url "
            "FROM court_judgments "
            "WHERE full_text IS NOT NULL AND CHAR_LENGTH(full_text) > 200"
            # 排除已有良好摘要（含「實務見解」且無 WFGY 殘留）的判決
            " AND (summary IS NULL OR summary NOT LIKE '%%實務見解%%'"
            "      OR summary LIKE '%%[2]%%' OR summary LIKE '%%[4]%%')"
            # 排除低價值程序性文書（最高/高等法院除外）
            " AND case_number IS NOT NULL"
            " AND (jid LIKE 'TPS%%' OR jid LIKE 'TPH%%'"
            "      OR case_number NOT REGEXP '司促字|促字第|司票字|票字第|補字第|附民字|續收字|司催字|司消債核字|司執字|司繼字|司聲字|全字第|暫字第|拍字第|司拍字')"
        )
        params: list = []
        if case_reason_filter:
            sql += " AND case_type LIKE %s"
            params.append(f"%{case_reason_filter}%")
        sql += " ORDER BY crawled_at ASC"
        if batch_size > 0:
            sql += " LIMIT %s"
            params.append(batch_size)
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
        cur.close()
    except Exception as e:
        conn.close()
        return {"success": False, "error": f"query_failed: {str(e)[:200]}"}

    total = len(rows)
    if dry_run:
        conn.close()
        return {"success": True, "dry_run": True, "total": total}

    processed = 0
    improved = 0
    failed = 0
    skipped = 0

    for row in rows:
        jid = row.get("jid") or ""
        full_text = row.get("full_text") or ""
        old_summary = row.get("summary") or ""
        case_type = row.get("case_type") or ""

        if len(full_text) < 200:
            skipped += 1
            continue

        # 已有良好摘要（含「## 實務見解」且無 WFGY 殘留）的跳過
        if old_summary and re.search(r"##\s*實務見解", old_summary) and not re.search(r"\[\d\]\s", old_summary):
            logger.info(f"[resummary] {jid} already has good summary, skipping")
            skipped += 1
            continue

        logger.info(f"[resummary] Processing {jid} ({case_type})...")

        try:
            new_summary = _summarize_judgment(full_text, case_type, timeout_sec=timeout_sec)
        except Exception as e:
            logger.warning(f"[resummary] {jid} exception: {e}")
            failed += 1
            continue

        if not new_summary or _is_degraded_summary(new_summary, case_type):
            logger.info(f"[resummary] {jid} new summary degraded, keeping old")
            failed += 1
            continue

        # 新摘要品質檢查：有實務見解標題、引用裁判字號、或包含法院見解標記
        has_opinion = bool(
            re.search(r"(?:##\s*)?實務見解", new_summary)
            or _SUPREME_COURT_CITATION.search(new_summary)
            or re.search(r"(?:本院[按認]|惟查|經查|按[，,])", new_summary)
        )
        if not has_opinion and new_summary.strip() != "本判決無可擷取之實務見解":
            logger.info(f"[resummary] {jid} new summary lacks opinion markers, keeping old")
            failed += 1
            continue

        # 更新 DB
        try:
            up_cur = conn.cursor()
            up_cur.execute(
                "UPDATE court_judgments SET summary=%s, crawled_at=CURRENT_TIMESTAMP WHERE jid=%s",
                (new_summary, jid),
            )
            conn.commit()
            up_cur.close()
            improved += 1
            logger.info(f"[resummary] {jid} updated successfully")
        except Exception as e:
            logger.warning(f"[resummary] {jid} DB update failed: {e}")
            failed += 1

        processed += 1

    conn.close()

    result = {
        "success": True,
        "total": total,
        "processed": processed,
        "improved": improved,
        "failed": failed,
        "skipped": skipped,
    }

    if notify and improved > 0:
        try:
            _notify(
                f"[judgment-collector] resummary 完成：{improved}/{total} 筆改善",
                topic_key="judgment_resummary",
            )
        except Exception:
            pass

    logger.info(f"[resummary] Done: {result}")
    return result


def _sanitize_summary(text: str) -> str:
    """Post-process LLM output: strip prompt leakage, WFGY scaffolding, and garbled prefixes."""
    s = str(text or "").strip()
    if not s:
        return s

    # Fast path: if output contains structured opinion section, truncate prompt echo above it
    opinion_start = re.search(r"##\s*實務見解", s)
    if opinion_start and opinion_start.start() > 100:
        # There's a lot of text before the actual content — likely prompt leakage
        s = s[opinion_start.start():]

    # Strip WFGY reasoning scaffolding — keep only the [7] CONVERGENCE output
    # or anything after the ``` wfgy block ends.
    convergence_match = re.search(
        r"\[7\]\s*(?:CONVERGENCE|收斂|匯流|匯聚)[：:\s]*\n?(.*)",
        s, re.DOTALL | re.IGNORECASE,
    )
    if convergence_match:
        candidate = convergence_match.group(1).strip().rstrip("`").strip()
        # Only use it if it's substantial (not just a one-liner from a bad generation)
        if len(candidate) > 80:
            s = candidate

    # Strip prompt leakage lines (system instructions that leaked into output)
    _leak_patterns = [
        r"^.*你是資深法律研究助理.*$",
        r"^.*你是一位精確的法律助理.*$",
        r"^.*作為\s*MAGI\s*系統的.*AI\s*助理.*$",
        r"^.*我已理解您的(?:需求|要求|指示).*$",
        r"^.*我將(?:會|立即|為您).*$",
        r"^.*請您提供.*判決書.*$",
        r"^.*請提供.*判決書.*$",
        r"^.*請.*貼上判決書.*$",
        r"^.*判決書貼於下方.*$",
        r"^.*專精司法見解分析.*$",
        r"^.*【摘要格式要求】.*$",
        r"^.*【注意事項】.*$",
        r"^.*【法院見解辨識規則】.*$",
        r"^.*【嚴格規則】.*$",
        r"^.*【格式化輸出】.*$",
        r"^.*【精準擷取】.*$",
        r"^.*【逐字複製】.*$",
        r"^.*【禁止】.*$",
        r"^.*必須忠實於判決原文，不得臆測.*$",
        r"^.*裁判案由必須與上方.*$",
        r"^.*若判決內文與案由明顯不符.*$",
        r"^.*法院見解通常出現在以下標記之後.*$",
        r"^\s*-\s*「本院[按判認審].*$",
        r"^\s*-\s*「惟查」「經查」.*$",
        r"^\s*-\s*段首「按.*$",
        r"^.*這些段落中若引用最高法院裁判字號.*$",
        r"^.*該段落極可能包含重要實務見解.*$",
        r"^.*嚴禁摘要、改寫、精煉.*$",
        r"^.*禁止輸出案件概要、事實摘要.*$",
        r"^.*EXECUTE WFGY PROTOCOL.*$",
        r"^.*BEGIN.*7-STEP.*$",
        r"^.*BBMC.*Residue Cleanup.*$",
        r"^.*Semantic Definition.*$",
        r"^.*Delta S.*Semantic Distance.*$",
        r"^.*Controlled Progression.*$",
        r"^.*BBAM.*Attention Balance.*$",
        r"^.*Self-Correction.*Rollback.*$",
        r"^```wfgy\s*$",
        r"^```\s*$",
    ]
    lines = s.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if any(re.match(pat, stripped, re.IGNORECASE) for pat in _leak_patterns):
            continue
        cleaned.append(line)
    s = "\n".join(cleaned).strip()

    # Remove WFGY step labels if present (e.g. "[1] BBMC: ...", "[2] 定義：...")
    s = re.sub(r"^\[(\d)\]\s*(BBMC|Definition|Delta S|Progression|BBAM|Correction|CONVERGENCE)[：:]\s*",
               "", s, flags=re.MULTILINE | re.IGNORECASE)
    # Also strip Chinese WFGY step labels (e.g. "[2] 核心法律問題：", "[4] 進度：", "[6] 修正：")
    s = re.sub(
        r"^\[(\d)\]\s*(?:核心法律問題|定義|說明|進度|進展|推進|修正|匯聯|匯流|收斂)[：:]\s*",
        "", s, flags=re.MULTILINE,
    )

    # Remove WFGY-style chain-of-thought reasoning that TAIDE sometimes generates
    # Patterns: "辨識到潛在混淆", "釐清", "核心語意", "錨定", "Low -", "步驟一"
    _cot_patterns = re.compile(
        r"^[-\s]*(?:辨識到潛在混淆|釐清|核心語意|錨定|聚焦|檢視|批判|調整|"
        r"Low\s*-|Medium\s*-|High\s*-|"
        r"步驟[一二三四五六七八九十]|"
        r"理由：(?:從|本案|依|根據)|"
        r"潛在混淆|問題核心).*$",
        re.MULTILINE,
    )
    s = _cot_patterns.sub("", s)
    # Clean up multiple blank lines left after removal
    s = re.sub(r"\n{3,}", "\n\n", s)

    # Remove hallucinated citation "最高法院112年度台上字第1234號" (prompt example leaked into output)
    s = re.sub(r"\n*最高法院112年度台上字第1234號[^\n]*\n*", "\n", s)

    return s.strip()


# ── 法院見解段落預擷取 ──────────────────────────────────────────────

# 法院見解起始標記（paragraph-level markers）
_OPINION_MARKERS = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"(?:[\u3008-\u301b\uff08-\uff09（〔\[【]*\s*本院(?:按|判斷|認[為定]|審酌|查)\s*[\u3008-\u301b\uff09）〕\]】]*)"
    r"|(?:惟查)"
    r"|(?:經查)"
    # 「按」「經查」的各種出現形式：段首、次按、再按、又按、數字標題後
    r"|(?:(?:[一二三四五六七八九十壹貳參肆伍陸柒捌玖拾]+、\s*)?(?:次按|再按|又按|按|經查))"
    r"|(?:本院(?:認為|審酌|查|按))"
    r")",
    re.MULTILINE,
)

# 最高法院裁判字號 pattern（例如：最高法院 112 年度台上字第 1234 號）
_SUPREME_COURT_CITATION = re.compile(
    r"最高(?:法院|行政法院)\s*\d{2,3}\s*年度?\s*[\u4e00-\u9fff]{1,6}\s*字?\s*第?\s*\d{1,7}\s*號"
)


def _extract_court_opinion_sections(full_text: str, *, is_supreme_court: bool = False) -> str:
    """
    從判決全文預擷取法院見解段落。

    規則：
    1. 找到「本院按」「本院判斷」「惟查」「按，」「經查」等標記後的段落
    2. 每個段落從標記開始，到下一個空行或下一個大標題為止
    3. 如果段落內引用了最高法院字號，優先保留
    4. 最高法院判決本身即為權威見解，即使沒有引用其他字號也保留

    回傳預擷取的文字。如果擷取結果太短（<200字），回傳原文讓 LLM 自己判斷。
    """
    if not full_text or len(full_text) < 100:
        return full_text

    sections: list[str] = []
    text = full_text

    # 找到所有標記位置
    matches = list(_OPINION_MARKERS.finditer(text))

    if not matches and not is_supreme_court:
        # 沒找到任何標記，回傳原文
        return full_text

    # 段落結束 pattern：連續空行、或新大標題（一、二、壹、貳、主文、理由、事實）
    _section_end = re.compile(
        r"(?:\n\s*\n)"  # 連續空行
        r"|(?:\n\s*(?:[一二三四五六七八九十壹貳參肆伍陸柒捌玖拾]+、))"  # 中文數字標題
        r"|(?:\n\s*(?:主\s*文|理\s*由|事\s*實|據上論[結斷]))"  # 大標題
    )

    for m in matches:
        start = m.start()
        # 從標記開始，找段落結束
        rest = text[start:]
        end_m = _section_end.search(rest, pos=len(m.group()))
        if end_m:
            section = rest[:end_m.start()].strip()
        else:
            # 沒找到結尾，取到文末但限制長度
            section = rest[:5000].strip()

        if len(section) > 50:  # 過短的段落跳過
            sections.append(section)

    # 最高法院判決：如果沒有透過標記找到足夠內容，
    # 掃描「理由」區段整段保留（最高法院判決本身就是權威見解）
    if is_supreme_court and len("\n".join(sections)) < 300:
        reason_m = re.search(r"理\s*由", text)
        if reason_m:
            reason_start = reason_m.start()
            # 到「據上論結」或文末
            reason_end_m = re.search(r"據上論[結斷]", text[reason_start:])
            if reason_end_m:
                reason_section = text[reason_start:reason_start + reason_end_m.start()].strip()
            else:
                reason_section = text[reason_start:].strip()
            if len(reason_section) > 100:
                sections = [reason_section]

    if not sections:
        return full_text

    extracted = "\n\n---\n\n".join(sections)

    # 擷取結果太短，可能漏抓，回傳原文
    if len(extracted) < 200:
        return full_text

    return extracted


def _detect_supreme_court(full_text: str) -> bool:
    """判斷是否為最高法院/最高行政法院判決。"""
    if not full_text:
        return False
    # 檢查前 500 字是否有最高法院字樣（通常在標題/案號區）
    header = full_text[:500]
    return bool(re.search(r"最高(?:法院|行政法院)", header))


_LOW_VALUE_PATTERNS = re.compile(
    r"支付命令|司促字|司票字|促字第|票字第|"
    r"補費裁定|附帶民事.*移送|附民字|"
    r"續收字|司催字|司消債核字|"
    r"司執字|司繼字|司聲字|全字第|暫字第|拍字第|司拍字",
)

def _summarize_judgment(full_text: str, case_reason: str, timeout_sec: int = 420) -> str:
    if not full_text:
        _set_last_summary_meta(is_degraded=True, route="empty_input", error="missing_full_text")
        return ""

    # 低價值程序性文書：直接標記，不浪費 LLM 推理（最高/高等法院除外）
    header = full_text[:500]
    _is_upper = any(kw in header for kw in ["最高法院", "最高行政法院", "高等法院", "高等行政法院"])
    if (not _is_upper) and _LOW_VALUE_PATTERNS.search(header):
        label = "支付命令" if "支付命令" in header or "促字" in header else \
                "本票裁定" if "票字" in header else \
                "附帶民事移送裁定" if "附民" in header else "程序性文書"
        logger.info(f"Low-value document detected: {label}, skipping LLM")
        return f"## 實務見解\n本件無可擷取之實務見解（{label}，屬程序性文書）。"

    logger.info(f"Summarizing text length: {len(full_text)}")

    # ── 預處理：擷取法院見解段落，減少雜訊 ──
    is_supreme = _detect_supreme_court(full_text)
    extracted = _extract_court_opinion_sections(full_text, is_supreme_court=is_supreme)
    use_extracted = (extracted != full_text)
    if use_extracted:
        logger.info(f"Pre-extracted court opinion sections: {len(extracted)} chars (from {len(full_text)})")

    # 用預擷取的文字做摘要（如果有的話）
    working_text = extracted

    # RAG 防護機制：大於 15000 字的判決書分段處理
    chunk_size = 15000
    chunks = []

    if len(working_text) > chunk_size:
        logger.info(f"Text too long, enabling chunked RAG synthesis...")
        for i in range(0, len(working_text), chunk_size):
            chunks.append(working_text[i:i+chunk_size])
    else:
        chunks = [working_text]

    combined_summaries = []
    rk = "judgment-collector:summarize:" + hashlib.sha256(case_reason.encode()).hexdigest()[:8]
    chunk_slots = min(5, len(chunks))
    chunk_route_buf: list[str] = [""] * chunk_slots
    chunk_deg_buf: list[bool] = [True] * chunk_slots

    def _summarize_one_chunk(idx, chunk):
        _part_hint = f"（第 {idx+1} 部分）" if len(chunks) > 1 else ""
        _supreme_hint = (
            "\n※ 本判決為最高法院裁判，其論述本身即為權威實務見解，"
            "即使未引用其他裁判字號，仍應擷取「理由」中的核心法律論述。\n"
        ) if is_supreme else ""
        prompt_template = (
            "你是一位精確的法律助理。你的唯一任務是從判決書中"
            f"「逐字擷取」可供其他案件參考的「實務見解」或「法律原則」{_part_hint}。\n"
            f"案由：{case_reason}\n"
            f"{_supreme_hint}"
            "判決內文：\n{raw_text}\n\n"
            "【法院見解辨識規則】\n"
            "法院見解通常出現在以下標記之後：\n"
            "  - 「本院按」「本院判斷」「本院認為」「本院審酌」\n"
            "  - 「惟查」「經查」\n"
            "  - 段首「按，...」（後接法律原則論述）\n"
            "這些段落中若引用最高法院裁判字號，"
            "該段落極可能包含重要實務見解，應優先擷取。\n\n"
            "【嚴格規則】\n"
            "1. 【精準擷取】：從上述標記後的段落中，找出引用最高法院字號或闡述法律原則的段落，"
            "擷取一到三個最具參考價值的段落。\n"
            "2. 【逐字複製】：必須逐字(verbatim)複製找到的段落，包含引用的裁判字號。\n"
            "3. 【禁止】：嚴禁摘要、改寫、精煉或加入自己的文字。\n"
            "4. 【禁止】：禁止輸出案件概要、事實摘要、判決結果、當事人主張等敘述。\n\n"
            "【格式化輸出】\n"
            "## 實務見解\n（從判決中逐字擷取的法院見解原文，保留完整裁判字號引用）\n\n"
            "## 引用裁判\n（列出判決內文中實際出現的最高法院裁判字號，禁止自行編造字號，若內文無引用則省略本節）\n\n"
            "## 適用法條\n（列出適用法條）\n\n"
            "【注意事項】\n"
            "- 若找不到有法律原則價值的見解，回覆「本判決無可擷取之實務見解」\n"
            "- 若判決內文與案由明顯不符，回覆「案由不符，無法擷取」\n"
        )
        # 摘要任務不使用 WFGY 推理包裝（逐字擷取不需要多步推理，WFGY 會浪費 token 且污染輸出）
        filled_prompt = prompt_template.replace("{raw_text}", chunk)
        payload = {
            "raw_text": chunk,
            "case_reason_context": case_reason,
            "prompt_template": filled_prompt,
        }
        result = _run_skill(
            "insight-refine",
            _skill_json_task("refine", payload),
            timeout_sec=int(timeout_sec),
            route_key=rk + f"_{idx}",
        )
        parsed = _parse_skill_output(result)
        if parsed.get("success"):
            text = _sanitize_summary((parsed.get("refined_text") or parsed.get("text") or parsed.get("output") or "").strip())
            if text:
                return idx, text, None, False, "skill_insight_refine"

        chunk_prompt = filled_prompt
        skill_err = parsed.get("error") or "insight_refine_failed"

        # ── Codex OAuth 優先：遠端 API 快且不佔本機 oMLX 資源 ──
        try:
            from skills.bridge.openclaw_codex_bridge import (
                feature_enabled as _codex_feat,
                run_prompt as _codex_run,
            )
            if _codex_feat("summary"):
                codex_r = _codex_run(
                    feature="summary",
                    prompt=chunk_prompt,
                    timeout_sec=max(60, int(timeout_sec)),
                )
                codex_text = _sanitize_summary(str(codex_r.get("text") or "").strip())
                if codex_r.get("success") and codex_text:
                    logger.info(f"Chunk {idx+1} summarized via Codex OAuth")
                    # ── 知識蒸餾：收集高品質 Codex 訓練資料 ──
                    try:
                        from skills.bridge.distill_collector import collect_summary_pair
                        collect_summary_pair(chunk_prompt, codex_text, case_reason, "openclaw_codex")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2637, exc_info=True)  # 收集失敗不影響主流程
                    return idx, codex_text, None, False, "openclaw_codex"
                skill_err += f" | codex:{codex_r.get('error', 'empty')}"
        except Exception as codex_exc:
            skill_err += f" | codex_exc:{codex_exc}"

        # ── NIM fallback：Codex 失敗時走免費 NVIDIA NIM（70B 默認，>20K 字自動 405B）──
        # nim_heavy.run_nim_chat 內建 semaphore (NVIDIA_NIM_MAX_CONCURRENT, 預設 3)、
        # daily budget (NVIDIA_NIM_DAILY_BUDGET, 預設 500)、circuit breaker、PII scrub。
        # 由 NVIDIA_NIM_ENABLE=1 開關；JUDGMENT_NIM_INGEST=1 額外控制 ingestion 是否走 NIM。
        if _env("NVIDIA_NIM_ENABLE", "0") in ("1", "true", "True", "yes", "YES") and \
           _env("JUDGMENT_NIM_INGEST", "1") in ("1", "true", "True", "yes", "YES"):
            try:
                from skills.bridge.nim_heavy import run_nim_chat
                nim_r = run_nim_chat(
                    prompt=chunk_prompt,
                    timeout_sec=max(60, int(timeout_sec)),
                    task_type="summary",
                    require_pii_scrub=_env("NVIDIA_NIM_REQUIRE_PII_SCRUB", "1") in ("1", "true", "True"),
                )
                if nim_r.get("success"):
                    nim_text = _sanitize_summary(str(nim_r.get("response") or "").strip())
                    if nim_text:
                        logger.info(f"Chunk {idx+1} summarized via NIM ({nim_r.get('model', '')})")
                        try:
                            from skills.bridge.distill_collector import collect_summary_pair
                            collect_summary_pair(chunk_prompt, nim_text, case_reason, "nvidia_nim_ingest")
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch nim distill", exc_info=True)
                        return idx, nim_text, None, False, "nvidia_nim"
                skill_err += f" | nim:{nim_r.get('error', 'empty')}"
            except Exception as nim_exc:
                skill_err += f" | nim_exc:{nim_exc}"

        # ── oMLX fallback：Codex/NIM 都不可用時走本機推理 ──
        gateway = _get_inference_gateway()
        if gateway:
            g = gateway.dispatch(
                chunk_prompt,
                task_type="summary",
                timeout=max(60, int(timeout_sec)),
                force_quality=_env("JUDGMENT_SUMMARY_FORCE_QUALITY", "0") in ("1", "true", "True", "yes", "YES"),
            )
            if g.get("success"):
                g_text = _sanitize_summary((g.get("response") or g.get("summary") or g.get("text") or "").strip())
                if g_text:
                    g_deg = bool(g.get("degraded", False) or _is_degraded_summary(g_text))
                    return idx, g_text, None, g_deg, str(g.get("route") or "gateway_dispatch")

            g_err = g.get("error", "") if isinstance(g, dict) else ""
            return idx, None, f"{skill_err} | dispatch:{g_err}", True, "failed"

        return idx, None, skill_err, True, "failed"

    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
    combined_buf = [None] * chunk_slots
    _chunk_deadline = max(120, int(timeout_sec) * 2)
    # max_workers=1: oMLX max-num-seqs=1, parallel requests just queue and risk timeout cascade
    with ThreadPoolExecutor(max_workers=1) as executor:
        fut_map = {executor.submit(_summarize_one_chunk, i, chunks[i]): i for i in range(chunk_slots)}
        try:
            for f in as_completed(fut_map, timeout=_chunk_deadline):
                i = fut_map[f]
                try:
                    _, text, err, is_deg, route = f.result()
                    if text:
                        combined_buf[i] = text
                        chunk_deg_buf[i] = bool(is_deg)
                        chunk_route_buf[i] = str(route or "unknown")
                    else:
                        logger.warning(f"Chunk {i+1} summary failed: {err}")
                        chunk_deg_buf[i] = True
                        chunk_route_buf[i] = str(route or "failed")
                except Exception as e:
                    logger.warning(f"Chunk {i+1} summary exception: {e}")
                    chunk_deg_buf[i] = True
                    chunk_route_buf[i] = "exception"
        except FuturesTimeoutError:
            logger.warning("Chunk summarization timed out after %ds, using partial results", _chunk_deadline)
            for f_pending in fut_map:
                if not f_pending.done():
                    f_pending.cancel()
                    i = fut_map[f_pending]
                    chunk_deg_buf[i] = True
                    chunk_route_buf[i] = "timeout"
    combined_summaries = [t for t in combined_buf if t]
            
    if not combined_summaries:
        lines = full_text.strip().split("\n")
        preview = "\n".join(lines[:20])
        _set_last_summary_meta(is_degraded=True, route="preview_fallback", error="all_chunks_failed")
        return "(摘要失敗，前 20 行預覽)\n" + preview

    if len(combined_summaries) == 1:
        one = combined_summaries[0]
        is_deg = bool(any(chunk_deg_buf) or _is_degraded_summary(one))
        route = next((r for r in chunk_route_buf if r), "single_chunk")
        _set_last_summary_meta(is_degraded=is_deg, route=route)
        return one
        
    # 如果有多段，進行最終收斂 (Reduce)
    logger.info("Reducing chunked summaries into final conclusion...")
    reduce_prompt = (
        "你是一位精確的法律助理。以下是同一份長篇判決書各段擷取的實務見解。\n"
        "案由：" + case_reason + "\n"
        "各段擷取：\n{raw_text}\n\n"
        "請從上述擷取中，挑選最具法律原則價值的一到三個段落，"
        "優先保留引用最高法院裁判字號的段落。"
        "合併成最終輸出。必須保持原文逐字不變，只做去重和排序。\n\n"
        "## 實務見解\n（從判決中逐字擷取的法院見解原文，保留完整裁判字號引用）\n\n"
        "## 引用裁判\n（列出判決內文中實際出現的最高法院裁判字號，禁止自行編造字號，若內文無引用則省略本節）\n\n"
        "## 適用法條\n（列出適用法條）\n"
    )
    
    reduce_raw_text = "\n### 分段 ###\n".join(combined_summaries)
    filled_reduce_prompt = reduce_prompt.replace("{raw_text}", reduce_raw_text)
    payload_reduce = {
        "raw_text": reduce_raw_text,
        "case_reason_context": case_reason,
        "prompt_template": filled_reduce_prompt,
    }

    final_res = _run_skill(
        "insight-refine",
        _skill_json_task("refine", payload_reduce),
        timeout_sec=int(timeout_sec),
        route_key=rk + "_final",
    )
    parsed_final = _parse_skill_output(final_res)
    if parsed_final.get("success"):
        final_text = _sanitize_summary((parsed_final.get("refined_text") or parsed_final.get("text") or parsed_final.get("output") or "").strip())
        if final_text:
            is_deg = bool(any(chunk_deg_buf) or _is_degraded_summary(final_text))
            _set_last_summary_meta(is_degraded=is_deg, route="skill_insight_refine_reduce")
            return final_text

    gateway = _get_inference_gateway()
    if gateway:
        reduce_direct_prompt = filled_reduce_prompt
        g_final = gateway.dispatch(
            reduce_direct_prompt,
            task_type="summary",
            timeout=max(60, int(timeout_sec)),
            force_quality=_env("JUDGMENT_SUMMARY_FORCE_QUALITY", "0") in ("1", "true", "True", "yes", "YES"),
        )
        if g_final.get("success"):
            final_text = _sanitize_summary((g_final.get("response") or g_final.get("summary") or g_final.get("text") or "").strip())
            if final_text:
                is_deg = bool(any(chunk_deg_buf) or g_final.get("degraded", False) or _is_degraded_summary(final_text))
                _set_last_summary_meta(
                    is_degraded=is_deg,
                    route=str(g_final.get("route") or "gateway_chat_reduce"),
                    error=(parsed_final.get("error") or ""),
                )
                return final_text

    fallback = "\n\n(系統提示：最終整合失敗，以下為分段重點紀錄)\n" + "\n---\n".join(combined_summaries)
    _set_last_summary_meta(is_degraded=True, route="chunk_concat_fallback", error=(parsed_final.get("error") or "reduce_failed"))
    return fallback



def _normalize_title_for_match(title: str) -> str:
    s = str(title or "").strip()
    if not s:
        return ""
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[，。、「」『』【】《》:：;；,./\\|_+\-\(\)\[\]{}·]", "", s)
    return s.lower()


def _extract_case_markers(text: str) -> set[str]:
    """
    從標題中抽取案號 marker，用於外部來源與司法院結果對齊。
    """
    s = str(text or "")
    if not s:
        return set()
    pats = [
        r"\d{1,3}\s*台\s*上\s*字?\s*第?\s*\d{1,7}\s*號?",
        r"\d{2,3}\s*年度\s*[\u4e00-\u9fff]{1,10}\s*字\s*第?\s*\d{1,8}\s*號?",
        r"\d{2,3}\s*年度\s*[\u4e00-\u9fff]{1,10}\s*第?\s*\d{1,8}\s*號?",
    ]
    out: set[str] = set()
    for p in pats:
        for m in re.findall(p, s):
            t = re.sub(r"\s+", "", m or "")
            t = t.replace("第", "").replace("號", "")
            if t:
                out.add(t)
    return out


def _build_judicial_fulltext_index(
    *,
    case_reason: str,
    case_type: str,
    query_hint: str = "",
    max_results: int,
    max_chars: int,
    headless: bool,
    timeout_sec: int,
    route_key: str,
) -> dict:
    """
    建立司法院全文候選索引，供外部來源無全文時逐筆回補。
    """
    query = str(query_hint or "").strip() or str(case_reason or "").strip()
    if not query:
        return {"success": False, "error": "missing_case_reason"}
    if ("行政" in str(case_type or "")) and ("行政" not in query):
        query = f"{query} 行政"

    payload = {
        "query": query,
        "max_results": max(20, min(int(JUDGMENT_JY_FILL_MAX_RESULTS), int(max_results))),
        "max_chars": int(max_chars if int(max_chars) > 0 else DEFAULT_MAX_CHARS),
        "headless": bool(headless),
        "timeout_sec": min(int(timeout_sec), 120),
    }
    # skill_timeout = payload timeout + small buffer
    skill_timeout = int(timeout_sec) + 10
    sr = _run_skill(
        "judicial-flow-search-archive",
        _skill_json_task("search_archive", payload),
        timeout_sec=skill_timeout,
        route_key=route_key + ":jyfill",
    )
    sp = _parse_skill_output(sr)
    if not sp.get("success"):
        return {"success": False, "error": sp.get("error") or "judicial_search_failed"}

    manifest_path = (sp.get("manifest_path") or "").strip()
    items = []
    if manifest_path and os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                mf = json.load(f) or {}
            items = mf.get("items") or []
        except Exception:
            items = []
    if not items:
        items = sp.get("items_preview") or []

    candidates = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        text_path = str(it.get("archived_text_path") or it.get("text_path") or "").strip()
        if not title or not text_path or (not os.path.exists(text_path)):
            continue
        candidates.append(
            {
                "title": title,
                "title_norm": _normalize_title_for_match(title),
                "markers": _extract_case_markers(title),
                "text_path": text_path,
                "url": str(it.get("url") or "").strip(),
                "size": int(os.path.getsize(text_path) if os.path.exists(text_path) else 0),
            }
        )

    return {
        "success": True,
        "query": query,
        "count": len(candidates),
        "manifest_path": manifest_path,
        "candidates": candidates,
    }


def _pick_best_judicial_text_for_title(title: str, index_obj: dict) -> dict:
    if not isinstance(index_obj, dict) or (not index_obj.get("success")):
        return {}
    cands = index_obj.get("candidates") or []
    if not cands:
        return {}
    t = str(title or "").strip()
    if not t:
        return {}
    t_norm = _normalize_title_for_match(t)
    t_markers = _extract_case_markers(t)

    best = None
    best_score = -1.0
    for c in cands:
        c_markers = c.get("markers") or set()
        score = 0.0
        if t_markers and c_markers:
            inter = t_markers.intersection(c_markers)
            if inter:
                score += 5.0 + (max(len(x) for x in inter) / 20.0)
        c_norm = str(c.get("title_norm") or "")
        if t_norm and c_norm:
            ratio = difflib.SequenceMatcher(None, t_norm, c_norm).ratio()
            score += ratio * 2.5
            # 鼓勵同前綴（通常是法院+案號一致）
            if t_norm[:12] and c_norm.startswith(t_norm[:12]):
                score += 0.8
        score += min(float(c.get("size") or 0) / 200000.0, 0.8)
        if score > best_score:
            best_score = score
            best = c

    if (not best) or best_score < 1.2:
        return {}
    out = dict(best)
    out["match_score"] = round(best_score, 4)
    return out


# ---------------------------------------------------------------------------
# Core: collect
# ---------------------------------------------------------------------------
def collect(
    case_number: str = "",
    case_reason: str = "",
    case_type: str = "",
    max_results: int = DEFAULT_MAX_RESULTS,
    max_chars: int = DEFAULT_MAX_CHARS,
    headless: bool = True,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    save_to_db: bool = True,
    notify: bool = True,
) -> dict:
    conn = None
    folder_path = ""
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    key = hashlib.sha256((case_reason + now).encode()).hexdigest()[:10]
    search_source = "unknown"

    # 0) Housekeeping: prune old cache runs
    _pruned = _cleanup_old_cache_runs()
    if _pruned:
        logger.info("Cache housekeeping: removed %d old run directories", _pruned)

    # 1) Resolve case info
    if case_number and not case_reason:
        conn = _get_db()
        if conn:
            _ensure_table(conn)
            _ensure_court_judgments_table(conn)
            info = _lookup_case(conn, case_number)
            case_reason = info.get("case_reason", "")
            case_type = case_type or info.get("case_type", "")
            folder_path = translate_case_path_to_local(info.get("folder_path", ""))

    if not case_reason:
        return {"success": False, "error": "missing case_reason (無法取得案由)"}

    # Validate case_reason: must be a plausible legal term (≥2 chars, no conversational fragments)
    _cr = case_reason.strip()
    _INVALID_REASON_PATTERNS = re.compile(
        r"^(查一下|幫我|請問|你好|hi|hello|test|測試|看看|找一下)",
        re.IGNORECASE,
    )
    if len(_cr) < 2 or _INVALID_REASON_PATTERNS.match(_cr):
        return {"success": False, "error": f"case_reason 不合法：'{case_reason}'（需為有效案由）"}

    logger.info("Collecting judgments: reason=%s type=%s", case_reason, case_type)

    # 2) Determine courts
    courts_display = _get_court_display(case_type, case_reason)
    logger.info("  Courts: %s", courts_display)

    # Key should be based on final case_reason after resolve.
    key = hashlib.sha256((case_reason + now).encode()).hexdigest()[:10]

    # 3) Search via Judicial Yuan Archive
    items = []
    boolean_query = case_reason
    archive_dir = ""
    manifest_path = ""

    logger.info("  Searching Judicial Yuan Archive...")
    rk = "judgment-collector:" + key

    search_payload = {
        "query": case_reason,
        "max_results": int(max_results),
        "max_chars": int(max_chars if int(max_chars) > 0 else DEFAULT_MAX_CHARS),
        "headless": bool(headless),
        "timeout_sec": min(180, int(timeout_sec)),
    }
    sr = _run_skill(
        "judicial-flow-search-archive",
        _skill_json_task("search_archive", search_payload),
        timeout_sec=int(timeout_sec) + 60,
        route_key=rk,
    )
    sp = _parse_skill_output(sr)
    if not sp.get("success"):
        msg = "judgment search failed (Judicial Yuan): " + (sp.get("error") or "unknown")
        _notify(msg, notify)
        return {"success": False, "error": msg, "detail": sp}

    archive_dir = (sp.get("archive_dir") or "").strip()
    manifest_path = (sp.get("manifest_path") or "").strip()

    # Load full manifest for text paths
    manifest = {}
    if manifest_path and os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3018, exc_info=True)

    items = manifest.get("items") or sp.get("items_preview") or []
    boolean_query = sp.get("boolean_query", "")
    search_source = "judicial_yuan"

    judicial_fill_index = {"success": False, "candidates": []}
    judicial_fallback_hits = 0
    judicial_fallback_single_retry_hits = 0
    judicial_fallback_error = ""
    # JUDGMENT_SKIP_JIRS=1 disables 司法院API fallback (e.g. during planned maintenance)
    _skip_jirs = _env("JUDGMENT_SKIP_JIRS", "0").lower() in {"1", "true", "yes"}
    # Track overall time budget from here (includes fill + per-item phases)
    import time as _time
    _collect_start = _time.monotonic()

    # 4) Summarize each judgment + store
    if not conn and save_to_db:
        conn = _get_db()
        if conn:
            _ensure_table(conn)
            _ensure_court_judgments_table(conn)

    results = []
    db_ids = []
    retry_queued_count = 0
    summary_lines = [
        "# 判決收集報告",
        "",
        "- **案由**: " + case_reason,
        "- **案件類型**: " + (case_type or "(未指定)"),
        "- **法院**: " + courts_display,
        "- **搜尋用語**: `" + boolean_query + "`",
        "- **收集時間**: " + datetime.now().strftime("%Y-%m-%d %H:%M"),
        "- **案件編號**: " + (case_number or "(未指定)"),
        "",
        "---",
        "",
    ]

    for idx, item in enumerate(items, start=1):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        ok = item.get("success", False)
        text_path = (item.get("archived_text_path") or item.get("text_path") or "").strip()

        if not ok or not title:
            results.append({"idx": idx, "title": title, "success": False})
            continue

        # Read full text
        full_text = ""
        if text_path and os.path.exists(text_path):
            try:
                with open(text_path, "r", encoding="utf-8", errors="replace") as f:
                    full_text = f.read()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3075, exc_info=True)
        private_source_key = (_env("MAGI_PRIVATE_LEGAL_SOURCE_KEY", "") or "").strip().lower()
        uses_private_source = bool(private_source_key and search_source == private_source_key)
        if (not full_text) and uses_private_source and judicial_fill_index.get("success"):
            matched = _pick_best_judicial_text_for_title(title, judicial_fill_index)
            alt_path = str(matched.get("text_path") or "").strip()
            if alt_path and os.path.exists(alt_path):
                try:
                    with open(alt_path, "r", encoding="utf-8", errors="replace") as f:
                        full_text = f.read()
                    if full_text:
                        text_path = alt_path
                        judicial_fallback_hits += 1
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3087, exc_info=True)
        _elapsed = _time.monotonic() - _collect_start
        _remaining = max(0, int(timeout_sec) - _elapsed)
        if (not full_text) and uses_private_source and not _skip_jirs and not _skip_fill and _remaining > 60:
            # 若預先索引沒命中，再以單筆標題/案號做一次小範圍補抓。
            # 避免外部來源標題與案由廣搜結果偏差導致補全文失敗。
            # 只在剩餘時間 > 200s 時嘗試，避免 timeout 級聯。
            try:
                marker_terms = sorted(_extract_case_markers(title), key=lambda x: len(x), reverse=True)
                hint = marker_terms[0] if marker_terms else title
                if hint:
                    _single_timeout = max(30, min(60, int(min(_remaining * 0.5, 60))))
                    single_idx = _build_judicial_fulltext_index(
                        case_reason=case_reason,
                        case_type=case_type,
                        query_hint=hint,
                        max_results=max(20, min(int(JUDGMENT_JY_FILL_MAX_RESULTS), int(max_results) * 8)),
                        max_chars=int(max_chars if int(max_chars) > 0 else DEFAULT_MAX_CHARS),
                        headless=bool(headless),
                        timeout_sec=_single_timeout,
                        route_key=("judgment-collector:" + key + f":single:{idx}"),
                    )
                    if single_idx.get("success"):
                        matched2 = _pick_best_judicial_text_for_title(title, single_idx)
                        alt_path2 = str(matched2.get("text_path") or "").strip()
                        if alt_path2 and os.path.exists(alt_path2):
                            with open(alt_path2, "r", encoding="utf-8", errors="replace") as f:
                                full_text = f.read()
                            if full_text:
                                text_path = alt_path2
                                judicial_fallback_hits += 1
                                judicial_fallback_single_retry_hits += 1
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3120, exc_info=True)

        # Summarize
        summary = ""
        summary_meta = {"is_degraded": True, "route": "unset", "error": ""}
        is_degraded_summary = True
        if full_text:
            logger.info("  Summarizing %d/%d: %s", idx, len(items), title[:40])
            try:
                _env_refine = int(_env("MAGI_JUDGMENT_REFINE_TIMEOUT_SEC", "420") or "420")
            except Exception:
                _env_refine = 420
            # Cap refine timeout by remaining time budget
            _elapsed_now = _time.monotonic() - _collect_start
            _remaining_now = max(10, int(timeout_sec) - _elapsed_now)
            refine_timeout = min(_env_refine, _remaining_now)
            summary = _summarize_judgment(full_text, case_reason, timeout_sec=refine_timeout)
            summary_meta = _get_last_summary_meta()
            is_degraded_summary = bool(
                summary_meta.get("is_degraded", _is_degraded_summary(summary, case_reason))
                or _is_degraded_summary(summary, case_reason)
            )
            if is_degraded_summary and _env("JUDGMENT_SUMMARY_RETRY_ENABLE", "1") in ("1", "true", "True", "yes", "YES"):
                queued = _enqueue_summary_retry(
                    {
                        "case_number": case_number,
                        "case_reason": case_reason,
                        "case_type": case_type,
                        "title": title,
                        "url": url,
                        "full_text_path": text_path,
                        "source": search_source,
                        "reason": str(summary_meta.get("error") or "degraded_summary"),
                    }
                )
                if queued:
                    retry_queued_count += 1

        # Store in DB
        row_id = None
        if save_to_db and conn:
            row_id = _store_judgment(conn, {
                "case_number": case_number,
                "case_reason": case_reason,
                "case_type": case_type,
                "court_level": courts_display,
                "judgment_title": title,
                "judgment_url": url,
                "full_text_path": text_path,
                "summary_text": summary,
                "is_degraded": is_degraded_summary,
                "search_query": boolean_query,
            })
            if row_id:
                db_ids.append(row_id)
            _upsert_court_judgment(
                conn,
                title=title,
                url=url,
                summary="" if is_degraded_summary else summary,
                full_text=full_text,
                case_type=case_type,
            )

        title_tw = _tw(title)
        summary_preview = _tw(summary[:200] if summary else "")

        results.append({
            "idx": idx,
            "title": title_tw,
            "url": url,
            "success": True,
            "summary_preview": summary_preview,
            "summary_full": _tw(summary) if summary else "",
            "db_id": row_id,
            "is_degraded": is_degraded_summary,
            "summary_route": summary_meta.get("route", ""),
        })

        # Add to report
        summary_lines.append("## " + str(idx) + ". " + title_tw)
        summary_lines.append("")
        summary_lines.append("**URL**: " + url)
        if summary:
            summary_lines.append("")
            summary_lines.append(_tw(summary))
        summary_lines.append("")
        summary_lines.append("---")
        summary_lines.append("")

    # 5) Write summary report
    if archive_dir and os.path.isdir(archive_dir):
        summary_path = os.path.join(archive_dir, "summary_report.md")
    else:
        run_dir = os.path.join(CACHE_ROOT, now + "_" + key)
        os.makedirs(run_dir, exist_ok=True)
        summary_path = os.path.join(run_dir, "summary_report.md")

    # --- SAVE TO OSC JUDGMENTS JSON (via shared helper) ---
    _collect_saved = 0
    for r in results:
        if not r.get("success") or r.get("is_degraded"):
            continue
        full = str(r.get("summary_full") or r.get("summary_preview") or "").strip()
        if not full or len(full) < 30:
            continue
        _src = "Judicial Yuan"
        if _upsert_judgments_json(
            title=_tw(r.get("title", "")),
            summary=full,
            case_reason=_tw(case_reason),
            url=r.get("url", ""),
            source=_src,
        ):
            _collect_saved += 1
    logger.info("Saved %d/%d judgments to judgments.json", _collect_saved, len(results))
    # ----------------------------------

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))

    # Copy to case folder if available
    if folder_path and os.path.isdir(folder_path):
        try:
            import shutil
            dest = os.path.join(folder_path, "判決收集_" + case_reason + "_" + now[:8] + ".md")
            shutil.copy2(summary_path, dest)
            logger.info("  Copied report to case folder: %s", dest)
        except Exception as e:
            logger.warning("Failed to copy to case folder: %s", e)

    # 5c) Ingest related statutes into vector DB (best-effort)
    try:
        svdb_action = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "statutes-vdb", "action.py")
        if os.path.exists(svdb_action) and (case_number or folder_path):
            svdb_cases = [{"case_number": case_number or "", "case_path": folder_path or ""}]
            svdb_payload = json.dumps({"cases": svdb_cases}, ensure_ascii=False)
            venv_py = str(get_skill_python())
            if os.path.exists(venv_py):
                subprocess.run(
                    [venv_py, svdb_action, "--task", "update_cases " + svdb_payload],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=120,
                )
                logger.info("  Statutes VDB updated for case %s", case_number or case_reason)
    except Exception as e:
        logger.warning("Statutes VDB update skipped: %s", e)


    # 6) Notify
    ok_count = len([r for r in results if r.get("success")])
    notify_text = (
        "📚 判決收集完成 — " + case_reason + "\n"
            "法院: " + courts_display + "\n"
            "收集筆數: " + str(ok_count) + "/" + str(len(items)) + "\n"
            "報告: " + summary_path + "\n"
        )
    if db_ids:
        notify_text += "DB IDs: " + str(db_ids) + "\n"
    if results:
        for r in results:
            if r.get("success"):
                notify_text += "  - " + r["title"][:50] + "\n"
    if retry_queued_count > 0:
        notify_text += f"摘要重試佇列: +{retry_queued_count}\n"
    _notify(notify_text, notify)

    if conn:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3291, exc_info=True)

    return {
        "success": True,
        "case_reason": case_reason,
        "case_type": case_type,
        "court_level": courts_display,
        "search_source": search_source,
        "boolean_query": boolean_query,
        "archive_dir": archive_dir,
        "summary_path": summary_path,
        "count": ok_count,
        "retry_queued_count": retry_queued_count,
        "db_ids": db_ids,
        "items": results[: max(10, min(120, int(max_results)))],
    }


# ---------------------------------------------------------------------------
# Official Judicial API (night pull + day process)
# ---------------------------------------------------------------------------
def _iter_jdg_raw_files() -> list[str]:
    out: list[str] = []
    root = Path(JDG_API_RAW_ROOT)
    if not root.exists():
        return out
    for p in root.glob("*/*.json"):
        try:
            if p.is_file():
                out.append(str(p))
        except Exception:
            continue
    out.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return out


def official_api_night_pull(
    *,
    max_jdocs: int = JDG_API_NIGHT_MAX_JDOCS,
    max_days: int = 0,
    force: bool = False,
    notify: bool = False,
) -> dict:
    """
    司法院裁判書 API 夜間批量拉取。

    Parameters:
        max_jdocs: 本次拉取上限筆數（預設 25000）
        max_days:  JList 日期上限（0=不限，拉完所有可用日期）
        force:     強制重新拉取已存在的判決
        notify:    完成後通知
    """
    now = datetime.now()
    if (not force) and (not _is_jdg_service_window(now)):
        return {
            "success": True,
            "skipped": True,
            "message": "不在司法院 API 服務時段（預設 00:00-06:00）",
            "now_hour": now.hour,
            "auth_success": None,
        }

    user, pwd, cred_src = _get_jdg_credentials()
    if (not user) or (not pwd):
        return {
            "success": True,
            "skipped": True,
            "message": "略過：未設定司法院裁判 API 專用帳密（judicial_api_user/judicial_api_pass）",
            "reason": "missing_dedicated_judicial_api_credentials",
            "auth_success": None,
        }

    lock_fh = None
    try:
        import fcntl

        lock_path = os.path.join(CACHE_ROOT, "judicial_api_night_pull.lock")
        lock_fh = open(lock_path, "w", encoding="utf-8")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        lock_fh.flush()
    except BlockingIOError:
        return {
            "success": True,
            "skipped": True,
            "reason": "judicial_api_night_pull_already_running",
            "message": "略過：司法院 API 夜間拉取已在執行，避免重複下載與 API/NAS 負載。",
            "auth_success": None,
        }
    except Exception as lock_exc:
        logger.warning("night_pull: lock unavailable, continuing without lock: %s", lock_exc)

    # Auth with retry (transient server failures may return 驗證失敗 even with valid creds)
    token = ""
    auth_attempts = int(os.environ.get("JUDICIAL_API_AUTH_RETRIES", "3") or "3")
    auth = {}
    for attempt in range(1, auth_attempts + 1):
        auth = _jdg_post_json("Auth", {"user": user, "password": pwd}, timeout_sec=20)
        token = str(auth.get("Token") or auth.get("token") or "").strip()
        if token:
            break
        err_msg = str(auth.get("error") or "")
        if err_msg and "服務時間" in err_msg:
            # 不在服務時段，不需重試
            break
        if attempt < auth_attempts:
            import time as _time
            _time.sleep(5)
    if not token:
        return {
            "success": False,
            "error": auth.get("error") or "auth_failed",
            "detail": auth,
            "auth_success": False,
            "auth_attempts": attempt,
        }

    jlist = _jdg_post_json("JList", {"token": token}, timeout_sec=25)
    if isinstance(jlist, dict) and jlist.get("error"):
        return {"success": False, "error": jlist.get("error"), "detail": jlist, "auth_success": True}
    if not isinstance(jlist, list):
        return {
            "success": False,
            "error": "jlist_invalid_response",
            "detail_type": str(type(jlist)),
            "auth_success": True,
        }

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    pull_state = _load_json_file(JDG_API_PULL_STATE_PATH, {"runs": []})
    fetched = 0
    updated_existing = 0
    skipped = 0
    failed = 0
    removed = 0
    dates_used: list[str] = []
    new_files: list[str] = []
    token_refreshes = 0
    jdoc_retry_max = int(os.environ.get("JUDICIAL_API_JDOC_RETRY_MAX", "2") or "2")
    jdoc_retry_delay = float(os.environ.get("JUDICIAL_API_JDOC_RETRY_DELAY", "1.5") or "1.5")
    refresh_existing = (_env("JUDICIAL_API_REFRESH_EXISTING", "0") or "0").lower() in {
        "1", "true", "yes", "on"
    }

    # max_days=0 表示不限制，拉完 JList 回傳的所有日期
    entries = jlist if (max_days <= 0) else jlist[: max(1, int(max_days))]
    logger.info(
        "night_pull: JList 回傳 %d 個日期，本次處理 %d 個（max_days=%d, max_jdocs=%d）",
        len(jlist), len(entries), max_days, max_jdocs,
    )
    for ent in entries:
        if fetched >= max_jdocs:
            break
        # 時段保護：若超出服務時段自動停止
        if not _is_jdg_service_window():
            logger.warning("night_pull: 已超出服務時段，提前結束（fetched=%d）", fetched)
            break
        if not isinstance(ent, dict):
            continue
        d = str(ent.get("date") or "").strip() or "unknown_date"
        dates_used.append(d)
        jids = ent.get("list") or []
        if not isinstance(jids, list):
            continue
        date_dir = os.path.join(JDG_API_RAW_ROOT, d.replace("/", "-"))
        os.makedirs(date_dir, exist_ok=True)
        date_failed = 0
        for jid in jids:
            if fetched >= max_jdocs:
                break
            jid_s = str(jid or "").strip()
            if not jid_s:
                continue
            slug = _jid_slug(jid_s)
            raw_path = os.path.join(date_dir, slug + ".json")
            existed_before = os.path.exists(raw_path)
            existing_raw = _read_text_file(raw_path, default="") if existed_before else ""
            if existed_before and (not refresh_existing) and (not force):
                skipped += 1
                continue
            time.sleep(0.15)  # 150ms 間隔（6hr 窗口可拉 ~25k 筆）

            # JDoc 拉取 + 失敗重試
            resp = None
            for _retry in range(1 + jdoc_retry_max):
                resp = _jdg_post_json("JDoc", {"token": token, "j": jid_s}, timeout_sec=30)
                # Token 過期自動刷新
                if str(resp.get("error") or "").strip() == "驗證失敗":
                    auth2 = _jdg_post_json("Auth", {"user": user, "password": pwd}, timeout_sec=20)
                    token2 = str(auth2.get("Token") or auth2.get("token") or "").strip()
                    if token2:
                        token_refreshes += 1
                        token = token2
                        resp = _jdg_post_json("JDoc", {"token": token, "j": jid_s}, timeout_sec=30)
                # 成功或「查無資料」→ 不需重試
                if isinstance(resp, dict) and not resp.get("error"):
                    break
                if isinstance(resp, dict) and "查無資料" in str(resp.get("error") or ""):
                    break
                # 暫時性失敗 → 等待後重試
                if _retry < jdoc_retry_max:
                    time.sleep(jdoc_retry_delay * (2 ** _retry))

            wrapper = {
                "jid": jid_s,
                "date": d,
                "pulled_at": datetime.now().isoformat(),
                "payload": resp,
            }
            wrapper_text = json.dumps(wrapper, ensure_ascii=False, sort_keys=True, indent=2)
            if existed_before and existing_raw:
                existing_hash = hashlib.sha1(existing_raw.encode("utf-8", errors="ignore")).hexdigest()
                wrapper_hash = hashlib.sha1(wrapper_text.encode("utf-8", errors="ignore")).hexdigest()
                if existing_hash == wrapper_hash:
                    skipped += 1
                    continue
            if isinstance(resp, dict) and str(resp.get("error") or "").find("查無資料") >= 0:
                removed += 1
            if isinstance(resp, dict) and resp.get("error") and str(resp.get("error")).strip() != "":
                failed += 1
                date_failed += 1
            else:
                if existed_before:
                    updated_existing += 1
                else:
                    fetched += 1
            try:
                with open(raw_path, "w", encoding="utf-8") as f:
                    f.write(wrapper_text)
                new_files.append(raw_path)
            except Exception:
                failed += 1
        # 日期層級日誌
        if len(jids) > 0:
            logger.info(
                "  date=%s: jids=%d, failed=%d, total_fetched=%d",
                d, len(jids), date_failed, fetched,
            )

    pull_state_runs = pull_state.get("runs") if isinstance(pull_state, dict) else []
    if not isinstance(pull_state_runs, list):
        pull_state_runs = []
        
    consecutive_failures = 0
    if len(pull_state_runs) > 0:
        consecutive_failures = pull_state_runs[0].get("consecutive_failures", 0)
        
    if fetched == 0 and updated_existing == 0 and (failed > 0 or not token):
        consecutive_failures += 1
    else:
        # Reset if we successfully fetched something
        if fetched > 0 or updated_existing > 0:
            consecutive_failures = 0
            
    pull_state_runs.insert(
        0,
        {
            "run_id": run_id,
            "ts": datetime.now().isoformat(),
            "fetched": fetched,
            "updated_existing": updated_existing,
            "skipped": skipped,
            "failed": failed,
            "removed": removed,
            "dates": sorted(set(dates_used), reverse=True),
            "credentials_source": cred_src,
            "token_refreshes": token_refreshes,
            "consecutive_failures": consecutive_failures,
        },
    )
    pull_state = {"runs": pull_state_runs[:30]}
    _save_json_file(JDG_API_PULL_STATE_PATH, pull_state)

    msg = (
        f"司法院 API 夜間拉取完成：新抓 {fetched}、更新 {updated_existing}、略過 {skipped}、失敗 {failed}、移除標記 {removed}。"
    )
    
    if consecutive_failures >= 3:
        alert_msg = f"🚨 **警告：司法院判決爬蟲異常** 🚨\n已經連續 {consecutive_failures} 天無法成功抓取任何新判決（可能原因：密碼失效、司法院改版或阻擋 IP）。請律師盡速檢查 `config.json` 憑證或系統狀態！"
        _notify(alert_msg, True, topic_key="judicial_api")
        
    _eventlog(
        "judgment:official_api:night_pull",
        ok=(failed == 0),
        payload={
            "run_id": run_id,
            "fetched": fetched,
            "updated_existing": updated_existing,
            "skipped": skipped,
            "failed": failed,
            "removed": removed,
            "dates": sorted(set(dates_used), reverse=True),
        },
    )
    if notify:
        _notify("🌙 " + msg, True, topic_key="judicial_api")
    return {
        "success": True,
        "auth_success": True,
        "run_id": run_id,
        "message": msg,
        "fetched": fetched,
        "updated_existing": updated_existing,
        "skipped": skipped,
        "failed": failed,
        "removed": removed,
        "dates": sorted(set(dates_used), reverse=True),
        "raw_root": JDG_API_RAW_ROOT,
        "new_files_preview": new_files[:20],
    }


def _extract_jdoc_fields(obj: dict) -> dict:
    payload = obj if isinstance(obj, dict) else {}
    jfullx = payload.get("JFULLX") if isinstance(payload.get("JFULLX"), dict) else {}
    full_text = str(jfullx.get("JFULLCONTENT") or "").strip()
    jfullpdf = str(jfullx.get("JFULLPDF") or "").strip()
    attachments = payload.get("ATTACHMENTS") or []
    if not isinstance(attachments, list):
        attachments = []
    return {
        "jid": str(payload.get("JID") or "").strip(),
        "jyear": str(payload.get("JYEAR") or "").strip(),
        "jcase": str(payload.get("JCASE") or "").strip(),
        "jno": str(payload.get("JNO") or "").strip(),
        "jdate": str(payload.get("JDATE") or "").strip(),
        "jtitle": str(payload.get("JTITLE") or "").strip(),
        "full_text": full_text,
        "full_type": str(jfullx.get("JFULLTYPE") or "").strip(),
        "full_pdf": jfullpdf,
        "attachments": attachments,
    }


def official_api_day_process(
    *,
    max_docs: int = JDG_API_DAY_MAX_PROCESS,
    summarize_max: int = JDG_API_DAY_SUMMARY_MAX,
    summary_mode: str = JDG_API_DAY_SUMMARY_MODE,
    skip_assets: Optional[bool] = None,
    vector_ingest: Optional[bool] = None,
    force: bool = False,
    notify: bool = False,
) -> dict:
    now = datetime.now()
    if (not force) and _is_jdg_service_window(now):
        return {
            "success": True,
            "skipped": True,
            "message": "目前在夜間 API 時段，白天整理任務自動略過",
            "now_hour": now.hour,
        }

    files = _iter_jdg_raw_files()
    if not files:
        return {
            "success": True,
            "skipped": True,
            "message": "無待整理 API 原始檔",
            "backlog_before": 0,
            "backlog_remaining": 0,
        }

    proc_state = _load_json_file(JDG_API_PROCESS_STATE_PATH, {"processed": {}})
    processed_map = proc_state.get("processed") if isinstance(proc_state, dict) else {}
    if not isinstance(processed_map, dict):
        processed_map = {}
    backlog_before_info = _jdg_backlog_status(processed_map)
    backlog_before_count = int(backlog_before_info.get("backlog_count") or 0)
    mode = str(summary_mode or JDG_API_DAY_SUMMARY_MODE or "llm").strip().lower()
    if mode not in {"llm", "extractive", "none", "auto"}:
        mode = "llm"
    if mode == "auto" and backlog_before_count >= max(1, JDG_API_FAST_BACKLOG_THRESHOLD):
        mode = "extractive"
    skip_assets_effective = JDG_API_DAY_SKIP_ASSETS if skip_assets is None else bool(skip_assets)
    if backlog_before_count >= max(1, JDG_API_FAST_BACKLOG_THRESHOLD) and mode in {"extractive", "none"}:
        skip_assets_effective = True
    vector_ingest_effective = (mode == "llm") if vector_ingest is None else bool(vector_ingest)

    conn = _get_db()
    if conn:
        _ensure_table(conn)
        _ensure_court_judgments_table(conn)

    handled = 0
    db_upserts = 0
    archive_upserts = 0
    vector_ingested = 0
    summarized = 0
    skipped = 0
    errors: list[str] = []
    remove_marked = 0
    remove_cleanups = 0
    skipped_low_value = 0
    skipped_missing_text = 0
    assets_skipped = 0
    report_items: list[dict] = []

    for raw_path in files:
        if handled >= int(max_docs):
            break
        rel = os.path.relpath(raw_path, JDG_API_ROOT)
        raw_text = _read_text_file(raw_path, default="")
        if not raw_text:
            skipped += 1
            continue
        raw_hash = hashlib.sha1(raw_text.encode("utf-8", errors="ignore")).hexdigest()
        if processed_map.get(rel) == raw_hash:
            skipped += 1
            continue

        handled += 1
        try:
            raw_obj = json.loads(raw_text) if raw_text else {}
            payload = raw_obj.get("payload") if isinstance(raw_obj, dict) else {}
            if not isinstance(payload, dict):
                payload = {}
            jid = str(payload.get("JID") or raw_obj.get("jid") or "").strip()
            if not jid:
                skipped += 1
                processed_map[rel] = raw_hash
                continue

            if str(payload.get("error") or "").find("查無資料") >= 0:
                remove_marked += 1
                cleanup = _remove_jdg_material_by_jid(conn, jid)
                remove_cleanups += int(cleanup.get("court_judgments_deleted") or 0) + int(cleanup.get("judgment_archive_deleted") or 0)
                processed_map[rel] = raw_hash
                report_items.append({"jid": jid, "status": "removed_marked", "cleanup": cleanup})
                continue

            fields = _extract_jdoc_fields(payload)
            jid = fields["jid"] or jid
            court_name = _jdg_court_name_from_jid(jid)
            case_no = ""
            if fields["jyear"] and fields["jcase"] and fields["jno"]:
                case_no = f"{fields['jyear']}年度{fields['jcase']}字第{fields['jno']}號"
            judgment_date = _parse_jdate_iso(fields["jdate"])
            title = _compose_title_from_jdoc(payload, jid)
            full_text = fields["full_text"]
            source_url = fields["full_pdf"] or (JDG_API_BASE + "/JDoc")
            case_reason = fields["jtitle"] or "裁判書"
            case_type = "行政" if ("行政" in court_name) else "一般"

            if not full_text:
                skipped_missing_text += 1
                processed_map[rel] = raw_hash
                report_items.append({"jid": jid, "status": "skipped_missing_full_text", "title": title[:120]})
                continue

            value_decision = classify_judgment_record(
                jid=jid,
                court_name=court_name,
                case_number=case_no,
                case_reason=case_reason,
                title=title,
                full_text=full_text,
            )
            if value_decision.disposition == SKIP_SUMMARY:
                skipped_low_value += 1
                processed_map[rel] = raw_hash
                report_items.append({
                    "jid": jid,
                    "status": "skipped_low_value",
                    "reason": value_decision.reason,
                    "category": value_decision.category,
                    "title": title[:120],
                })
                continue

            day_tag = (judgment_date or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
            txt_dir = os.path.join(JDG_API_NORMALIZED_ROOT, day_tag)
            os.makedirs(txt_dir, exist_ok=True)
            slug = _jid_slug(jid)
            text_path = os.path.join(txt_dir, slug + ".txt")
            text_out = full_text or (json.dumps(payload, ensure_ascii=False))
            with open(text_path, "w", encoding="utf-8") as f:
                f.write(text_out)
            if skip_assets_effective:
                assets_skipped += 1
                asset_info = {"pdf_path": "", "attachments": [], "failed": [], "skipped": True}
            else:
                asset_info = _download_jdg_assets(jid=jid, fields=fields, target_dir=txt_dir)

            summary = ""
            summary_meta = {"is_degraded": True, "route": "unset", "error": ""}
            is_degraded_summary = True
            if full_text and summarized < int(summarize_max) and mode == "extractive":
                summary = _extractive_judgment_summary(full_text, case_reason)
                summary_meta = {"is_degraded": False, "route": "extractive_backlog", "error": ""}
                is_degraded_summary = bool(_is_degraded_summary(summary, case_reason) or len(summary.strip()) < 80)
                summarized += 1
            elif full_text and summarized < int(summarize_max) and mode == "llm":
                summary = _summarize_judgment(
                    full_text,
                    case_reason,
                    timeout_sec=max(90, int(JDG_API_DAY_SUMMARY_TIMEOUT_SEC)),
                )
                summary_meta = _get_last_summary_meta()
                is_degraded_summary = bool(
                    summary_meta.get("is_degraded", _is_degraded_summary(summary, case_reason))
                    or _is_degraded_summary(summary, case_reason)
                )
                summarized += 1
                # Also store in judgments.json if quality passes
                if not is_degraded_summary and summary:
                    _upsert_judgments_json(
                        title=title,
                        summary=summary,
                        case_reason=case_reason,
                        url=source_url,
                        source="Judicial Yuan API",
                    )
            elif full_text:
                # 截斷時尊重句子邊界，避免斷在中文字中間
                _trunc = full_text[:1400]
                for _sep in ("。", "；", ".\n", "\n"):
                    _cut = _trunc.rfind(_sep, 0, 1200)
                    if _cut > 600:
                        _trunc = _trunc[: _cut + len(_sep)]
                        break
                else:
                    _trunc = full_text[:1200]
                summary = _trunc
                is_degraded_summary = True
            else:
                summary = "（原始資料未提供全文文字，已存原始 JSON）"
                is_degraded_summary = True

            ok_upsert = _upsert_court_judgment_by_jid(
                conn,
                jid=jid,
                court_name=court_name,
                case_number=case_no or jid,
                case_type=case_type,
                judgment_date=judgment_date,
                summary="" if is_degraded_summary else summary,
                full_text=full_text,
                source_url=source_url,
                commit=False,
            )
            if ok_upsert:
                db_upserts += 1

            if conn:
                archive_id = _upsert_judgment_archive_by_source_jid(
                    conn,
                    source_jid=jid,
                    row={
                        "case_number": "",
                        "case_reason": case_reason,
                        "case_type": case_type,
                        "court_level": court_name,
                        "judgment_title": title,
                        "judgment_url": source_url,
                        "judgment_date": judgment_date or "",
                        "full_text_path": text_path,
                        "summary_text": summary,
                        "is_degraded": is_degraded_summary,
                        "search_query": "[JDG API]",
                        "source": "judicial_api",
                        "source_jid": jid,
                    },
                    commit=False,
                )
                if archive_id:
                    archive_upserts += 1

            mem_payload = (
                f"【司法院裁判書】\nJID: {jid}\n法院: {court_name}\n案號: {case_no or jid}\n"
                f"日期: {judgment_date or fields['jdate']}\n案由: {case_reason}\n\n"
                f"摘要:\n{summary[:3000]}\n\n全文節錄:\n{text_out[:max(800, int(JDG_API_DAY_VECTOR_MAX_CHARS))]}"
            )
            if vector_ingest_effective and _remember_judgment_memory(mem_payload, source=f"judicial_api:{jid}", is_degraded=is_degraded_summary):
                vector_ingested += 1

            processed_map[rel] = raw_hash
            report_items.append({
                "jid": jid,
                "status": "ok",
                "title": title[:120],
                "text_path": text_path,
                "pdf_path": asset_info.get("pdf_path") or "",
                "attachments_downloaded": len(asset_info.get("attachments") or []),
                "asset_failures": len(asset_info.get("failed") or []),
            })
        except Exception as e:
            errors.append(f"{os.path.basename(raw_path)}: {type(e).__name__}: {str(e)[:160]}")

    if conn:
        try:
            conn.commit()
        except Exception as e:
            logger.warning("official_api_day_process final commit failed: %s", e)
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3811, exc_info=True)

    # prune process map (avoid unbounded growth)
    if len(processed_map) > 200000:
        keys = list(processed_map.keys())[-120000:]
        processed_map = {k: processed_map[k] for k in keys if k in processed_map}
    proc_state = {
        "processed": processed_map,
        "updated_at": datetime.now().isoformat(),
        "last_run": {
            "handled": handled,
            "db_upserts": db_upserts,
            "archive_upserts": archive_upserts,
            "vector_ingested": vector_ingested,
            "summarized": summarized,
            "remove_marked": remove_marked,
            "remove_cleanups": remove_cleanups,
            "skipped_low_value": skipped_low_value,
            "skipped_missing_text": skipped_missing_text,
            "assets_skipped": assets_skipped,
            "summary_mode": mode,
            "skip_assets": skip_assets_effective,
            "vector_ingest": vector_ingest_effective,
            "errors": len(errors),
            "backlog_before": int(backlog_before_info.get("backlog_count") or 0),
            "max_docs": int(max_docs),
        },
    }
    _save_json_file(JDG_API_PROCESS_STATE_PATH, proc_state)
    backlog_after_info = _jdg_backlog_status(processed_map)
    backlog_remaining = int(backlog_after_info.get("backlog_count") or 0)

    interpretation = build_backlog_interpretation(
        backlog_before=int(backlog_before_info.get("backlog_count") or 0),
        backlog_remaining=backlog_remaining,
        handled=handled,
        db_upserts=db_upserts,
        archive_upserts=archive_upserts,
        vector_ingested=vector_ingested,
        summarized=summarized,
        errors=len(errors),
        oldest_age_hours=backlog_after_info.get("oldest_backlog_age_hours"),
        newest_age_hours=backlog_after_info.get("newest_backlog_age_hours"),
        raw_total=backlog_after_info.get("raw_total"),
        unreadable_count=backlog_after_info.get("unreadable_count"),
        skipped_low_value=skipped_low_value,
        skipped_missing_text=skipped_missing_text,
        max_docs=max_docs,
        runs_per_day=_env("JUDICIAL_API_DAY_RUNS_PER_DAY", "5"),
        cache_root=JDG_API_ROOT,
    )
    msg = format_backlog_notice("白天整理完成", interpretation)
    _eventlog(
        "judgment:official_api:day_process",
        ok=(len(errors) == 0),
        payload={
            "handled": handled,
            "db_upserts": db_upserts,
            "archive_upserts": archive_upserts,
            "vector_ingested": vector_ingested,
            "summarized": summarized,
            "remove_marked": remove_marked,
            "remove_cleanups": remove_cleanups,
            "skipped_low_value": skipped_low_value,
            "skipped_missing_text": skipped_missing_text,
            "assets_skipped": assets_skipped,
            "summary_mode": mode,
            "skip_assets": skip_assets_effective,
            "vector_ingest": vector_ingest_effective,
            "errors": len(errors),
            "backlog_before": int(backlog_before_info.get("backlog_count") or 0),
            "backlog_remaining": backlog_remaining,
            "backlog_status": interpretation.get("status"),
        },
    )
    try:
        backlog_alert_threshold = int(_env("JUDICIAL_API_BACKLOG_ALERT_THRESHOLD", "20") or "20")
    except Exception:
        backlog_alert_threshold = 20
    try:
        backlog_age_threshold = float(_env("JUDICIAL_API_BACKLOG_ALERT_AGE_HOURS", "4") or "4")
    except Exception:
        backlog_age_threshold = 4.0
    if notify and backlog_remaining > 0:
        oldest_age = float(backlog_after_info.get("oldest_backlog_age_hours") or 0.0)
        if backlog_remaining >= max(1, backlog_alert_threshold) or oldest_age >= max(0.5, backlog_age_threshold):
            _notify(
                format_backlog_notice("⚠️ 司法院 API 晨間整理：backlog 需要判讀", interpretation),
                True,
            )
    if notify:
        _notify("☀️ " + msg, True)
    return {
        "success": True,
        "message": msg,
        "handled": handled,
        "db_upserts": db_upserts,
        "archive_upserts": archive_upserts,
        "vector_ingested": vector_ingested,
        "summarized": summarized,
        "remove_marked": remove_marked,
        "remove_cleanups": remove_cleanups,
        "skipped_low_value": skipped_low_value,
        "skipped_missing_text": skipped_missing_text,
        "assets_skipped": assets_skipped,
        "summary_mode": mode,
        "skip_assets": skip_assets_effective,
        "vector_ingest": vector_ingest_effective,
        "errors": errors[:50],
        "items_preview": report_items[:30],
        "backlog_before": int(backlog_before_info.get("backlog_count") or 0),
        "backlog_remaining": backlog_remaining,
        "backlog_before_info": backlog_before_info,
        "backlog_after_info": backlog_after_info,
        "backlog_interpretation": interpretation,
    }


def official_api_auto(force: bool = False, notify: bool = False) -> dict:
    if _is_jdg_service_window():
        return official_api_night_pull(force=force, notify=notify)
    return official_api_day_process(force=force, notify=notify)


# ---------------------------------------------------------------------------
# Daily Crawl
# ---------------------------------------------------------------------------
def _scan_active_cases(max_cases: int = 8) -> list[dict]:
    """
    Scan active case folders with Synology-safe timeouts.
    Synology Drive 偶爾會讓 os.scandir/os.listdir 卡住；這裡改用 /bin/ls + /usr/bin/stat
    並加入 timeout，避免 nightly 任務整輪被拖死。
    """
    # Prefer local DB case index if available; it's far more stable than CloudStorage directory walking.
    # The nightly job may run while Synology Drive is syncing, and `/bin/ls` can time out or return
    # empty results transiently. DB-first avoids false "no active cases" skips.
    def _scan_from_db() -> list[dict]:
        if (_env("JUDGMENT_DAILY_USE_DB_CASES", "1") or "1").strip() not in ("1", "true", "True", "yes", "YES"):
            return []
        conn = _get_db()
        if not conn:
            return []
        try:
            cur = conn.cursor(dictionary=True)
            # Ensure the table exists (best-effort); if not, fall back to filesystem scanning.
            try:
                cur.execute("SHOW TABLES LIKE 'cases'")
                if not cur.fetchone():
                    return []
            except Exception:
                return []

            # Pull more than max_cases to allow filtering (missing folders, closed cases, etc.).
            if _is_unlimited(max_cases):
                limit = max(200, int(JUDGMENT_DAILY_SCAN_FALLBACK_LIMIT))
            else:
                limit = max(20, int(max_cases) * 8)
            cur.execute(
                """
                SELECT
                  case_number,
                  folder_path,
                  case_type,
                  case_reason,
                  status,
                  updated_at,
                  created_date
                FROM cases
                WHERE (
                    status IS NULL
                    OR TRIM(status) = ''
                    OR LOWER(TRIM(status)) IN ('active', 'open', 'pending', 'processing', 'in_progress')
                    OR TRIM(status) IN ('進行中', '處理中', '辦理中', '審理中', '待處理')
                )
                  AND (
                    (folder_path IS NOT NULL AND folder_path <> '')
                    OR (case_reason IS NOT NULL AND case_reason <> '')
                  )
                ORDER BY COALESCE(updated_at, created_date) DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall() or []
        except Exception:
            return []
        finally:
            try:
                cur.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3956, exc_info=True)
            try:
                conn.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3960, exc_info=True)

        out: list[dict] = []
        for r in rows:
            p = translate_case_path_to_local((r.get("folder_path") or "").strip())
            case_no = (r.get("case_number") or "").strip()
            reason = (r.get("case_reason") or "").strip()
            name = os.path.basename(p.rstrip("/")) if p else ""
            if not name:
                # Fallback for DB rows without folder_path:
                # keep the case visible to daily crawl via case_number/reason.
                name = case_no or reason
            if not name or any(k in name for k in CLOSED_KEYWORDS):
                continue
            # If Synology/CloudStorage is busy, folder existence checks can false-negative.
            # For daily crawl we mainly need case_reason; allow missing folder when DB has reason.
            ok_path = False
            if p:
                try:
                    rr = subprocess.run(["/bin/test", "-d", p], timeout=1.5)
                    ok_path = (rr.returncode == 0)
                except Exception:
                    ok_path = False
            out.append({
                "name": name,
                "path": p,
                "mtime": 0,
                "db_case_type": (r.get("case_type") or "").strip(),
                "db_case_reason": reason,
                "path_ok": bool(ok_path),
            })
            if (not _is_unlimited(max_cases)) and len(out) >= int(max_cases):
                break
        return out

    db_cases = _scan_from_db()
    if db_cases:
        return db_cases if _is_unlimited(max_cases) else db_cases[:max_cases]

    case_root = next((p for p in SYNOLOGY_CASE_ROOTS if os.path.isdir(p)), "")
    if not case_root:
        # Fall back to pdf-namer case index cache (does not require traversing Synology root).
        try:
            idx_path = _env("MAGI_PDF_NAMER_CASE_INDEX", f"{_MAGI_ROOT}/skills/pdf-namer/_case_index.json")
            idx_candidates = [
                idx_path,
                os.path.join(PROJECT_ROOT, "skills", "pdf-namer", "_case_index.json"),
            ]
            idx = None
            for cand in idx_candidates:
                if not cand or not os.path.exists(cand):
                    continue
                with open(cand, "r", encoding="utf-8") as f:
                    idx = json.load(f) or []
                if isinstance(idx, list):
                    break
            if isinstance(idx, list):
                out2 = []
                idx_cap = max(50, int(max_cases) * 5) if (not _is_unlimited(max_cases)) else max(200, int(JUDGMENT_DAILY_SCAN_FALLBACK_LIMIT))
                for r in idx[: idx_cap]:
                    p = str((r or {}).get("path") or "").strip()
                    name = str((r or {}).get("folder_name") or "").strip() or os.path.basename(p.rstrip(os.sep))
                    if not p or not name or any(k in name for k in CLOSED_KEYWORDS):
                        continue
                    out2.append({"name": name, "path": p, "mtime": 0, "index_fallback": True})
                    if (not _is_unlimited(max_cases)) and len(out2) >= int(max_cases):
                        break
                if out2:
                    return out2 if _is_unlimited(max_cases) else out2[:max_cases]
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4030, exc_info=True)
        return []

    def _ls_dirs(path: str, timeout_sec: float) -> list[str]:
        try:
            r = subprocess.run(["/bin/ls", "-1", path], capture_output=True, text=True, timeout=timeout_sec)
            if r.returncode != 0:
                return []
            items = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
            out = []
            for name in items:
                full = os.path.join(path, name)
                # Use a quick directory check in a subprocess to avoid Finder/CloudStorage stalls.
                #
                # NOTE: macOS usually provides `test` as a shell builtin; the external binary is at `/bin/test`.
                try:
                    rr = subprocess.run(["/bin/test", "-d", full], timeout=1.0)
                    if rr.returncode == 0:
                        out.append(name)
                except Exception:
                    continue
            return out
        except Exception:
            return []

    def _mtime(path: str) -> int:
        try:
            # macOS: stat -f %m returns epoch mtime
            stat_to = float((_env("JUDGMENT_DAILY_STAT_TIMEOUT_SEC", "2.0") or "2.0").strip() or "2.0")
            r = subprocess.run(["/usr/bin/stat", "-f", "%m", path], capture_output=True, text=True, timeout=stat_to)
            if r.returncode != 0:
                return 0
            return int((r.stdout or "").strip() or "0")
        except Exception:
            return 0

    out: list[dict] = []
    max_age_days = int(_env("JUDGMENT_DAILY_MAX_AGE_DAYS", "365") or "365")
    max_age_sec = max(1, max_age_days) * 24 * 3600
    now = time.time()

    # Directory listing timeout: keep it conservative but not so low that Synology/CloudStorage
    # transient slowness causes false negatives (leading to "no active cases").
    base_ls_to = float((_env("JUDGMENT_DAILY_LS_TIMEOUT_SEC", "6.0") or "6.0").strip() or "6.0")

    # Depth: root / d1 / d2 / d3(case)
    # Retry once if we get an empty set on the first attempt (common during Synology re-index/sync).
    d1s = _ls_dirs(case_root, base_ls_to) or []
    if not d1s and base_ls_to < 12.0:
        time.sleep(0.4)
        d1s = _ls_dirs(case_root, min(12.0, base_ls_to + 4.0)) or []

    for d1 in d1s:
        p1 = os.path.join(case_root, d1)
        d2s = _ls_dirs(p1, base_ls_to) or []
        if not d2s and base_ls_to < 12.0:
            d2s = _ls_dirs(p1, min(12.0, base_ls_to + 4.0)) or []
        for d2 in d2s:
            p2 = os.path.join(p1, d2)
            d3s = _ls_dirs(p2, base_ls_to) or []
            if not d3s and base_ls_to < 12.0:
                d3s = _ls_dirs(p2, min(12.0, base_ls_to + 4.0)) or []
            for d3 in d3s:
                name = d3 or ""
                if any(k in name for k in CLOSED_KEYWORDS):
                    continue
                p3 = os.path.join(p2, d3)
                mt = _mtime(p3)
                if not mt:
                    continue
                if (now - float(mt)) > float(max_age_sec):
                    continue
                out.append({"name": name, "path": p3, "mtime": mt})

    out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    if out:
        return out if _is_unlimited(max_cases) else out[:max_cases]

    # Final fallback: pdf-namer case index (handles transient empty `ls` results).
    try:
        idx_path = _env("MAGI_PDF_NAMER_CASE_INDEX", f"{_MAGI_ROOT}/skills/pdf-namer/_case_index.json")
        if os.path.exists(idx_path):
            with open(idx_path, "r", encoding="utf-8") as f:
                idx = json.load(f) or []
            out2 = []
            idx_cap = max(80, int(max_cases) * 8) if (not _is_unlimited(max_cases)) else max(200, int(JUDGMENT_DAILY_SCAN_FALLBACK_LIMIT))
            for r in idx[: idx_cap]:
                p = str((r or {}).get("path") or "").strip()
                name = str((r or {}).get("folder_name") or "").strip() or os.path.basename(p.rstrip(os.sep))
                if not p or not name or any(k in name for k in CLOSED_KEYWORDS):
                    continue
                out2.append({"name": name, "path": p, "mtime": 0, "index_fallback": True})
                if (not _is_unlimited(max_cases)) and len(out2) >= int(max_cases):
                    break
            if out2:
                return out2 if _is_unlimited(max_cases) else out2[:max_cases]
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4127, exc_info=True)
    return []


def _parse_reason(folder_name: str) -> str:
    parts = [p.strip() for p in (folder_name or "").split("-") if p.strip()]
    if not parts:
        return ""
    reason = parts[-1]
    reason = reason.replace("詐騙", "詐欺").replace("侵佔", "侵占")
    return reason


def _detect_domain(path: str) -> str:
    p = (path or "").replace("\\", "/")
    if "/行政/" in p:
        return "行政"
    return ""


def daily_crawl(
    max_cases: int = 0,
    max_reasons: int = 0,
    max_results_per: int = 120,
    headless: bool = True,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> dict:
    logger.info("Starting daily crawl...")

    t0 = time.time()
    time_budget_sec = int(_env("JUDGMENT_DAILY_TIME_BUDGET_SEC", "21600") or "21600")

    # Allow env overrides for safer/gradual rollouts.
    try:
        max_cases = int(_env("JUDGMENT_DAILY_MAX_CASES", str(max_cases)) or str(max_cases))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4163, exc_info=True)
    try:
        max_reasons = int(_env("JUDGMENT_DAILY_MAX_REASONS", str(max_reasons)) or str(max_reasons))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4167, exc_info=True)
    try:
        max_results_per = int(_env("JUDGMENT_DAILY_MAX_RESULTS_PER", str(max_results_per)) or str(max_results_per))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4171, exc_info=True)
    try:
        timeout_sec = int(_env("JUDGMENT_DAILY_COLLECT_TIMEOUT_SEC", str(timeout_sec)) or str(timeout_sec))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4175, exc_info=True)

    cases = _scan_active_cases(max_cases=max_cases)
    if not cases:
        msg = "每日判決爬取：找不到進行中案件（DB/索引/目錄皆為空），略過"
        _eventlog("judgment:daily_crawl:skipped", ok=True, payload={"message": msg})
        if _env("JUDGMENT_DAILY_NOTIFY_SKIPS", "0") in ("1", "true", "True", "yes", "YES"):
            _notify(msg, True)
        return {"success": True, "skipped": True, "message": msg}

    # Collect unique reasons
    seen = set()
    reasons = []
    for c in cases:
        # Skip non-litigation "case types" that do not map to court judgments.
        # This avoids wasting crawl budget on "常年顧問/法律顧問" etc.
        try:
            ct0 = (c.get("db_case_type") or "").strip() if isinstance(c, dict) else ""
        except Exception:
            ct0 = ""
        if any(k in (ct0 or "") for k in ["顧問", "法律顧問", "常年顧問"]):
            continue

        r = (c.get("db_case_reason") or "").strip() if isinstance(c, dict) else ""
        if not r:
            r = _parse_reason(c.get("name", ""))
        if any(k in (r or "") for k in ["顧問", "常年顧問"]):
            continue
        if not r or r.lower() in seen:
            continue
        seen.add(r.lower())
        domain = (c.get("db_case_type") or "").strip() if isinstance(c, dict) else ""
        if not domain:
            domain = _detect_domain(c.get("path", ""))
        # Normalize to the expected "行政" or "" signal.
        domain = "行政" if "行政" in domain else ""
        reasons.append({"reason": r, "case_type": domain})
        if (not _is_unlimited(max_reasons)) and len(reasons) >= max_reasons:
            break

    if not reasons:
        msg = "每日爬取：無法從案件資料夾解析案由，跳過"
        _notify(msg, True)
        return {"success": True, "skipped": True, "message": msg}

    logger.info("  Found %d unique case reasons: %s", len(reasons), [r["reason"] for r in reasons])

    # Process each reason
    report_lines = ["🌙 每日判決爬取 — " + datetime.now().strftime("%Y-%m-%d %H:%M")]
    all_results = []
    for item in reasons:
        elapsed = (time.time() - t0)
        if time_budget_sec > 0 and elapsed > float(time_budget_sec):
            report_lines.append("  - ⏳ 已達時間上限，剩餘案由留待下一輪 nightly")
            break
        remaining = float(time_budget_sec) - elapsed if time_budget_sec > 0 else float(timeout_sec)
        # Bound per-reason runtime to the remaining budget (best-effort).
        effective_timeout = int(max(30.0, min(float(timeout_sec), remaining)))
        reason = item["reason"]
        case_type = item["case_type"]
        logger.info("  Processing: %s (%s)", reason, case_type or "一般")

        try:
            # Hard-stop protection: even if a sub-skill or DB call hangs, we will continue the nightly run.
            collect_payload = {
                "case_reason": reason,
                "case_type": case_type,
                "max_results": int(max_results_per),
                "max_chars": int(DEFAULT_MAX_CHARS),
                "headless": bool(headless),
                "timeout_sec": int(effective_timeout),
                "save_to_db": True,
                "notify": False,
            }
            result = _collect_with_hard_timeout(collect_payload, hard_timeout_sec=int(effective_timeout) + 60)
            ok = result.get("count", 0)
            report_lines.append(
                "  - " + reason + "（" + _get_court_display(case_type, reason) + "）: "
                + str(ok) + " 筆判決已收集"
            )
            all_results.append({"reason": reason, "result": result})
        except Exception as e:
            report_lines.append("  - " + reason + ": ❌ " + str(e)[:80])
            all_results.append({"reason": reason, "error": str(e)[:200]})

    summary_text = "\n".join(report_lines)
    _eventlog(
        "judgment:daily_crawl:done",
        ok=True,
        payload={
            "reasons_processed": len(reasons),
            "reasons": [r.get("reason") for r in reasons][:10],
            "preview": summary_text[:600],
        },
    )
    if _env("JUDGMENT_DAILY_NOTIFY", "0") in ("1", "true", "True", "yes", "YES"):
        _notify(summary_text, True)

    retry_result = {"success": True, "processed": 0, "improved": 0, "remaining": 0, "offpeak": _is_offpeak_now()}
    if _env("JUDGMENT_SUMMARY_RETRY_ENABLE", "1") in ("1", "true", "True", "yes", "YES"):
        retry_result = retry_summary_queue_auto(notify=False)

    return {
        "success": True,
        "reasons_processed": len(reasons),
        "results": all_results,
        "summary_retry": retry_result,
    }


# ---------------------------------------------------------------------------
# LINE/DC Command Parsing
# ---------------------------------------------------------------------------
def parse_line_command(text: str) -> Optional[dict]:
    t = (text or "").strip()
    if not t:
        return None

    triggers = [
        "判決搜集", "判決蒐集", "收集判決", "蒐集判決",
        "搜尋判決", "搜尋最高法院判決", "搜集判決",
        "判決收集", "判決搜尋",
    ]
    matched = False
    for trigger in triggers:
        if t.startswith(trigger):
            t = t[len(trigger):].strip()
            matched = True
            break
    if not matched:
        return None

    # Check for case_number prefix
    case_number = ""
    m = re.match(r"case_number[：:]?\s*(\S+)\s*(.*)", t)
    if m:
        case_number = m.group(1)
        t = m.group(2).strip()

    if not t and not case_number:
        return None

    return {
        "case_number": case_number,
        "case_reason": t,
    }


# ---------------------------------------------------------------------------
# Main / CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="judgment-collector skill")
    ap.add_argument("--task", default="help", help="task text")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({
            "success": True,
            "commands": [
                "help",
                "self_test",
                'collect {case_reason, case_type?, case_number?, max_results?, max_chars?}',
                'scan_active_cases {"max_cases":0}',
                'scan_active_reasons {"max_cases":0,"max_reasons":0}',
                "daily_crawl",
                'official_api_night_pull {"max_jdocs":25000,"max_days":0,"force":false}',
                'official_api_day_process {"max_docs":200,"summarize_max":80,"force":false}',
                'official_api_auto {"force":false}',
                'backfill_archive_summaries {"max_items":50,"year_min":0,"year_max":9999}',
                'backfill_court_judgments {"limit":0}',
                "retry_summary_queue",
                "retry_summary_queue_auto",
                'trend_analysis {"case_reason":"","top_n":10}',
                'synthesize {"case_reason":"詐欺","statute":"","max_items":20}',
            ],
            "line_triggers": [
                "判決搜集 <案由>",
                "收集判決 <案由>",
                "搜尋判決 <案由>",
            ],
        })

    if task == "self_test":
        # Keep self-test deterministic and short: validate local scan + parser only.
        try:
            cases = _scan_active_cases(max_cases=5)
            parsed = _parse_reason("臺灣臺北地方法院 113年度訴字第123號 詐欺")
            ok = isinstance(cases, list) and isinstance(parsed, str) and bool(parsed.strip())
            return _ok(
                {
                    "success": bool(ok),
                    "details": {
                        "scanned_cases": len(cases or []),
                        "parsed_reason": parsed,
                    },
                }
            )
        except Exception as e:
            return _ok({"success": False, "error": f"{type(e).__name__}: {e}"})

    if task.startswith("scan_active_cases"):
        payload = _load_jsonish(task[len("scan_active_cases"):].strip())
        try:
            v = (payload.get("max_cases") if isinstance(payload, dict) else None)
            max_cases = int(v) if v is not None else 0
        except Exception:
            max_cases = 0
        cases = _scan_active_cases(max_cases=max_cases)
        # Keep output compact; full details are in report.json when run via autopilot.
        slim = []
        preview_n = int(_env("JUDGMENT_SCAN_PREVIEW_LIMIT", "200") or "200")
        for c in (cases or [])[: max(1, preview_n)]:
            if not isinstance(c, dict):
                continue
            slim.append(
                {
                    "name": (c.get("name") or "")[:120],
                    "path": (c.get("path") or "")[:240],
                    "db_case_type": (c.get("db_case_type") or ""),
                    "db_case_reason": (c.get("db_case_reason") or ""),
                }
            )
        return _ok({"success": True, "count": len(cases or []), "cases": slim})

    if task.startswith("scan_active_reasons"):
        payload = _load_jsonish(task[len("scan_active_reasons"):].strip())
        try:
            v = (payload.get("max_cases") if isinstance(payload, dict) else None)
            max_cases = int(v) if v is not None else 0
        except Exception:
            max_cases = 0
        try:
            v = (payload.get("max_reasons") if isinstance(payload, dict) else None)
            max_reasons = int(v) if v is not None else 0
        except Exception:
            max_reasons = 0
        cases = _scan_active_cases(max_cases=max_cases)
        seen = set()
        reasons = []
        for c in (cases or []):
            if not isinstance(c, dict):
                continue
            r = (c.get("db_case_reason") or "").strip()
            if not r:
                r = _parse_reason(c.get("name", ""))
            if not r:
                continue
            k = r.lower()
            if k in seen:
                continue
            seen.add(k)
            domain = (c.get("db_case_type") or "").strip()
            if not domain:
                domain = _detect_domain(c.get("path", ""))
            domain = "行政" if "行政" in domain else ""
            reasons.append({"reason": r, "case_type": domain})
            if (not _is_unlimited(max_reasons)) and len(reasons) >= int(max_reasons):
                break
        return _ok({"success": True, "cases_scanned": len(cases or []), "reasons": reasons})

    if task.startswith("collect") or task.startswith("收集判決") or task.startswith("判決搜集"):
        key = ""
        for k in ["collect", "收集判決", "判決搜集"]:
            if task.startswith(k):
                key = k
                break
        payload = _load_jsonish(task[len(key):].strip())
        r = collect(
            case_number=payload.get("case_number", ""),
            case_reason=payload.get("case_reason", ""),
            case_type=payload.get("case_type", ""),
            max_results=int(payload.get("max_results", DEFAULT_MAX_RESULTS)),
            max_chars=int(payload.get("max_chars", DEFAULT_MAX_CHARS)),
            headless=bool(payload.get("headless", True)),
            timeout_sec=int(payload.get("timeout_sec", DEFAULT_TIMEOUT_SEC)),
            save_to_db=bool(payload.get("save_to_db", True)),
            notify=bool(payload.get("notify", True)),
        )
        return _ok(r)

    if task in ("daily_crawl", "每日爬取"):
        r = daily_crawl()
        return _ok(r)

    if task.startswith("official_api_night_pull") or task.startswith("夜間拉取裁判API"):
        payload = _load_jsonish(task.replace("official_api_night_pull", "", 1).replace("夜間拉取裁判API", "", 1).strip())
        try:
            max_jdocs = int(payload.get("max_jdocs", JDG_API_NIGHT_MAX_JDOCS))
        except Exception:
            max_jdocs = JDG_API_NIGHT_MAX_JDOCS
        try:
            max_days = int(payload.get("max_days", 7))
        except Exception:
            max_days = 7
        force = bool(payload.get("force", False))
        notify = bool(payload.get("notify", False))
        r = official_api_night_pull(max_jdocs=max_jdocs, max_days=max_days, force=force, notify=notify)
        return _ok(r)

    if task.startswith("official_api_day_process") or task.startswith("白天整理裁判API"):
        payload = _load_jsonish(task.replace("official_api_day_process", "", 1).replace("白天整理裁判API", "", 1).strip())
        try:
            max_docs = int(payload.get("max_docs", JDG_API_DAY_MAX_PROCESS))
        except Exception:
            max_docs = JDG_API_DAY_MAX_PROCESS
        try:
            summarize_max = int(payload.get("summarize_max", JDG_API_DAY_SUMMARY_MAX))
        except Exception:
            summarize_max = JDG_API_DAY_SUMMARY_MAX
        summary_mode = str(payload.get("summary_mode") or JDG_API_DAY_SUMMARY_MODE or "llm")
        skip_assets = payload.get("skip_assets")
        if skip_assets is None:
            skip_assets = payload.get("skip_asset_downloads")
        vector_ingest = payload.get("vector_ingest")
        if vector_ingest is None:
            vector_ingest = payload.get("vector")
        force = bool(payload.get("force", False))
        notify = bool(payload.get("notify", False))
        r = official_api_day_process(
            max_docs=max_docs,
            summarize_max=summarize_max,
            summary_mode=summary_mode,
            skip_assets=skip_assets if skip_assets is not None else None,
            vector_ingest=vector_ingest if vector_ingest is not None else None,
            force=force,
            notify=notify,
        )
        return _ok(r)

    if task.startswith("official_api_auto") or task.startswith("裁判API自動模式"):
        payload = _load_jsonish(task.replace("official_api_auto", "", 1).replace("裁判API自動模式", "", 1).strip())
        force = bool(payload.get("force", False))
        notify = bool(payload.get("notify", False))
        r = official_api_auto(force=force, notify=notify)
        return _ok(r)

    if task.startswith("backfill_archive_summaries") or task.startswith("回填見解庫"):
        payload = _load_jsonish(
            task.replace("backfill_archive_summaries", "", 1)
                .replace("回填見解庫", "", 1).strip()
        )
        r = backfill_archive_summaries(
            max_items=int(payload.get("max_items", 50)),
            min_text_bytes=int(payload.get("min_text_bytes", 2000)),
            timeout_per_item=int(payload.get("timeout_per_item", 300)),
            year_min=int(payload.get("year_min", 0)),
            year_max=int(payload.get("year_max", 9999)),
            notify=bool(payload.get("notify", False)),
        )
        return _ok(r)

    if task.startswith("backfill_court_judgments") or task.startswith("回填判決向量索引"):
        payload = _load_jsonish(task.replace("backfill_court_judgments", "", 1).replace("回填判決向量索引", "", 1).strip())
        try:
            limit = int(payload.get("limit", 0))
        except Exception:
            limit = 0
        conn = _get_db()
        r = _backfill_court_judgments_from_archive(conn, limit=limit)
        try:
            if conn:
                conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4525, exc_info=True)
        return _ok(r)

    if task.startswith("retry_summary_queue_auto") or task.startswith("重試摘要佇列自動"):
        payload = _load_jsonish(task.replace("retry_summary_queue_auto", "", 1).replace("重試摘要佇列自動", "", 1).strip())
        notify = bool(payload.get("notify", False))
        r = retry_summary_queue_auto(notify=notify)
        return _ok(r)

    if task.startswith("retry_summary_queue") or task.startswith("重試摘要佇列"):
        payload = _load_jsonish(task.replace("retry_summary_queue", "", 1).replace("重試摘要佇列", "", 1).strip())
        try:
            max_items = int(payload.get("max_items", 3))
        except Exception:
            max_items = 3
        try:
            timeout_sec = int(payload.get("timeout_sec", 420))
        except Exception:
            timeout_sec = 420
        notify = bool(payload.get("notify", False))
        r = retry_summary_queue(max_items=max_items, timeout_sec=timeout_sec, notify=notify)
        return _ok(r)

    if task.startswith("synthesize") or task.startswith("綜合分析") or task.startswith("見解整合"):
        payload = _load_jsonish(
            task.replace("synthesize", "", 1)
            .replace("綜合分析", "", 1)
            .replace("見解整合", "", 1)
            .strip()
        )
        case_reason = str(payload.get("case_reason", "")).strip()
        statute = str(payload.get("statute", "")).strip()
        max_items = int(payload.get("max_items", 20))
        report = synthesize_holdings(case_reason=case_reason, statute=statute, max_items=max_items)
        print(report)
        return 0

    if task.startswith("trend_analysis") or task.startswith("判決趨勢") or task.startswith("趨勢分析"):
        payload = _load_jsonish(
            task.replace("trend_analysis", "", 1)
            .replace("判決趨勢", "", 1)
            .replace("趨勢分析", "", 1)
            .strip()
        )
        case_reason = str(payload.get("case_reason", "")).strip()
        top_n = int(payload.get("top_n", 10))
        data = trend_analysis(case_reason=case_reason, top_n=top_n)
        report = format_trend_report(data)
        print(report)
        return 0

    # Try as LINE command
    parsed = parse_line_command(task)
    if parsed:
        r = collect(
            case_number=parsed.get("case_number", ""),
            case_reason=parsed.get("case_reason", ""),
            max_results=DEFAULT_MAX_RESULTS,
            max_chars=DEFAULT_MAX_CHARS,
            headless=True,
            timeout_sec=DEFAULT_TIMEOUT_SEC,
            save_to_db=True,
            notify=True,
        )
        return _ok(r)

    return _ok({"success": False, "error": "unknown task: " + task})


if __name__ == "__main__":
    raise SystemExit(main())
