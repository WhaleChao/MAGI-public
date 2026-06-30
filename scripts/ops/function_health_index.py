#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = MAGI_ROOT / "config" / "test_matrix.json"
DEFAULT_RUNTIME_DIR = MAGI_ROOT / ".runtime"
DEFAULT_LIVE_RUNTIME_ROOT = Path("/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2")

_SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".versions",
    "__pycache__",
    "_bg_jobs",
    "node_modules",
    "venv",
}
_SCRIPT_RE = re.compile(
    r"(?:^|[\s'\"/])"
    r"((?:api|config|scripts|skills)/[^'\"\s]+?\.(?:py|sh))"
)
_JSON_OUT_FLAGS = {"--json-out", "--output-json", "--report-json"}
_FAILED_STATUS = {"error", "failed", "fail", "down", "not_ready", "unhealthy"}
_OK_STATUS = {"ok", "ready", "live", "success", "passed", "healthy", "skipped"}
try:
    from api.tools.contracts import GENERAL_ERROR_CATEGORIES as _TOOL_GENERAL_ERROR_CATEGORIES
except Exception:
    _TOOL_GENERAL_ERROR_CATEGORIES = (
        "auth_required",
        "login_failed",
        "path_missing",
        "external_service",
        "validation_failed",
        "unknown",
    )

GENERAL_ERROR_CATEGORIES = tuple(_TOOL_GENERAL_ERROR_CATEGORIES)
_SKILL_INTERNAL_ALIASES = {
    "iron_dome": "iron-dome",
    "osc_orchestrator": "osc-orchestrator",
}
_SKILL_ACTION_OPTIONAL = {
    "ops/database",
    "ops/sunrise_protocol",
}
DEFAULT_INTELLIGENCE_SNAPSHOT = MAGI_ROOT / ".runtime" / "magi_health_intelligence_snapshot_latest.json"


CORE_FUNCTION_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "id": "system_health",
        "name": "系統健康與每日啟動",
        "manual_section_hint": "每日啟動與健康檢查",
        "user_summary": "先看 MAGI、模型、磁碟、NAS、DB、OAuth 與背景服務是不是能安全工作。",
        "entry_points": ["MAGI 系統狀態", "/health", "magi status", "NERV 狀態頁"],
        "unit_tests": [
            "tests/test_function_health_index.py",
            "tests/test_health_readiness_routes.py",
            "tests/test_system_health_probes.py",
            "tests/test_magi_doctor.py",
        ],
        "live_patterns": [
            "production_live_latest",
            "magi_doctor",
            "model_live_gate_latest",
            "resource_governor",
            "function_health_index",
        ],
        "manual_commands": ["MAGI 系統狀態。", "跑完整 smoke62 與 commercial readiness。"],
        "token_names": [],
    },
    {
        "id": "conversation_routing",
        "name": "自然語言入口與工具路由",
        "manual_section_hint": "對話、指令與工具調用",
        "user_summary": "用平常說法查行程、案件、文件或系統狀態；MAGI 會把需要工具的問題送到對應功能。",
        "entry_points": ["聊天框", "LINE / Discord / Telegram", "@heavy / @重型"],
        "unit_tests": [
            "tests/test_manual_command_routes.py",
            "tests/test_intent_no_weather_for_reminder.py",
            "tests/test_route_policy.py",
            "tests/test_command_dispatch.py",
        ],
        "live_patterns": ["manual_command_smoke", "smoke_test_full_live_latest", "smoke_test_full_latest"],
        "manual_commands": ["今天有什麼行程？", "@heavy 翻譯這份 PDF，專有名詞後保留原文。"],
        "token_names": [],
    },
    {
        "id": "case_workspace",
        "name": "案件查詢與案件資料夾",
        "manual_section_hint": "案件與資料夾",
        "user_summary": "用案號、當事人或案件線索查案件，並打開標準案件資料夾與終局文件區。",
        "entry_points": ["案件頁", "自然語言案件查詢", "資料夾捷徑"],
        "unit_tests": [
            "tests/test_case_display.py",
            "tests/test_osc_folder_utils.py",
            "tests/test_osc_open_folder.py",
            "tests/test_laf_folder_category.py",
        ],
        "live_patterns": ["smoke_test_full_live_latest", "business_module_live_check_latest", "case_query"],
        "manual_commands": ["查 2026-0001 的案件狀態。", "打開 2026-0001 資料夾。"],
        "token_names": [],
    },
    {
        "id": "calendar_todos",
        "name": "Google Calendar 與 OSC 待辦",
        "manual_section_hint": "Calendar 與 OSC 待辦",
        "user_summary": "查今日或本週行程、看 OSC 待辦，並讓外部日曆事件保留原本標題。",
        "entry_points": ["Calendar", "OSC 待辦頁", "自然語言"],
        "unit_tests": [
            "tests/test_osc_events_refresh.py",
            "tests/test_osc_gcal_sync_import.py",
            "tests/test_gcal_dedup.py",
            "tests/test_intent_no_weather_for_reminder.py",
        ],
        "live_patterns": ["calendar_todo_status_live", "google_calendar", "manual_command_smoke", "production_live_latest"],
        "manual_commands": ["今天有什麼行程？", "列出本週 OSC 建立待辦。"],
        "token_names": ["google_calendar"],
    },
    {
        "id": "laf_business",
        "name": "法扶業務模組",
        "manual_section_hint": "法扶業務",
        "user_summary": "處理法扶派案、狀態查詢、開辦/報結資料檢查、待補資料文字與文件分類。",
        "entry_points": ["法扶頁", "自然語言", "夜間巡檢"],
        "unit_tests": [
            "tests/test_laf_handler.py",
            "tests/test_laf_case_classifier.py",
            "tests/test_laf_go_live_docs.py",
            "tests/test_laf_nightly_audit_status.py",
        ],
        "live_patterns": ["laf_portal_live", "laf_self_test", "business_module_live_check_latest"],
        "manual_commands": ["查 1150421-W-004 法扶狀態。", "產生這件消債案件的待補資料文字。"],
        "token_names": [],
        "auth_hint": "法扶入口使用 portal/session 類憑證；OAuth token health 不會列出帳密內容。",
    },
    {
        "id": "file_review",
        "name": "閱卷業務模組",
        "manual_section_hint": "閱卷業務",
        "user_summary": "分辨待繳費、可下載、到院閱卷與已略過，避免把繳費單當成閱卷成果。",
        "entry_points": ["閱卷頁", "自然語言", "夜間巡檢"],
        "unit_tests": [
            "tests/test_file_review_dispatch.py",
            "tests/test_file_review_db_sync.py",
            "tests/test_file_review_preclick_dedup.py",
            "tests/test_file_review_login_diagnostics.py",
        ],
        "live_patterns": ["file_review_self_test", "file_review_downloadable_probe", "business_module_live_check_latest"],
        "manual_commands": ["檢查這件是否有新閱卷資料。", "列出待繳費閱卷。"],
        "token_names": [],
        "auth_hint": "閱卷入口使用 portal/session 類憑證；OAuth token health 不會列出帳密內容。",
    },
    {
        "id": "transcript",
        "name": "筆錄業務模組",
        "manual_section_hint": "筆錄業務",
        "user_summary": "確認 SSO、入口頁與 DB 探測後下載新筆錄；登入失敗時停止後續歸檔。",
        "entry_points": ["筆錄頁", "自然語言", "夜間巡檢"],
        "unit_tests": [
            "tests/test_transcript_indexer_action.py",
            "tests/test_transcript_notify_summary.py",
            "tests/test_transcript_portal_failure.py",
            "tests/test_transcript_todo_extractor.py",
        ],
        "live_patterns": ["transcript_self_test", "transcript_db_probe", "business_module_live_check_latest"],
        "manual_commands": ["下載這件的新筆錄。", "檢查筆錄同步狀態。"],
        "token_names": [],
        "auth_hint": "筆錄入口使用 SSO/session 類憑證；OAuth token health 不會列出帳密內容。",
    },
    {
        "id": "pdf_ocr_naming",
        "name": "PDF 判讀、OCR 與歸檔命名",
        "manual_section_hint": "PDF、OCR、命名與書籤",
        "user_summary": "掃描或可讀 PDF 會先抽文字，再依文件種類、日期、法院與資料夾範本產出可歸檔檔名。",
        "entry_points": ["檔案頁", "PDF 工具", "@heavy"],
        "unit_tests": [
            "tests/test_pdf_namer_runtime.py",
            "tests/test_pdf_bridge_ocr_consensus.py",
            "tests/test_ocr_consensus.py",
            "tests/test_legal_ocr_corrector.py",
        ],
        "live_patterns": ["smoke_test_full_live_latest", "chandra_ocr_healthcheck", "heavy_translation_quality", "document_processing"],
        "manual_commands": ["從這份法院通知建立待辦。", "幫這批 PDF 依資料夾範本命名。"],
        "token_names": [],
    },
    {
        "id": "pdf_bookmarking",
        "name": "PDF 書籤",
        "manual_section_hint": "PDF、OCR、命名與書籤",
        "user_summary": "長卷宗可依文件邊界、日期與文件類型建立書籤；單一文件也會保留 page-1 書籤。",
        "entry_points": ["PDF 工具", "批次腳本", "@heavy"],
        "unit_tests": [
            "tests/test_benchmark_pdf_bookmarker.py",
            "tests/test_benchmark_pdf_bookmarker_thresholds.py",
            "tests/test_pdf_bookmarker_single_doc_live.py",
            "tests/test_weekend_bookmark_batch.py",
        ],
        "live_patterns": ["smoke_test_full_live_latest", "benchmark_pdf_bookmarker", "pdf_bookmarker"],
        "manual_commands": ["@heavy 請摘要這份卷宗並建立書籤。"],
        "token_names": [],
    },
    {
        "id": "judicial_research",
        "name": "司法 API、判決資料與法律研究",
        "manual_section_hint": "法律研究與判決資料",
        "user_summary": "查判決、法條與實務見解，並標示來源；夜間管線會回報是否仍在追趕 backlog。",
        "entry_points": ["法律研究", "夜間作業", "自然語言"],
        "unit_tests": [
            "tests/test_judicial_api_load_policy.py",
            "tests/test_judicial_api_backlog.py",
            "tests/test_judicial_web_search.py",
            "tests/test_judgment_flow.py",
        ],
        "live_patterns": ["judicial_api_pipeline", "judgment_summary_live", "production_live_latest"],
        "manual_commands": ["查這個爭點的實務見解。", "列出司法 API 管線健康狀態。"],
        "token_names": [],
    },
    {
        "id": "notifications",
        "name": "通知分流與去重",
        "manual_section_hint": "通知與分層",
        "user_summary": "LINE、Discord、Telegram 按 topic 分層，避免繳費、閱卷與系統健康通知互相混雜或重複。",
        "entry_points": ["通知設定", "Webhook", "Red phone"],
        "unit_tests": [
            "tests/test_notification_routing_policy.py",
            "tests/test_business_module_notifications.py",
            "tests/test_discord_channel_router_progress_channel.py",
            "tests/test_line_compat.py",
        ],
        "live_patterns": ["business_module_live_check_latest", "smoke_three_channels", "manual_command_smoke"],
        "manual_commands": ["檢查通知分流狀態。", "測試 Telegram topic routing。"],
        "token_names": ["line_messaging", "line_channel_secret", "discord_bot", "telegram_bot"],
    },
    {
        "id": "drive_nas_sync",
        "name": "Google Drive / NAS 同步",
        "manual_section_hint": "帳務、Drive 與 NAS",
        "user_summary": "檢查 NAS 掛載、Drive 下載/上傳與案件檔案同步，失敗時指出是 token、路徑或 API 問題。",
        "entry_points": ["同步頁", "夜間作業", "自然語言"],
        "unit_tests": [
            "tests/test_drive_case_sync.py",
            "tests/test_drive_case_sync_worker_lock.py",
            "tests/test_nas_mount_guard.py",
            "tests/test_tailscale_funnel_healthcheck.py",
        ],
        "live_patterns": ["nas_mounts_live", "drive_sync_status_live", "business_module_live_check_latest"],
        "manual_commands": ["檢查 Drive/NAS 同步。"],
        "token_names": ["google_drive_sync_readonly", "google_drive_sync_write"],
    },
    {
        "id": "accounting",
        "name": "帳務匯入",
        "manual_section_hint": "帳務、Drive 與 NAS",
        "user_summary": "從 Google Sheets/Drive 匯入帳務，排除非本人項目並監控 OAuth 授權狀態。",
        "entry_points": ["帳務頁", "自然語言", "Google Sheets"],
        "unit_tests": [
            "tests/test_accounting_sheet_import.py",
            "tests/test_accounting_monthly_bonus.py",
            "tests/test_osc_laf_debt_required_checklist.py",
        ],
        "live_patterns": ["token_health_refresh", "business_module_live_check_latest", "accounting_query"],
        "manual_commands": ["匯入這個月帳務，排除非本人項目。"],
        "token_names": ["google_accounting_sheets"],
    },
    {
        "id": "model_resource_governance",
        "name": "模型與資源治理",
        "manual_section_hint": "每日啟動與健康檢查",
        "user_summary": "確認日夜模型、MTP sidecar、resource governor 與重型模式在目前資源下可安全運作。",
        "entry_points": ["系統狀態", "夜間巡檢", "@heavy"],
        "unit_tests": [
            "tests/test_model_live_gate.py",
            "tests/test_resource_governor.py",
            "tests/test_inference_gateway.py",
            "tests/test_omlx_switch_gatekeeper.py",
        ],
        "live_patterns": ["model_live_gate_latest", "resource_governor", "heavy_fallback_live", "smart_model_router_live"],
        "manual_commands": ["檢查目前模型。", "@heavy 請精讀這份長文件。"],
        "token_names": ["nvidia_nim", "gemini", "huggingface"],
    },
    {
        "id": "security_release",
        "name": "安全與公開版守門",
        "manual_section_hint": "使用守則",
        "user_summary": "發布或交付前檢查私密資料、硬編碼端點、不安全 shell、公開版隔離與 release gate。",
        "entry_points": ["CI", "release guard", "public audit"],
        "unit_tests": [
            "tests/test_public_push_guard.py",
            "tests/test_security_baseline.py",
            "tests/test_public_release_audit.py",
            "tests/test_safe_process.py",
        ],
        "live_patterns": ["commercial_readiness_live_latest", "operational_hardening_audit_latest", "public_release_audit"],
        "manual_commands": ["跑公開版隔離檢查。", "跑 commercial-release gate。"],
        "token_names": ["magi_internal_api"],
    },
)
_CORE_SKILL_CANONICALS = {
    "file-review-orchestrator",
    "judicial-web-search",
    "laf-orchestrator",
    "osc-orchestrator",
    "pdf-namer",
    "transcript-downloader",
    "transcript-todo-extractor",
}
_CORE_DIRECT_HANDLERS = {
    "web_search",
}
_CORE_API_TOOLS = {
    "fetch",
    "research",
    "search",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def default_runtime_dir(root: Path) -> Path:
    source_runtime = root / ".runtime"
    live_env = os.environ.get("MAGI_LIVE_RUNTIME_ROOT")
    live_root = Path(live_env or DEFAULT_LIVE_RUNTIME_ROOT).expanduser()
    live_runtime = live_root / ".runtime"
    try:
        use_live = bool(live_env) or root.resolve() == MAGI_ROOT.resolve()
        if use_live and live_root.exists() and live_runtime.exists() and live_root.resolve() != root.resolve():
            return live_runtime
    except Exception:
        pass
    return source_runtime


def _read_json(path: Path, default: Any = None) -> tuple[Any, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return default, "missing"
    except Exception as exc:
        return default, f"{type(exc).__name__}: {exc}"


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _mtime_dt(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return None


def _age_hours(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return round(max(0.0, (now - dt).total_seconds() / 3600.0), 3)


def _has_skip_part(path: Path) -> bool:
    return any(part in _SKIP_PARTS or part.startswith(".") for part in path.parts)


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            elif isinstance(item, ast.FormattedValue):
                parts.append("{}")
            else:
                return None
        return "".join(parts)
    return None


def _literal_value(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        text = _literal_string(node)
        if text is not None:
            return text
    return None


def _dedupe_limited(values: list[str], *, limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw or "").strip())
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value[:220])
        if len(out) >= limit:
            break
    return out


def _read_text(path: Path, *, max_chars: int = 80_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def _annotation_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _format_default(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if len(value) > 48:
            value = value[:45] + "..."
        return repr(value)
    return repr(value)


def _route_methods(call: ast.Call) -> list[str]:
    for keyword in call.keywords:
        if keyword.arg != "methods":
            continue
        value = keyword.value
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            out: list[str] = []
            for item in value.elts:
                text = _literal_string(item)
                if text:
                    out.append(text.upper())
            return sorted(set(out or ["GET"]))
        text = _literal_string(value)
        if text:
            return [text.upper()]
    return ["GET"]


def _route_calls_from_decorator(node: ast.AST) -> ast.Call | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "route":
        return node
    return None


def _is_add_url_rule_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_url_rule"
    )


def classify_route_domain(route: str, file_path: str = "") -> str:
    route_l = route.lower()
    file_l = file_path.lower()
    if route in {"/health", "/livez", "/readyz"} or "health" in route_l or "status" in route_l:
        return "health"
    if route_l.startswith("/api/osc") or route_l.startswith("/osc") or "osc_" in file_l:
        return "osc"
    if route_l.startswith("/api/nerv") or route_l.startswith("/admin") or "admin_runtime" in file_l:
        return "admin"
    if route_l.startswith("/skills") or route_l.startswith("/api/skills"):
        return "skills"
    if "webhook" in route_l or route_l.startswith("/line") or route_l.startswith("/telegram") or route_l == "/callback":
        return "webhooks"
    if route_l.startswith("/api/memory") or route_l in {"/remember", "/recall"}:
        return "memory"
    if route_l.startswith("/legal") or "judicial" in route_l:
        return "legal"
    if route_l.startswith("/collab"):
        return "collab"
    if route_l.startswith("/shortcut") or route_l in {"/search", "/research", "/fetch", "/vision", "/summarize"}:
        return "tools"
    if route_l.startswith("/mobile") or route_l.startswith("/app"):
        return "mobile"
    if route_l.startswith("/static") or route_l.startswith("/exports") or route_l.startswith("/s/"):
        return "files"
    if "golem" in route_l or "golem" in file_l:
        return "golem"
    if route_l.startswith("/dashboard") or route_l in {"/", "/login", "/logout", "/register"}:
        return "web"
    return "other"


def discover_api_routes(root: Path) -> dict[str, Any]:
    routes: list[dict[str, Any]] = []
    api_root = root / "api"
    if not api_root.exists():
        return {"present": False, "total": 0, "domains": {}, "routes": []}

    for path in sorted(api_root.rglob("*.py")):
        rel = Path(_rel(path, root))
        if _has_skip_part(rel):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError as exc:
            routes.append(
                {
                    "route": "",
                    "methods": [],
                    "domain": "parse_error",
                    "file": rel.as_posix(),
                    "line": exc.lineno or 0,
                    "handler": "",
                    "error": str(exc),
                }
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    call = _route_calls_from_decorator(decorator)
                    if not call or not call.args:
                        continue
                    route = _literal_string(call.args[0])
                    if not route:
                        continue
                    routes.append(
                        {
                            "route": route,
                            "methods": _route_methods(call),
                            "domain": classify_route_domain(route, rel.as_posix()),
                            "file": rel.as_posix(),
                            "line": getattr(call, "lineno", getattr(node, "lineno", 0)),
                            "handler": node.name,
                        }
                    )
            elif _is_add_url_rule_call(node) and node.args:
                route = _literal_string(node.args[0])
                if not route:
                    continue
                routes.append(
                    {
                        "route": route,
                        "methods": _route_methods(node),
                        "domain": classify_route_domain(route, rel.as_posix()),
                        "file": rel.as_posix(),
                        "line": getattr(node, "lineno", 0),
                        "handler": "add_url_rule",
                    }
                )

    domains: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for route in routes:
        grouped[route["domain"]].append(route)
    for domain, items in sorted(grouped.items()):
        method_counts: dict[str, int] = defaultdict(int)
        files = set()
        for item in items:
            files.add(item["file"])
            for method in item["methods"]:
                method_counts[method] += 1
        domains[domain] = {
            "count": len(items),
            "method_counts": dict(sorted(method_counts.items())),
            "files": sorted(files),
        }

    return {
        "present": True,
        "total": len(routes),
        "domains": domains,
        "routes": sorted(routes, key=lambda item: (item["domain"], item["route"], item["file"], item["line"])),
    }


def _skill_frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    out: dict[str, str] = {}
    lines = text.splitlines()
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    for raw in lines[1:] if in_frontmatter else lines[:30]:
        line = raw.strip()
        if in_frontmatter and line == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in {"name", "description"}:
            out[key] = value.strip().strip('"').strip("'")
    return out


def _skill_canonical(folder: str) -> str:
    top, _, rest = folder.partition("/")
    top = _SKILL_INTERNAL_ALIASES.get(top, top)
    canonical = top.replace("_", "-")
    if rest:
        return f"{canonical}/{rest.replace('_', '-')}"
    return canonical


def discover_skills(root: Path) -> dict[str, Any]:
    skills_root = root / "skills"
    entries: list[dict[str, Any]] = []
    if not skills_root.exists():
        return {
            "present": False,
            "total": 0,
            "with_skill_md": 0,
            "with_action": 0,
            "internal_alias_count": 0,
            "action_optional_count": 0,
            "missing_skill_md": [],
            "missing_action": [],
            "duplicate_canonical": [],
            "entries": [],
        }

    for path in sorted(skills_root.rglob("*")):
        if not path.is_dir():
            continue
        rel = Path(_rel(path, root))
        if _has_skip_part(rel):
            continue
        skill_md = path / "SKILL.md"
        action_py = path / "action.py"
        if not skill_md.exists() and not action_py.exists():
            continue
        folder = _rel(path, skills_root)
        top_folder = Path(folder).parts[0] if folder else ""
        if top_folder in _SKILL_INTERNAL_ALIASES:
            continue
        meta = _skill_frontmatter(skill_md) if skill_md.exists() else {}
        name = meta.get("name") or path.name
        action_optional = folder in _SKILL_ACTION_OPTIONAL
        entries.append(
            {
                "folder": folder,
                "name": name,
                "canonical": _skill_canonical(folder),
                "description": meta.get("description", ""),
                "has_skill_md": skill_md.exists(),
                "has_action": action_py.exists(),
                "action_optional": action_optional,
                "python_files": len(list(path.glob("*.py"))),
            }
        )

    duplicate_canonical: list[dict[str, Any]] = []
    by_canonical: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        by_canonical[entry["canonical"]].append(entry["folder"])
    for canonical, folders in sorted(by_canonical.items()):
        if len(folders) > 1:
            duplicate_canonical.append({"canonical": canonical, "folders": sorted(folders)})

    missing_skill_md = sorted(entry["folder"] for entry in entries if not entry["has_skill_md"])
    missing_action = sorted(
        entry["folder"]
        for entry in entries
        if not entry["has_action"] and not entry.get("action_optional")
    )
    return {
        "present": True,
        "total": len(entries),
        "with_skill_md": sum(1 for entry in entries if entry["has_skill_md"]),
        "with_action": sum(1 for entry in entries if entry["has_action"]),
        "internal_alias_count": sum(1 for name in _SKILL_INTERNAL_ALIASES if (skills_root / name).exists()),
        "action_optional_count": sum(1 for entry in entries if entry.get("action_optional")),
        "missing_skill_md": missing_skill_md,
        "missing_action": missing_action,
        "duplicate_canonical": duplicate_canonical,
        "entries": entries,
    }


def _markdown_input_hints(skill_md: Path) -> list[str]:
    text = _read_text(skill_md, max_chars=50_000)
    if not text:
        return []
    hints: list[str] = []
    for match in re.finditer(r"(?:^|\s)(python(?:3)?\s+action\.py[^\n`]+)", text, flags=re.I):
        hints.append(match.group(1))
    capture = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lower()
            capture = any(
                key in heading
                for key in (
                    "usage",
                    "指令",
                    "payload",
                    "呼叫格式",
                    "參數",
                    "参数",
                    "line/dc",
                )
            )
            continue
        if not capture:
            continue
        if set(line) <= {"|", "-", " ", ":"}:
            continue
        if line.startswith("|") and "必填" not in line and "payload" not in line.lower():
            continue
        if (
            line.startswith(("-", "*"))
            or re.match(r"^\d+\.", line)
            or "必填" in line
            or "--task" in line
            or "action=" in line
            or "payload" in line.lower()
        ):
            hints.append(line.strip("`"))
    return _dedupe_limited(hints, limit=10)


def _argparse_input_hints(action_py: Path) -> list[str]:
    if not action_py.exists():
        return []
    try:
        tree = ast.parse(_read_text(action_py, max_chars=120_000), filename=str(action_py))
    except SyntaxError:
        return []
    hints: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        option_names = [_literal_string(arg) for arg in node.args]
        option_names = [name for name in option_names if name and name.startswith("-")]
        if not option_names:
            continue
        help_text = ""
        default_text = ""
        required = False
        choices_text = ""
        for keyword in node.keywords:
            if keyword.arg == "help":
                help_text = str(_literal_value(keyword.value) or "")
            elif keyword.arg == "default":
                default = _literal_value(keyword.value)
                if default not in (None, ""):
                    default_text = f"default={_format_default(default)}"
            elif keyword.arg == "required":
                required = bool(_literal_value(keyword.value))
            elif keyword.arg == "choices":
                choices = _literal_value(keyword.value)
                if isinstance(choices, (list, tuple, set)):
                    choices_text = "choices=" + ",".join(str(item) for item in choices)
        bits = ["/".join(option_names)]
        attrs = [item for item in (default_text, "required" if required else "", choices_text) if item]
        if attrs:
            bits.append("(" + "; ".join(attrs) + ")")
        if help_text:
            bits.append("- " + help_text)
        hints.append(" ".join(bits))
    return _dedupe_limited(hints, limit=12)


def _function_task_hints(action_py: Path) -> list[str]:
    if not action_py.exists():
        return []
    try:
        tree = ast.parse(_read_text(action_py, max_chars=160_000), filename=str(action_py))
    except SyntaxError:
        return []
    task_names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("task_"):
            task_names.append(node.name.removeprefix("task_"))
        elif node.name.startswith("cmd_"):
            task_names.append(node.name.removeprefix("cmd_"))
    if not task_names:
        return []
    preview = ", ".join(sorted(set(task_names))[:12])
    suffix = " ..." if len(set(task_names)) > 12 else ""
    return [f"task functions: {preview}{suffix}"]


def _infer_failure_categories(text: str, *, path_missing: bool = False) -> list[str]:
    low = text.lower()
    categories: set[str] = {"unknown"}
    checks = {
        "auth_required": (
            "auth_required",
            "oauth",
            "token",
            "api key",
            "credential",
            "scope",
            "授權",
            "憑證",
            "權限",
        ),
        "login_failed": (
            "login_failed",
            "sso_login_failed",
            "login",
            "password",
            "captcha",
            "登入",
            "密碼",
            "驗證碼",
        ),
        "path_missing": (
            "path_missing",
            "missing path",
            "no such file",
            "not found",
            "file path",
            "folder",
            "pdf",
            "檔案",
            "資料夾",
            "路徑",
        ),
        "external_service": (
            "portal",
            "gmail",
            "google",
            "ezlawyer",
            "selenium",
            "playwright",
            "chrome",
            "requests",
            "http",
            "db",
            "mysql",
            "mariadb",
            "nas",
            "synology",
            "司法院",
            "外部",
        ),
        "validation_failed": (
            "validation",
            "invalid",
            "required",
            "payload",
            "json",
            "unknown task",
            "格式",
            "必填",
            "參數",
            "参数",
            "案號",
        ),
    }
    for category, needles in checks.items():
        if any(needle in low for needle in needles):
            categories.add(category)
    if path_missing:
        categories.add("path_missing")
    return [category for category in GENERAL_ERROR_CATEGORIES if category in categories]


def _live_check_hint(action_py: Path, rel_action: str, combined_text: str) -> str:
    if not action_py.exists():
        return "static contract only: verify SKILL.md parses"
    low = combined_text.lower()
    if "self_test" in low or "self-test" in low:
        return f"python {rel_action} --task self_test"
    if "db_smoke" in low:
        return f"python {rel_action} --task db_smoke"
    if "argumentparser" in low or "--help" in low:
        return f"python {rel_action} --help"
    if "--task" in low:
        return f"python {rel_action} --task help"
    return f"python {rel_action}"


def _build_skill_contract(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    folder = str(entry.get("folder") or "")
    skill_dir = root / "skills" / folder
    skill_md = skill_dir / "SKILL.md"
    action_py = skill_dir / "action.py"
    rel_action = f"skills/{folder}/action.py"
    rel_skill = f"skills/{folder}/SKILL.md"
    skill_text = _read_text(skill_md, max_chars=50_000)
    action_text = _read_text(action_py, max_chars=90_000)
    input_hints = _dedupe_limited(
        _markdown_input_hints(skill_md)
        + _argparse_input_hints(action_py)
        + _function_task_hints(action_py),
        limit=14,
    )
    if not input_hints:
        input_hints = ["message text accepted by SkillRegistry subprocess dispatch"]
    canonical = str(entry.get("canonical") or _skill_canonical(folder))
    contract = {
        "name": canonical,
        "declared_name": str(entry.get("name") or canonical),
        "source": "skill_dir",
        "entrypoint": rel_action if action_py.exists() else rel_skill,
        "input_hints": input_hints,
        "failure_categories": _infer_failure_categories(
            "\n".join([canonical, skill_text, action_text]),
            path_missing=not action_py.exists(),
        ),
        "live_check_hint": _live_check_hint(action_py, rel_action, skill_text + "\n" + action_text),
        "is_core": canonical in _CORE_SKILL_CANONICALS,
    }
    return contract


def _extract_signature_hints(tree: ast.AST) -> dict[str, list[str]]:
    signatures: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = list(node.args.args)
        defaults = list(node.args.defaults)
        default_by_arg: dict[str, Any] = {}
        if defaults:
            for arg, default_node in zip(positional[-len(defaults) :], defaults):
                default_by_arg[arg.arg] = _literal_value(default_node)
        hints: list[str] = []
        for arg in positional + list(node.args.kwonlyargs):
            if arg.arg in {"self", "context", "tool_context"}:
                continue
            ann = _annotation_text(arg.annotation)
            default = default_by_arg.get(arg.arg, None)
            if arg in node.args.kwonlyargs:
                idx = node.args.kwonlyargs.index(arg)
                if idx < len(node.args.kw_defaults) and node.args.kw_defaults[idx] is not None:
                    default = _literal_value(node.args.kw_defaults[idx])
            bit = arg.arg + (f": {ann}" if ann else "")
            if arg.arg in default_by_arg or default is not None:
                default_repr = _format_default(default)
                if default_repr:
                    bit += f" (default={default_repr})"
            else:
                bit += " (required)"
            hints.append(bit)
        signatures[node.name] = _dedupe_limited(hints, limit=10)
    return signatures


def _discover_direct_handler_contracts(root: Path) -> list[dict[str, Any]]:
    path = root / "skills" / "skill_loader.py"
    text = _read_text(path, max_chars=80_000)
    if not text:
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    contracts: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_handler"
            and node.args
        ):
            continue
        name = _literal_string(node.args[0])
        if not name:
            continue
        aliases: list[str] = []
        for keyword in node.keywords:
            if keyword.arg == "aliases":
                value = _literal_value(keyword.value)
                if isinstance(value, (list, tuple)):
                    aliases = [str(item) for item in value if str(item or "").strip()]
        input_hints = ["message text from orchestrator semantic dispatch"]
        if aliases:
            input_hints.append("aliases: " + ", ".join(aliases))
        categories = _infer_failure_categories(name + " " + " ".join(aliases))
        if name in {"web_search", "judgment_search", "court_hearing", "stock_briefing"} and "external_service" not in categories:
            categories.insert(-1, "external_service")
        live_check = (
            "POST /search or /research on Tools API"
            if name == "web_search"
            else f"SkillRegistry.dispatch('{name}', message)"
        )
        contracts.append(
            {
                "name": name,
                "declared_name": name,
                "source": "direct_handler",
                "entrypoint": "skills/skill_loader.py::_register_direct_handlers",
                "input_hints": input_hints,
                "failure_categories": [category for category in GENERAL_ERROR_CATEGORIES if category in set(categories)],
                "live_check_hint": live_check,
                "aliases": aliases,
                "is_core": name in _CORE_DIRECT_HANDLERS,
            }
        )
    return sorted(contracts, key=lambda item: item["name"])


def _discover_api_tool_contracts(root: Path) -> list[dict[str, Any]]:
    path = root / "api" / "tools_api.py"
    text = _read_text(path, max_chars=220_000)
    if not text:
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    signatures = _extract_signature_hints(tree)
    contracts: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_callable"
            and node.args
        ):
            continue
        name = _literal_string(node.args[0])
        if not name:
            continue
        fn_name = ""
        if len(node.args) >= 2:
            fn = node.args[1]
            if isinstance(fn, ast.Name):
                fn_name = fn.id
            elif isinstance(fn, ast.Attribute):
                fn_name = fn.attr
        description = ""
        permission_tag = ""
        timeout_sec = 60
        metadata: dict[str, Any] = {}
        for keyword in node.keywords:
            if keyword.arg == "description":
                description = str(_literal_value(keyword.value) or "")
            elif keyword.arg == "permission_tag":
                permission_tag = str(_literal_value(keyword.value) or "")
            elif keyword.arg == "timeout_sec":
                raw_timeout = _literal_value(keyword.value)
                if isinstance(raw_timeout, int):
                    timeout_sec = raw_timeout
            elif keyword.arg == "metadata":
                raw_metadata = _literal_value(keyword.value)
                if isinstance(raw_metadata, dict):
                    metadata = {str(k): v for k, v in raw_metadata.items()}
        route = str(metadata.get("route") or "").strip()
        route_hint = f"POST {route} on Tools API" if route else f"ToolRegistry.execute('{name}', arguments)"
        categories = _infer_failure_categories(" ".join([name, description, route, permission_tag]))
        if "external_service" not in categories and name in {"search", "research", "fetch", "vision"}:
            categories.insert(-1, "external_service")
        if "validation_failed" not in categories:
            categories.insert(-1, "validation_failed")
        contracts.append(
            {
                "name": name,
                "declared_name": name,
                "source": "api_tool_registry",
                "entrypoint": "api/tools_api.py::_bootstrap_tool_registry",
                "callable": fn_name,
                "input_hints": signatures.get(fn_name) or ["JSON object arguments"],
                "failure_categories": [category for category in GENERAL_ERROR_CATEGORIES if category in set(categories)],
                "live_check_hint": route_hint,
                "permission_tag": permission_tag,
                "timeout_sec": timeout_sec,
                "metadata": metadata,
                "is_core": name in _CORE_API_TOOLS,
            }
        )
    return sorted(contracts, key=lambda item: item["name"])


def _discover_ops_contracts(root: Path, test_suites: dict[str, Any], cron_jobs: dict[str, Any]) -> list[dict[str, Any]]:
    script_keys: set[str] = {
        "scripts/ops/business_module_live_check.py",
        "scripts/ops/function_health_index.py",
        "scripts/ops/skill_realworld_smoke.py",
    }
    for suite in test_suites.get("suites") or []:
        for check in suite.get("checks") or []:
            for script in command_script_keys(check.get("command") or []):
                if script.startswith("scripts/ops/"):
                    script_keys.add(script)
    for job in cron_jobs.get("entries") or []:
        for script in job.get("scripts") or []:
            if script.startswith("scripts/ops/"):
                script_keys.add(script)
    contracts: list[dict[str, Any]] = []
    for script in sorted(script_keys):
        path = root / script
        if not path.exists():
            continue
        hints = _argparse_input_hints(path)
        if not hints:
            hints = ["CLI arguments; run --help when supported"]
        text = _read_text(path, max_chars=60_000)
        live_hint = f"python {script} --help" if "ArgumentParser" in text else f"python {script}"
        contracts.append(
            {
                "name": Path(script).stem,
                "declared_name": Path(script).stem,
                "source": "ops_script",
                "entrypoint": script,
                "input_hints": hints,
                "failure_categories": _infer_failure_categories(script + "\n" + text),
                "live_check_hint": live_hint,
                "is_core": Path(script).name in {"business_module_live_check.py", "function_health_index.py"},
            }
        )
    return contracts


def build_contract_summary(
    root: Path,
    *,
    skills: dict[str, Any],
    test_suites: dict[str, Any],
    cron_jobs: dict[str, Any],
) -> dict[str, Any]:
    skill_contracts = [
        _build_skill_contract(root, entry)
        for entry in skills.get("entries") or []
        if isinstance(entry, dict)
    ]
    direct_handler_contracts = _discover_direct_handler_contracts(root)
    api_tool_contracts = _discover_api_tool_contracts(root)
    ops_contracts = _discover_ops_contracts(root, test_suites, cron_jobs)
    observed_names = {
        *(item["name"] for item in skill_contracts),
        *(item["name"] for item in direct_handler_contracts),
        *(item["name"] for item in api_tool_contracts),
    }
    expected_core = sorted(_CORE_SKILL_CANONICALS | _CORE_DIRECT_HANDLERS | _CORE_API_TOOLS)
    missing_core = [name for name in expected_core if name not in observed_names]
    return {
        "version": "1.0",
        "failure_taxonomy": {"GeneralError": list(GENERAL_ERROR_CATEGORIES)},
        "summary": {
            "skill_contract_count": len(skill_contracts),
            "direct_handler_contract_count": len(direct_handler_contracts),
            "api_tool_contract_count": len(api_tool_contracts),
            "ops_entrypoint_contract_count": len(ops_contracts),
            "core_contract_count": len(expected_core) - len(missing_core),
            "missing_core_contracts": missing_core,
        },
        "skills": sorted(skill_contracts, key=lambda item: item["name"]),
        "direct_handlers": direct_handler_contracts,
        "api_tools": api_tool_contracts,
        "ops_entrypoints": ops_contracts,
    }


def _split_command(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(part) for part in command]
    text = str(command or "")
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _command_text(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _resolve_output_path(raw: str, root: Path, runtime_dir: Path) -> Path:
    text = str(raw).replace("{root}", str(root))
    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    if text.startswith(".runtime/"):
        return runtime_dir / text[len(".runtime/") :]
    if text.startswith("static/"):
        return root / text
    if text.startswith("runtime/"):
        return runtime_dir / text[len("runtime/") :]
    return root / text


def extract_json_outputs(command: Any, root: Path, runtime_dir: Path) -> list[str]:
    parts = _split_command(command)
    outputs: list[str] = []
    for index, part in enumerate(parts):
        if part in _JSON_OUT_FLAGS and index + 1 < len(parts):
            outputs.append(_rel(_resolve_output_path(parts[index + 1], root, runtime_dir), root))
            continue
        for flag in _JSON_OUT_FLAGS:
            prefix = flag + "="
            if part.startswith(prefix):
                outputs.append(_rel(_resolve_output_path(part[len(prefix) :], root, runtime_dir), root))
    return sorted(set(outputs))


def command_script_keys(command: Any) -> list[str]:
    text = _command_text(command)
    return sorted(set(match.group(1) for match in _SCRIPT_RE.finditer(text)))


def _cron_expected_interval_hours(expr: str) -> float | None:
    parts = str(expr or "").split()
    if len(parts) != 5:
        return None
    minute, hour, day_of_month, _month, day_of_week = parts
    if day_of_month not in {"*", "?"}:
        return 31 * 24.0
    if day_of_week not in {"*", "?"}:
        return 7 * 24.0
    if hour in {"*", "*/1"}:
        if minute.startswith("*/"):
            try:
                return max(1.0 / 60.0, int(minute[2:]) / 60.0)
            except Exception:
                return 1.0
        return 1.0
    if hour.startswith("*/"):
        try:
            return float(max(1, int(hour[2:])))
        except Exception:
            return 1.0
    if "," in hour:
        slots = [part for part in hour.split(",") if part.strip()]
        if slots:
            return max(1.0, 24.0 / len(slots))
    return 24.0


def _cron_stale_threshold_hours(expr: str) -> float:
    interval = _cron_expected_interval_hours(expr)
    if interval is None:
        return 72.0
    return max(2.0, round(interval * 2.5 + 6.0, 3))


def _infer_payload_ok(data: Any) -> tuple[bool | None, str]:
    if isinstance(data, dict):
        for key in ("ok", "success", "passed"):
            if isinstance(data.get(key), bool):
                return bool(data[key]), key
        failed = data.get("failed")
        if isinstance(failed, int):
            return failed == 0, "failed"
        failures = data.get("failures")
        if isinstance(failures, list):
            return len(failures) == 0, "failures"
        errors = data.get("errors")
        if isinstance(errors, int):
            return errors == 0, "errors"
        status = str(data.get("status") or "").strip().lower()
        if status in _FAILED_STATUS:
            return False, "status"
        if status in _OK_STATUS:
            return True, "status"
    return None, "unknown"


def _payload_timestamp(data: Any) -> datetime | None:
    if not isinstance(data, dict):
        return None
    for key in ("generated_at", "timestamp", "created_at", "updated_at", "last_run", "last_success_at"):
        dt = _parse_dt(data.get(key))
        if dt:
            return dt
    return None


def _health_file_candidate(path: Path, base: Path) -> bool:
    name = path.name.lower()
    if name.startswith("function_health_index_"):
        return False
    parts = {part.lower() for part in path.parts}
    if "latest" in name or "health" in name:
        return True
    if name == "cron_state.json":
        return True
    if "test_reports" in parts and name.endswith(".json"):
        return True
    return False


def discover_runtime_health_files(root: Path, runtime_dir: Path, *, include_static: bool = True) -> list[Path]:
    bases = [runtime_dir]
    if include_static:
        bases.append(root / "static")
    out: list[Path] = []
    seen: set[Path] = set()
    for base in bases:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.json")):
            if path in seen:
                continue
            try:
                rel = path.relative_to(base)
            except Exception:
                rel = path
            if _has_skip_part(rel):
                continue
            if _health_file_candidate(path, base):
                out.append(path)
                seen.add(path)
    return out


def evaluate_health_file(path: Path, root: Path, now: datetime, max_age_hours: float) -> dict[str, Any]:
    data, error = _read_json(path, None)
    mtime = _mtime_dt(path)
    timestamp = _payload_timestamp(data) or mtime
    age = _age_hours(timestamp, now)
    ok, ok_source = _infer_payload_ok(data)
    status = "ok"
    reason = ""
    if error:
        status = "missing" if error == "missing" else "failed"
        reason = error
    elif ok is False:
        status = "failed"
        reason = f"{ok_source}=false"
    elif max_age_hours > 0 and age is not None and age > max_age_hours:
        status = "stale"
        reason = f"age_hours>{max_age_hours:g}"
    elif ok is None:
        status = "observed"
        reason = "no explicit ok/success/failed contract"

    return {
        "path": _rel(path, root),
        "status": status,
        "ok": status in {"ok", "observed"},
        "reason": reason,
        "contract": ok_source,
        "timestamp": timestamp.isoformat() if timestamp else "",
        "age_hours": age,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


def _expected_health_paths_from_matrix(matrix: dict[str, Any], root: Path, runtime_dir: Path) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    suites = matrix.get("suites") if isinstance(matrix, dict) else {}
    if not isinstance(suites, dict):
        return expected
    for suite_name, suite in suites.items():
        checks = suite.get("checks") if isinstance(suite, dict) else []
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            command = check.get("command") or []
            if "scripts/ops/function_health_index.py" in _command_text(command):
                continue
            for out in extract_json_outputs(command, root, runtime_dir):
                expected.append(
                    {
                        "path": out,
                        "owner": f"matrix:{suite_name}:{check.get('id') or check.get('name') or 'unnamed'}",
                        "source": "matrix_json_out",
                    }
                )
    return expected


def discover_test_suites(matrix_path: Path, root: Path, runtime_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    matrix, error = _read_json(matrix_path, {})
    suites = matrix.get("suites") if isinstance(matrix, dict) else {}
    suite_entries: list[dict[str, Any]] = []
    check_count = 0
    if isinstance(suites, dict):
        for name, suite in sorted(suites.items()):
            checks = suite.get("checks") if isinstance(suite, dict) else []
            if not isinstance(checks, list):
                checks = []
            check_count += len(checks)
            suite_entries.append(
                {
                    "name": name,
                    "description": str(suite.get("description") or "") if isinstance(suite, dict) else "",
                    "check_count": len(checks),
                    "checks": [
                        {
                            "id": str(check.get("id") or check.get("name") or "unnamed"),
                            "name": str(check.get("name") or check.get("id") or "unnamed"),
                            "timeout_sec": int(check.get("timeout_sec") or 300),
                            "json_outputs": extract_json_outputs(check.get("command") or [], root, runtime_dir),
                            "requires_env": check.get("require_env") or [],
                        }
                        for check in checks
                        if isinstance(check, dict)
                    ],
                }
            )

    return (
        {
            "matrix": _rel(matrix_path, root),
            "present": not bool(error),
            "error": "" if error == "missing" else error,
            "total": len(suite_entries),
            "check_count": check_count,
            "suites": suite_entries,
        },
        _expected_health_paths_from_matrix(matrix, root, runtime_dir),
    )


def discover_cron_jobs(root: Path, runtime_dir: Path, now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    runtime_root = runtime_dir.parent
    runtime_cron = runtime_root / "cron_jobs.json"
    cron_path = runtime_cron if runtime_cron.exists() and runtime_root.resolve() != root.resolve() else root / "cron_jobs.json"
    jobs, error = _read_json(cron_path, [])
    state_path = runtime_dir / "cron_state.json"
    state, state_error = _read_json(state_path, {})
    entries: list[dict[str, Any]] = []
    missing_state: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    expected_health: list[dict[str, Any]] = [
        {"path": _rel(state_path, root), "owner": "cron_state", "source": "cron_state"}
    ]

    if not isinstance(jobs, list):
        jobs = []
        error = error or "cron_jobs.json is not a list"
    if not isinstance(state, dict):
        state = {}
        state_error = state_error or "cron_state.json is not an object"

    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "")
        enabled = bool(job.get("enabled", True))
        command = job.get("command") or ""
        json_outputs = extract_json_outputs(command, root, runtime_dir)
        for out in json_outputs:
            expected_health.append({"path": out, "owner": f"cron:{job_id}", "source": "cron_json_out"})
        state_item = state.get(job_id) if isinstance(state, dict) else None
        if not isinstance(state_item, dict):
            state_item = {}
        last_run = _parse_dt(state_item.get("last_run")) or _parse_dt(job.get("last_run"))
        age = _age_hours(last_run, now)
        threshold = _cron_stale_threshold_hours(str(job.get("cron") or ""))
        state_ok, state_contract = _infer_payload_ok(state_item)
        entry = {
            "id": job_id,
            "enabled": enabled,
            "cron": str(job.get("cron") or ""),
            "description": str(job.get("desc") or ""),
            "command_kind": "macro" if str(command).strip().startswith("@MAGI") else "script",
            "scripts": command_script_keys(command),
            "json_outputs": json_outputs,
            "last_run": last_run.isoformat() if last_run else "",
            "age_hours": age,
            "stale_threshold_hours": threshold,
            "state_present": bool(state_item),
        }
        entries.append(entry)
        if not enabled:
            continue
        if not state_item and state_error != "missing":
            missing_state.append({"id": job_id, "reason": "missing cron_state entry"})
            continue
        if not state_item and state_error == "missing":
            missing_state.append({"id": job_id, "reason": "cron_state.json missing"})
            continue
        if state_ok is False:
            failed.append({"id": job_id, "reason": f"{state_contract}=false"})
        if last_run is None:
            missing_state.append({"id": job_id, "reason": "missing last_run"})
        elif age is not None and age > threshold:
            stale.append({"id": job_id, "age_hours": age, "threshold_hours": threshold})

    return (
        {
            "source": _rel(cron_path, root),
            "present": not bool(error),
            "error": "" if error == "missing" else error,
            "state": _rel(state_path, root),
            "state_present": not bool(state_error),
            "state_error": "" if state_error == "missing" else state_error,
            "total": len(entries),
            "enabled": sum(1 for entry in entries if entry["enabled"]),
            "missing_state": missing_state,
            "stale": stale,
            "failed": failed,
            "entries": entries,
        },
        expected_health,
    )


def _dedupe_expected(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    owners: dict[str, set[str]] = defaultdict(set)
    sources: dict[str, set[str]] = defaultdict(set)
    for item in paths:
        path = str(item.get("path") or "")
        if not path:
            continue
        owners[path].add(str(item.get("owner") or "unknown"))
        sources[path].add(str(item.get("source") or "unknown"))
    return [
        {"path": path, "owners": sorted(owners[path]), "sources": sorted(sources[path])}
        for path in sorted(owners)
    ]


def _observed_issue(item: dict[str, Any]) -> dict[str, Any]:
    issue = {"path": item["path"], "reason": item["reason"], "contract": item["contract"]}
    if item.get("age_hours") is not None:
        issue["age_hours"] = item.get("age_hours")
    return issue


def _path_from_report_path(raw: str, root: Path) -> Path:
    path = Path(str(raw or "")).expanduser()
    if path.is_absolute():
        return path
    return root / path


def _lower_patterns(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip().lower() for value in values if str(value).strip()]


def _matches_patterns(text: Any, patterns: list[str]) -> bool:
    haystack = str(text or "").lower()
    return any(pattern and pattern in haystack for pattern in patterns)


def _coerce_item_id(name: str, item: dict[str, Any]) -> str:
    for key in ("id", "name", "task", "check", "label"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return name


def _iter_named_payload_items(data: Any) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, dict[str, Any]]] = []

    for key in ("results", "checks", "items"):
        collection = data.get(key)
        if isinstance(collection, list):
            for idx, item in enumerate(collection):
                if isinstance(item, dict):
                    name = _coerce_item_id(str(idx), item)
                    out.append((name, item))
        elif isinstance(collection, dict):
            for name, item in collection.items():
                if isinstance(item, dict):
                    merged = dict(item)
                    merged.setdefault("id", str(name))
                    out.append((str(name), merged))

    details = data.get("details")
    if isinstance(details, dict):
        steps = details.get("steps")
        if isinstance(steps, dict):
            for name, item in steps.items():
                if isinstance(item, dict):
                    merged = dict(item)
                    merged.setdefault("id", str(name))
                    out.append((str(name), merged))

    return out


def _result_status(item: dict[str, Any]) -> tuple[str, bool | None, str]:
    if bool(item.get("skipped")):
        return "skipped", True, "skipped"
    ok, source = _infer_payload_ok(item)
    if ok is None and isinstance(item.get("returncode"), int):
        ok = int(item.get("returncode")) == 0
        source = "returncode"
    if ok is True:
        return "ok", True, source
    if ok is False:
        return "failed", False, source
    status = str(item.get("status") or "").strip().lower()
    if status:
        return status, None, "status"
    return "observed", None, "unknown"


def _runtime_artifact_records(root: Path, health_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in health_files:
        rel_path = str(item.get("path") or "")
        if not rel_path or rel_path in seen:
            continue
        seen.add(rel_path)
        path = _path_from_report_path(rel_path, root)
        data, error = _read_json(path, None)
        records.append(
            {
                "path": rel_path,
                "abs_path": str(path),
                "exists": path.exists(),
                "status": item.get("status") or ("missing" if error == "missing" else "unknown"),
                "ok": bool(item.get("ok")),
                "reason": item.get("reason") or error,
                "timestamp": item.get("timestamp") or "",
                "age_hours": item.get("age_hours"),
                "payload": data,
                "items": _iter_named_payload_items(data),
            }
        )
    return records


def _unit_test_snapshot(contract: dict[str, Any], root: Path, now: datetime) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    latest_dt: datetime | None = None
    latest_source = ""
    for raw in contract.get("unit_tests") or []:
        rel = str(raw)
        path = root / rel
        exists = path.exists()
        timestamp = _mtime_dt(path) if exists else None
        if timestamp and (latest_dt is None or timestamp > latest_dt):
            latest_dt = timestamp
            latest_source = rel
        entries.append(
            {
                "path": rel,
                "exists": exists,
                "timestamp": timestamp.isoformat() if timestamp else "",
                "age_hours": _age_hours(timestamp, now),
            }
        )
    coverage_count = sum(1 for entry in entries if entry["exists"])
    return {
        "status": "covered" if coverage_count else "missing",
        "source": latest_source,
        "timestamp": latest_dt.isoformat() if latest_dt else "",
        "age_hours": _age_hours(latest_dt, now),
        "evidence_count": coverage_count,
        "tests": entries,
    }


def _artifact_timestamp(record: dict[str, Any]) -> datetime | None:
    return _parse_dt(record.get("timestamp")) or _mtime_dt(Path(str(record.get("abs_path") or "")))


def _live_check_snapshot(
    contract: dict[str, Any],
    artifacts: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    patterns = _lower_patterns(contract.get("live_patterns") or [])
    patterns.extend([str(contract.get("id") or "").lower(), str(contract.get("name") or "").lower()])
    patterns = sorted({pattern for pattern in patterns if pattern})
    candidates: list[dict[str, Any]] = []

    for record in artifacts:
        path_text = str(record.get("path") or "")
        payload = record.get("payload")
        record_dt = _artifact_timestamp(record)
        matched_item = False
        for item_name, item in record.get("items") or []:
            item_text = " ".join(
                str(part or "")
                for part in (
                    item_name,
                    item.get("id"),
                    item.get("name"),
                    item.get("task"),
                    item.get("message"),
                    item.get("status"),
                )
            )
            if not _matches_patterns(item_text, patterns):
                continue
            status, ok, source = _result_status(item)
            item_dt = _payload_timestamp(item) or record_dt
            candidates.append(
                {
                    "status": status,
                    "ok": ok if ok is not None else status not in {"failed", "error"},
                    "source": path_text,
                    "check_id": item_name,
                    "timestamp": item_dt.isoformat() if item_dt else "",
                    "age_hours": _age_hours(item_dt, now),
                    "evidence": source,
                    "detail": str(item.get("message") or item.get("detail") or item.get("summary") or "")[:180],
                }
            )
            matched_item = True

        payload_text = ""
        if isinstance(payload, dict):
            payload_text = " ".join(str(payload.get(key) or "") for key in ("id", "name", "task", "message", "status"))
        if not matched_item and (_matches_patterns(path_text, patterns) or _matches_patterns(payload_text, patterns)):
            candidates.append(
                {
                    "status": str(record.get("status") or "unknown"),
                    "ok": bool(record.get("ok")),
                    "source": path_text,
                    "check_id": "",
                    "timestamp": record.get("timestamp") or "",
                    "age_hours": record.get("age_hours"),
                    "evidence": str(record.get("reason") or "artifact"),
                    "detail": "",
                }
            )

    candidates.sort(key=lambda item: _parse_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    if candidates:
        latest = dict(candidates[0])
        latest["matched_evidence_count"] = len(candidates)
        return latest
    return {
        "status": "missing",
        "ok": False,
        "source": "",
        "check_id": "",
        "timestamp": "",
        "age_hours": None,
        "evidence": "no matching runtime/live artifact",
        "detail": "",
        "matched_evidence_count": 0,
    }


def _token_report_records(artifacts: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in artifacts:
        path_text = str(record.get("path") or "")
        if "token_health" not in path_text.lower() and "smoke_token_health" not in path_text.lower():
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        dt = _artifact_timestamp(record)
        records.append(
            {
                "source": path_text,
                "timestamp": dt.isoformat() if dt else "",
                "age_hours": _age_hours(dt, now),
                "payload": payload,
                "items": _iter_named_payload_items(payload),
            }
        )
    return records


def _token_status_hint(
    contract: dict[str, Any],
    token_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    names = [str(name) for name in contract.get("token_names") or [] if str(name)]
    auth_hint = str(contract.get("auth_hint") or "").strip()
    if not names:
        return {
            "status": "external_auth" if auth_hint else "not_required",
            "hint": auth_hint or "這項功能不需要專屬 OAuth/API token。",
            "source": "",
            "checks": [],
        }

    latest_by_name: dict[str, dict[str, Any]] = {}
    for report in token_reports:
        report_dt = _parse_dt(report.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
        for item_name, item in report.get("items") or []:
            name = str(item.get("name") or item.get("id") or item_name or "")
            if name not in names:
                continue
            current = latest_by_name.get(name)
            current_dt = _parse_dt(current.get("timestamp")) if current else None
            if current is None or current_dt is None or report_dt >= current_dt:
                latest_by_name[name] = {
                    "name": name,
                    "status": str(item.get("status") or ""),
                    "ok": bool(item.get("ok")),
                    "message": str(item.get("message") or "")[:160],
                    "next_action": str(item.get("next_action") or "")[:180],
                    "source": str(report.get("source") or ""),
                    "timestamp": str(report.get("timestamp") or ""),
                }

    missing_names = [name for name in names if name not in latest_by_name]
    checks = [latest_by_name[name] for name in names if name in latest_by_name]
    failures = [item for item in checks if not bool(item.get("ok"))]
    auth_statuses = {
        "auth_required",
        "expired",
        "expiring_soon",
        "missing_scope",
        "missing_token",
        "missing_key",
        "account_mismatch",
        "invalid_token_file",
    }
    if failures:
        status = "needs_auth" if any(str(item.get("status") or "") in auth_statuses for item in failures) else "attention"
        hint = "需要處理授權或 token：" + "、".join(f"{item['name']}={item.get('status') or 'unknown'}" for item in failures[:4])
    elif checks and not missing_names:
        if all(str(item.get("status") or "") == "skipped" for item in checks):
            status = "optional_or_disabled"
            hint = "相關 token 目前是 optional/skipped，代表服務未啟用或不是此環境必需。"
        else:
            status = "ok"
            hint = "相關 token health 最近回報可用。"
    elif token_reports:
        status = "unknown"
        hint = "token health report 存在，但未涵蓋：" + "、".join(missing_names)
    else:
        status = "unknown"
        hint = "尚未找到 token health report；請先跑 scripts/ops/token_health_check.py。"
    return {
        "status": status,
        "hint": hint,
        "source": checks[0]["source"] if checks else "",
        "checks": checks,
        "missing_checks": missing_names,
    }


def _derive_core_status(unit: dict[str, Any], live: dict[str, Any], token: dict[str, Any]) -> str:
    token_status = str(token.get("status") or "")
    live_status = str(live.get("status") or "")
    if token_status in {"needs_auth", "attention"}:
        return "needs_auth"
    if live_status in {"failed", "error", "down", "not_ready", "unhealthy"}:
        return "needs_attention"
    if live_status == "stale":
        return "stale_live_check"
    if live_status in {"ok", "observed", "skipped"} or bool(live.get("ok")):
        return "verified_live"
    if unit.get("status") == "covered":
        return "unit_covered_pending_live"
    return "unknown"


def build_intelligence_snapshot(
    *,
    root: Path,
    runtime_dir: Path,
    now: datetime,
    health_files: list[dict[str, Any]],
    test_suites: dict[str, Any],
) -> dict[str, Any]:
    artifacts = _runtime_artifact_records(root, health_files)
    token_reports = _token_report_records(artifacts, now)
    functions: list[dict[str, Any]] = []
    for contract in CORE_FUNCTION_CONTRACTS:
        unit = _unit_test_snapshot(contract, root, now)
        live = _live_check_snapshot(contract, artifacts, now)
        token = _token_status_hint(contract, token_reports)
        status = _derive_core_status(unit, live, token)
        functions.append(
            {
                "id": contract["id"],
                "name": contract["name"],
                "status": status,
                "user_summary": contract["user_summary"],
                "entry_points": list(contract.get("entry_points") or []),
                "manual_commands": list(contract.get("manual_commands") or []),
                "manual_section_hint": contract["manual_section_hint"],
                "last_unit_test": unit,
                "last_live_check": live,
                "token_status_hint": token,
            }
        )

    status_counts: dict[str, int] = defaultdict(int)
    for item in functions:
        status_counts[str(item.get("status") or "unknown")] += 1

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "root": str(root),
        "runtime_dir": _rel(runtime_dir, root),
        "sources": {
            "test_matrix": test_suites.get("matrix", ""),
            "runtime_artifact_count": len(artifacts),
            "token_report_count": len(token_reports),
            "contract_count": len(CORE_FUNCTION_CONTRACTS),
        },
        "summary": {
            "core_function_count": len(functions),
            "status_counts": dict(sorted(status_counts.items())),
            "live_verified_count": status_counts.get("verified_live", 0),
            "auth_attention_count": sum(1 for item in functions if item.get("status") == "needs_auth"),
        },
        "core_functions": functions,
    }


def build_index(
    *,
    root: Path = MAGI_ROOT,
    matrix_path: Path | None = None,
    runtime_dir: Path | None = None,
    now: datetime | None = None,
    max_health_age_hours: float = 72.0,
    include_static: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    matrix_path = (matrix_path or root / "config" / "test_matrix.json").resolve()
    runtime_dir = (runtime_dir or default_runtime_dir(root)).resolve()
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    api_routes = discover_api_routes(root)
    skills = discover_skills(root)
    test_suites, expected_from_matrix = discover_test_suites(matrix_path, root, runtime_dir)
    cron_jobs, expected_from_cron = discover_cron_jobs(root, runtime_dir, now)
    contracts = build_contract_summary(
        root,
        skills=skills,
        test_suites=test_suites,
        cron_jobs=cron_jobs,
    )
    expected = _dedupe_expected(expected_from_matrix + expected_from_cron)

    health_paths = discover_runtime_health_files(root, runtime_dir, include_static=include_static)
    health_by_rel = {_rel(path, root): path for path in health_paths}
    for item in expected:
        rel_path = str(item["path"])
        if rel_path not in health_by_rel:
            health_by_rel[rel_path] = root / rel_path

    health_files = [
        evaluate_health_file(path, root, now, max_health_age_hours)
        for _rel_path, path in sorted(health_by_rel.items())
    ]
    intelligence_snapshot = build_intelligence_snapshot(
        root=root,
        runtime_dir=runtime_dir,
        now=now,
        health_files=health_files,
        test_suites=test_suites,
    )
    expected_paths = {str(item["path"]) for item in expected}
    missing = [
        {
            "path": item["path"],
            "owners": item["owners"],
            "sources": item["sources"],
            "reason": "expected health file missing",
        }
        for item in expected
        if not (root / str(item["path"])).exists()
    ]
    failed = [
        {"path": item["path"], "reason": item["reason"], "contract": item["contract"]}
        for item in health_files
        if item["status"] == "failed" and item["path"] in expected_paths
    ]
    stale = [
        {"path": item["path"], "age_hours": item["age_hours"], "reason": item["reason"]}
        for item in health_files
        if item["status"] == "stale" and item["path"] in expected_paths
    ]
    observed_failed = [
        _observed_issue(item)
        for item in health_files
        if item["status"] == "failed"
        and item["path"] not in expected_paths
        and (item.get("age_hours") is None or float(item.get("age_hours") or 0) <= max_health_age_hours)
    ]
    archived_observed_failed = [
        _observed_issue(item)
        for item in health_files
        if item["status"] == "failed"
        and item["path"] not in expected_paths
        and item.get("age_hours") is not None
        and float(item.get("age_hours") or 0) > max_health_age_hours
    ]
    archived_observed_stale = [
        {"path": item["path"], "age_hours": item["age_hours"], "reason": item["reason"]}
        for item in health_files
        if item["status"] == "stale" and item["path"] not in expected_paths
    ]
    missing.extend(
        {"path": item["path"], "owners": [], "sources": ["scan"], "reason": item["reason"]}
        for item in health_files
        if item["status"] == "missing" and item["path"] not in expected_paths
    )

    failed.extend({"path": f"cron:{item['id']}", "reason": item["reason"], "contract": "cron_state"} for item in cron_jobs["failed"])
    stale.extend({"path": f"cron:{item['id']}", "age_hours": item["age_hours"], "reason": "cron last_run stale"} for item in cron_jobs["stale"])
    missing.extend(
        {"path": f"cron:{item['id']}", "owners": [f"cron:{item['id']}"], "sources": ["cron_state"], "reason": item["reason"]}
        for item in cron_jobs["missing_state"]
    )

    health_ok = not failed and not stale and not missing
    skill_issue_count = (
        len(skills.get("missing_skill_md") or [])
        + len(skills.get("missing_action") or [])
        + len(skills.get("duplicate_canonical") or [])
    )
    report = {
        "ok": health_ok,
        "generated_at": now.isoformat(),
        "root": str(root),
        "matrix": _rel(matrix_path, root),
        "runtime_dir": _rel(runtime_dir, root),
        "summary": {
            "api_route_count": api_routes["total"],
            "api_route_domain_count": len(api_routes["domains"]),
            "skill_count": skills["total"],
            "skill_issue_count": skill_issue_count,
            "cron_job_count": cron_jobs["total"],
            "enabled_cron_job_count": cron_jobs["enabled"],
            "test_suite_count": test_suites["total"],
            "test_check_count": test_suites["check_count"],
            "runtime_health_file_count": len(health_files),
            "contract_skill_count": contracts["summary"]["skill_contract_count"],
            "contract_api_tool_count": contracts["summary"]["api_tool_contract_count"],
            "contract_missing_core_count": len(contracts["summary"]["missing_core_contracts"]),
            "failed_health_count": len(failed),
            "stale_health_count": len(stale),
            "missing_health_count": len(missing),
            "observed_failed_health_count": len(observed_failed),
            "observed_stale_health_count": 0,
            "archived_runtime_artifact_count": len(archived_observed_failed) + len(archived_observed_stale),
        },
        "api_routes": api_routes,
        "skills": skills,
        "contracts": contracts,
        "cron_jobs": cron_jobs,
        "test_suites": test_suites,
        "intelligence_snapshot": intelligence_snapshot,
        "runtime_health": {
            "max_age_hours": max_health_age_hours,
            "expected": expected,
            "files": health_files,
            "failed": failed,
            "stale": stale,
            "missing": missing,
            "observed_failed": observed_failed,
            "observed_stale": [],
            "artifact_hygiene": {
                "archived_observed_failed_count": len(archived_observed_failed),
                "archived_observed_stale_count": len(archived_observed_stale),
                "archived_observed_failed": archived_observed_failed[:30],
                "archived_observed_stale": archived_observed_stale[:30],
            },
        },
        "health": {
            "ok": health_ok,
            "failed": failed,
            "stale": stale,
            "missing": missing,
        },
    }
    return report


def _print_human_summary(report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    print(
        "function_health_index "
        f"ok={report.get('ok')} "
        f"routes={summary.get('api_route_count', 0)} "
        f"skills={summary.get('skill_count', 0)} "
        f"contracts={summary.get('contract_skill_count', 0)}/{summary.get('contract_api_tool_count', 0)} "
        f"cron={summary.get('enabled_cron_job_count', 0)}/{summary.get('cron_job_count', 0)} "
        f"suites={summary.get('test_suite_count', 0)} "
        f"health_files={summary.get('runtime_health_file_count', 0)} "
        f"failed={summary.get('failed_health_count', 0)} "
        f"stale={summary.get('stale_health_count', 0)} "
        f"missing={summary.get('missing_health_count', 0)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a public-safe MAGI function health index.")
    parser.add_argument("--root", default=str(MAGI_ROOT), help="MAGI repository root.")
    parser.add_argument("--matrix", default="", help="Path to config/test_matrix.json.")
    parser.add_argument("--runtime-dir", default="", help="Runtime health directory; defaults to <root>/.runtime.")
    parser.add_argument("--json-out", default="", help="Write the full index JSON to this path.")
    parser.add_argument(
        "--snapshot-out",
        default="",
        help="Write only the MAGI health/intelligence snapshot JSON to this path.",
    )
    parser.add_argument("--max-health-age-hours", type=float, default=72.0, help="Mark health files older than this as stale; <=0 disables.")
    parser.add_argument("--no-static", action="store_true", help="Do not scan static/*latest*.json health artifacts.")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON.")
    parser.add_argument("--summary", action="store_true", help="Print a one-line summary before JSON.")
    parser.add_argument("--fail-on-health", action="store_true", help="Return 1 when failed/stale/missing health is found.")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    matrix = Path(args.matrix).expanduser().resolve() if args.matrix else root / "config" / "test_matrix.json"
    runtime_dir = Path(args.runtime_dir).expanduser().resolve() if args.runtime_dir else default_runtime_dir(root)
    report = build_index(
        root=root,
        matrix_path=matrix,
        runtime_dir=runtime_dir,
        max_health_age_hours=args.max_health_age_hours,
        include_static=not args.no_static,
    )

    payload = json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2, sort_keys=False)
    if args.json_out:
        out = Path(args.json_out).expanduser()
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
    if args.snapshot_out:
        snapshot_out = Path(args.snapshot_out).expanduser()
        if not snapshot_out.is_absolute():
            snapshot_out = root / snapshot_out
        snapshot_out.parent.mkdir(parents=True, exist_ok=True)
        snapshot_payload = json.dumps(
            report["intelligence_snapshot"],
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=False,
        )
        snapshot_out.write_text(snapshot_payload + "\n", encoding="utf-8")
    if args.summary:
        _print_human_summary(report)
    print(payload)
    return 1 if args.fail_on_health and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
