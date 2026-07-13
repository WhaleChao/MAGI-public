"""
Dashboard / Intel / MAGI management page routes
===============================================

First modularization slice for the page layer that was previously embedded in
`api/server.py`.

This blueprint keeps the existing behavior for:
  - /static/worldmonitor_reports -> /intel
  - /worldmonitor -> /intel
  - /intel -> worldmonitor report index
  - /dashboard
  - /status
  - /dashboard/nerv (legacy compatibility)
  - /magi-adjust

The module is intentionally dependency-light and does not import server.py.
"""

from __future__ import annotations

import logging
import json
import html
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, logout_user

import requests as _requests

dashboard_pages_bp = Blueprint("dashboard_pages", __name__)

_MAGI_ROOT = Path(__file__).resolve().parents[2]
_WORLDMONITOR_REPORT_DIR = _MAGI_ROOT / "static" / "worldmonitor_reports"


def _is_mobile_app_request() -> bool:
    requested_with = (request.headers.get("X-Requested-With") or "").strip().lower()
    user_agent = (request.headers.get("User-Agent") or "").lower()
    if requested_with == "tw.local.magi.mobile":
        return True
    if "capacitor" in user_agent:
        return True
    if "; wv" in user_agent or " version/4.0 chrome/" in user_agent:
        return True
    return "mobile" in user_agent and ("safari" in user_agent or "chrome" in user_agent)


def _maybe_force_mobile_app_login():
    if not _is_mobile_app_request():
        return None
    if session.get("magi_mobile_app_auth_at"):
        return None
    try:
        logout_user()
    except Exception:
        logging.getLogger(__name__).debug("mobile app reauth logout cleanup failed", exc_info=True)
    session.clear()
    return redirect(url_for("login", next="/mobile", mobile_app="1"))


@dashboard_pages_bp.before_request
def _force_mobile_app_reauth_before_dashboard_page():
    if request.endpoint == "dashboard_pages.mobile_manifest":
        return None
    if current_user.is_authenticated:
        return _maybe_force_mobile_app_login()
    return None


def _strip_trailing_dot(value: str) -> str:
    return str(value or "").strip().rstrip(".")


def _load_tailscale_status() -> dict:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return data if isinstance(data, dict) else {}
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 59, exc_info=True)
    return {}


def _load_tailscale_serve_url() -> str:
    try:
        result = subprocess.run(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        data = json.loads(result.stdout)
        web = data.get("Web") if isinstance(data, dict) else {}
        if not isinstance(web, dict):
            return ""
        for host, config in web.items():
            if isinstance(config, dict) and config.get("Handlers"):
                host = _strip_trailing_dot(str(host).split(":")[0])
                return f"https://{host}" if host else ""
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 82, exc_info=True)
    return ""


def _build_mobile_app_config() -> dict:
    status = _load_tailscale_status()
    self_node = status.get("Self") if isinstance(status, dict) else {}
    dns_name = _strip_trailing_dot((self_node or {}).get("DNSName") or "")
    ips = (self_node or {}).get("TailscaleIPs") or []
    tailscale_ip = str(ips[0]) if ips else ""
    configured_url = (
        os.environ.get("MAGI_MOBILE_BASE_URL")
        or os.environ.get("MAGI_TAILSCALE_URL")
        or _load_tailscale_serve_url()
        or (f"https://{dns_name}" if dns_name else "")
        or (f"http://{tailscale_ip}:5002" if tailscale_ip else "")
        or "http://127.0.0.1:5002"
    ).rstrip("/")
    routes = [
        {"label": "MAGI", "path": "/golem", "kind": "core"},
        {"label": "Paperclip", "path": "/osc", "kind": "core"},
        {"label": "全球新聞網", "path": "/intel", "kind": "info"},
        {"label": "研究", "path": "/research", "kind": "info"},
        {"label": "MAGI 調整", "path": "/magi-adjust", "kind": "admin"},
        {"label": "手機後台", "path": "/mobile-admin", "kind": "admin"},
    ]
    return {
        "app_name": "MAGI Mobile",
        "base_url": configured_url,
        "tailscale_dns": dns_name,
        "tailscale_ip": tailscale_ip,
        "tailscale_online": bool((self_node or {}).get("Online")),
        "routes": routes,
        "android_package": "tw.local.magi.mobile",
        "ios_bundle_id": "tw.local.magi.mobile",
    }


def _parse_worldmonitor_timestamp(entry: Path) -> datetime | None:
    import re as _re

    match = _re.match(r"intel_(\d{8})_(\d{4,6})$", entry.stem)
    if not match:
        return None
    date_bits, time_bits = match.groups()
    if len(time_bits) == 4:
        time_bits = f"{time_bits}00"
    try:
        return datetime.strptime(f"{date_bits}_{time_bits}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _worldmonitor_sort_key(entry: Path) -> tuple[float, str]:
    parsed_at = _parse_worldmonitor_timestamp(entry)
    if parsed_at is not None:
        return (parsed_at.timestamp(), entry.name)
    try:
        return (entry.stat().st_mtime, entry.name)
    except OSError:
        return (0, entry.name)


def _format_worldmonitor_date(entry: Path) -> str:
    parsed_at = _parse_worldmonitor_timestamp(entry)
    if parsed_at is not None:
        return parsed_at.strftime("%Y-%m-%d %H:%M")
    return entry.stem.replace("intel_", "")


def _is_placeholder_worldmonitor_report(content: str) -> bool:
    compact = content.strip().lower()
    return compact in {"", "payload", "null", "none", "{}", "[]"}


def _is_failed_worldmonitor_report(content: str) -> bool:
    retired_source_failures = (
        "AP News: FAIL",
        "Reuters World: FAIL",
        "FINNHUB_API_KEY 未設定，市場行情已停用",
    )
    return (
        "[推理失敗]" in content
        or "Melchior reasoning failed" in content
        or any(marker in content for marker in retired_source_failures)
    )


def _clean_worldmonitor_text(text: str) -> str:
    cleaned = html.unescape(str(text or "")).strip()
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"^\d+[\.)]\s*", "", cleaned)
    cleaned = cleaned.strip(" -\t")
    return cleaned


def _strip_markup_text(text: str, limit: int = 360) -> str:
    cleaned = html.unescape(str(text or ""))
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit].rstrip()


def _xml_child_text(node: ET.Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return str(child.text).strip()
    for child in list(node):
        local = child.tag.rsplit("}", 1)[-1] if "}" in child.tag else child.tag
        if local in names and child.text:
            return str(child.text).strip()
    return ""


def _xml_child_link(node: ET.Element) -> str:
    link_text = _xml_child_text(node, "link")
    if link_text:
        return link_text
    for child in list(node):
        local = child.tag.rsplit("}", 1)[-1] if "}" in child.tag else child.tag
        if local == "link":
            href = str(child.attrib.get("href") or "").strip()
            if href:
                return href
    return ""


def _parse_research_feed(raw: bytes, source_url: str) -> dict:
    root = ET.fromstring(raw)
    channel = root.find("channel")
    feed_node = channel if channel is not None else root
    title = _xml_child_text(feed_node, "title") or source_url
    site_link = _xml_child_link(feed_node) or source_url
    updated = _xml_child_text(feed_node, "lastBuildDate", "updated", "pubDate")

    candidates = feed_node.findall("item")
    if not candidates:
        candidates = [
            child for child in list(feed_node)
            if (child.tag.rsplit("}", 1)[-1] if "}" in child.tag else child.tag) == "entry"
        ]

    items: list[dict] = []
    for item in candidates[:30]:
        item_title = _strip_markup_text(_xml_child_text(item, "title"), limit=180)
        link = _xml_child_link(item)
        summary = _strip_markup_text(
            _xml_child_text(item, "description", "summary", "content"),
            limit=420,
        )
        pub_date = _strip_markup_text(_xml_child_text(item, "pubDate", "published", "updated"), limit=120)
        if not item_title and not link:
            continue
        items.append({
            "title": item_title or link,
            "link": link,
            "summary": summary,
            "date": pub_date,
        })
    return {
        "title": _strip_markup_text(title, limit=160),
        "site_link": site_link,
        "source_url": source_url,
        "updated": _strip_markup_text(updated, limit=120),
        "items": items,
    }


def _fetch_research_feed(source_url: str, timeout: int = 12) -> dict:
    req = urllib.request.Request(
        source_url,
        headers={
            "User-Agent": "MAGI Research Preview/1.0",
            "Accept": "application/rss+xml, application/atom+xml, text/xml, application/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(2_000_000)
    return _parse_research_feed(raw, source_url)


def _normalise_source_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _load_worldmonitor_sidecar(entry: Path) -> dict:
    sidecar = entry.with_suffix(".json")
    try:
        if sidecar.is_file():
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 277, exc_info=True)
    return {}


def _parse_worldmonitor_markdown(content: str) -> dict:
    meta: dict[str, str] = {}
    sections: list[dict] = []
    source_health: list[str] = []
    current: dict | None = None
    in_details = False
    in_source_health = False

    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("<details"):
            in_details = True
            continue
        if in_details:
            continue
        if line.startswith("**") and "**:" in line:
            key, _, value = line.partition(":")
            meta[_clean_worldmonitor_text(key)] = _clean_worldmonitor_text(value)
            continue
        if line in {"---", "----"}:
            continue
        if line.startswith("## "):
            title = _clean_worldmonitor_text(line.lstrip("#").strip())
            in_source_health = "來源健康" in title
            if in_source_health:
                current = None
                continue
            if title in {"全球新聞", "市場數據"}:
                current = None
                continue
            current = {"title": title, "items": []}
            sections.append(current)
            continue
        item_match = re.match(r"^(?:[-*•]|\d+[\.)])\s+(.+)$", line)
        if item_match:
            item = _clean_worldmonitor_text(item_match.group(1))
            if in_source_health:
                source_health.append(item)
            elif current is not None and item:
                current["items"].append(item)

    sections = [section for section in sections if section.get("items")]
    return {"meta": meta, "sections": sections, "source_health": source_health}


def _normalise_worldmonitor_news_items(sidecar: dict, limit: int = 30) -> list[dict]:
    raw_items = sidecar.get("news_items") if isinstance(sidecar, dict) else []
    if not isinstance(raw_items, list):
        return []
    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = _clean_worldmonitor_text(raw.get("title") or "")
        if not title:
            continue
        items.append({
            "source": _clean_worldmonitor_text(raw.get("source") or "來源"),
            "title": title,
            "summary": _clean_worldmonitor_text(raw.get("summary") or ""),
            "link": str(raw.get("link") or raw.get("url") or "").strip(),
            "date": _clean_worldmonitor_text(raw.get("date") or ""),
        })
        if len(items) >= limit:
            break
    return items


def _iter_worldmonitor_reports(limit: int = 20) -> list[dict]:
    reports: list[dict] = []
    if not _WORLDMONITOR_REPORT_DIR.is_dir():
        return reports
    entries = [
        entry
        for entry in _WORLDMONITOR_REPORT_DIR.iterdir()
        if entry.is_file() and entry.suffix.lower() == ".md"
    ]
    for entry in sorted(entries, key=_worldmonitor_sort_key, reverse=True):
        if len(reports) >= limit:
            break
        try:
            full_content = entry.read_text(encoding="utf-8")
            content = full_content[:8000]
            read_error = ""
        except Exception:
            full_content = ""
            content = "(讀取失敗)"
            read_error = "檔案讀取失敗"
        is_placeholder = _is_placeholder_worldmonitor_report(full_content)
        warning = ""
        if read_error:
            warning = read_error
        elif is_placeholder:
            warning = "這份報告只有測試內容，沒有新聞摘要或分析。請按「立即更新」重新產生。"
        if is_placeholder or _is_failed_worldmonitor_report(full_content):
            continue
        parsed = _parse_worldmonitor_markdown(full_content)
        sidecar = _load_worldmonitor_sidecar(entry)
        source_health = parsed["source_health"]
        if not source_health and isinstance(sidecar.get("news_statuses"), list):
            healthy = sum(1 for item in sidecar["news_statuses"] if isinstance(item, dict) and item.get("ok"))
            total = len(sidecar["news_statuses"])
            source_health = [f"新聞來源：{healthy}/{total} 成功"]
            for item in sidecar["news_statuses"]:
                if not isinstance(item, dict):
                    continue
                state = "OK" if item.get("ok") else "FAIL"
                detail = f"{item.get('count', 0)} 篇" if item.get("ok") else item.get("error") or "fetch failed"
                source_health.append(f"{item.get('source', 'unknown')}: {state} ({detail})")
            market_status = sidecar.get("market_status") if isinstance(sidecar.get("market_status"), dict) else {}
            if market_status:
                state = "OK" if market_status.get("ok") else "DEGRADED"
                source_health.append(f"市場資料：{state} ({market_status.get('detail') or '未提供'})")
        reports.append({
            "name": entry.name,
            "content": content,
            "summary_text": _clean_worldmonitor_text(content[:1200]),
            "meta": parsed["meta"],
            "sections": parsed["sections"],
            "source_health": source_health,
            "news_items": _normalise_worldmonitor_news_items(sidecar),
            "date_display": _format_worldmonitor_date(entry),
            "is_placeholder": is_placeholder,
            "warning": warning,
            "size_bytes": entry.stat().st_size if entry.exists() else 0,
        })
    return reports


def _run_worldmonitor_collect(timeout: int = 240) -> tuple[bool, str]:
    """Run the local worldmonitor skill from the web app without exposing /skills/run."""
    action_path = _MAGI_ROOT / "skills" / "worldmonitor-intel" / "action.py"
    if not action_path.is_file():
        return False, "找不到全球新聞網技能程式。"

    bundled_python = _MAGI_ROOT / "venv" / "bin" / "python"
    python_bin = os.environ.get("MAGI_SKILL_PYTHON") or (str(bundled_python) if bundled_python.exists() else sys.executable)
    try:
        result = subprocess.run(
            [python_bin, str(action_path), "--task", "collect", "--no-reasoning", "--plain-output"],
            cwd=str(_MAGI_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "全球新聞更新逾時，請稍後再試。"
    except Exception as exc:
        return False, f"全球新聞更新啟動失敗：{exc}"

    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if result.returncode != 0:
        return False, output[-1200:] or "全球新聞更新失敗。"
    return True, output[-1200:] or "全球新聞已更新。"


@dashboard_pages_bp.route("/static/worldmonitor_reports")
@dashboard_pages_bp.route("/static/worldmonitor_reports/")
def worldmonitor_reports_redirect():
    return redirect("/intel")


@dashboard_pages_bp.route("/worldmonitor")
@dashboard_pages_bp.route("/worldmonitor/")
def worldmonitor_entry():
    return redirect("/intel")


@dashboard_pages_bp.route("/intel")
@login_required
def intel_panel():
    reports = _iter_worldmonitor_reports()
    return render_template("intel.html", reports=reports)


def _intel_refresh_response(ok: bool, message: str):
    wants_json = (
        request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
    )
    if wants_json:
        return jsonify({"ok": ok, "message": message}), 200 if ok else 500
    return redirect(url_for("dashboard_pages.intel_panel", refresh="ok" if ok else "failed"))


@dashboard_pages_bp.route("/api/intel/refresh", methods=["POST"])
@login_required
def intel_refresh():
    ok, message = _run_worldmonitor_collect()
    return _intel_refresh_response(ok, message)


@dashboard_pages_bp.route("/api/skills/run", methods=["POST"])
@login_required
def api_skills_run_compat():
    """Main-site compatibility shim for canonical Tools API skill execution."""
    data = request.get_json(silent=True) if request.is_json else None
    data = data if isinstance(data, dict) else request.form.to_dict(flat=True)
    try:
        from api.tools_api import _run_skill_from_payload

        return _run_skill_from_payload(
            data,
            user_id=str(getattr(current_user, "id", "") or "main-site"),
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("main-site skills/run compat failed: %s", exc)
    return jsonify({
        "ok": False,
        "error": "unsupported_main_site_skill_route",
        "canonical_endpoint": "/skills/run",
        "message": "主網站相容路由無法委派技能執行；請改用 canonical Tools API endpoint /skills/run。",
    }), 503


def _read_json_file(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 500, exc_info=True)
    return default


def _format_report_time(path: Path, payload: dict) -> str:
    raw = str(payload.get("timestamp") or payload.get("generated_at") or "").strip()
    if raw:
        return raw.replace("T", " ")[:19]
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "尚無紀錄"


def _report_timestamp(path: Path, payload: dict) -> float:
    raw = str(payload.get("timestamp") or payload.get("generated_at") or "").strip()
    if raw:
        try:
            normalized = raw.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except Exception:
            logging.getLogger(__name__).debug("report timestamp parse failed: %s", raw, exc_info=True)
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def _report_age_hours(path: Path, payload: dict) -> float | None:
    ts = _report_timestamp(path, payload)
    if ts <= 0:
        return None
    return max(0.0, (time.time() - ts) / 3600.0)


def _is_report_fresh(path: Path, payload: dict, max_age_hours: float) -> bool:
    age = _report_age_hours(path, payload)
    return age is not None and age <= max_age_hours


def _load_live_health_snapshot() -> dict:
    try:
        if current_app.config.get("TESTING") and not os.environ.get("MAGI_INTERNAL_HEALTH_URL"):
            return {}
    except RuntimeError:
        pass
    url = os.environ.get("MAGI_INTERNAL_HEALTH_URL") or "http://127.0.0.1:5002/health"
    try:
        with urllib.request.urlopen(url, timeout=0.8) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _find_report_test(report: dict, *ids: str) -> dict:
    wanted = {str(item) for item in ids if item}
    tests = report.get("tests") if isinstance(report.get("tests"), list) else []
    for item in tests:
        if isinstance(item, dict) and str(item.get("id") or "") in wanted:
            return item
    return {}


def _find_smoke_check(report: dict, name: str) -> dict:
    checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    for item in checks:
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            return item
    return {}


def _state_from_bool(value) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return "warn"


def _beginner_capability(name: str, status: str, detail: str, action: str = "") -> dict:
    return {
        "name": name,
        "status": status if status in {"pass", "warn", "fail"} else "warn",
        "detail": str(detail or "尚無最近檢查紀錄"),
        "action": str(action or ""),
    }


def _build_beginner_dashboard() -> dict:
    static_dir = _MAGI_ROOT / "static"
    system_report_path = static_dir / "system_test_report.json"
    smoke_report_path = static_dir / "integration_smoke_latest.json"
    magi_status_path = static_dir / "magi_status.json"

    system_report = _read_json_file(system_report_path, {})
    smoke_report = _read_json_file(smoke_report_path, {})
    magi_status = _read_json_file(magi_status_path, {})
    live_health = _load_live_health_snapshot()
    live_operational = str(live_health.get("status") or "").lower() == "operational"
    live_op_health = live_health.get("operational_health") if isinstance(live_health.get("operational_health"), dict) else {}
    live_health_ok = live_operational and live_op_health.get("ok") is not False
    system_fresh = _is_report_fresh(system_report_path, system_report, 24)
    smoke_fresh = _is_report_fresh(smoke_report_path, smoke_report, 72)

    system_total = int(system_report.get("total") or 0)
    system_passed = int(system_report.get("passed") or 0)
    system_failed = int(system_report.get("failed") or 0)
    system_ok = system_total > 0 and system_failed == 0 and system_passed == system_total
    system_tests = system_report.get("tests") if isinstance(system_report.get("tests"), list) else []
    failed_tests = [item for item in system_tests if isinstance(item, dict) and item.get("pass") is False]
    blocking_test_ids = {
        "local_llm",
        "casper_ollama",
        "keeper_db",
        "memory_module",
        "local_embed",
        "melchior_remote",
        "research_module",
        "iron_dome",
    }
    blocking_failures = [
        item
        for item in failed_tests
        if str(item.get("id") or "") in blocking_test_ids
    ]
    recovered_schedule_self_test = (
        live_health_ok
        and system_failed > 0
        and failed_tests
        and all(
            str(item.get("id") or "") == "autopilot_schedule"
            and "discord_bot" in str(item.get("detail") or "")
            for item in failed_tests
        )
    )

    main_health = _find_smoke_check(smoke_report, "main_health")
    embed_health = _find_smoke_check(smoke_report, "embed_service_health")
    smoke_overall = smoke_report.get("overall_ok") if smoke_fresh else None

    failed_smoke = [] if not smoke_fresh else [
        {
            "name": str(item.get("name") or "未命名檢查"),
            "summary": str(item.get("summary") or "沒有摘要").strip(),
            "kind": "setup" if "unauthorized" in str(item.get("summary") or "").lower() else "attention",
        }
        for item in (smoke_report.get("checks") if isinstance(smoke_report.get("checks"), list) else [])
        if isinstance(item, dict) and item.get("ok") is False
    ][:8]

    local_llm = _find_report_test(system_report, "local_llm", "casper_ollama")
    embed = _find_report_test(system_report, "local_embed", "melchior_remote")
    db = _find_report_test(system_report, "keeper_db")
    memory = _find_report_test(system_report, "memory_module")
    research = _find_report_test(system_report, "research_module")
    iron_dome = _find_report_test(system_report, "iron_dome")
    schedule = _find_report_test(system_report, "autopilot_schedule")
    if live_health_ok and schedule.get("pass") is False and "discord_bot" in str(schedule.get("detail") or ""):
        schedule = {
            **schedule,
            "pass": True,
            "detail": "即時 health 顯示核心服務與排程守門已恢復；清晨舊自測僅保留為歷史紀錄。",
        }

    capabilities = [
        _beginner_capability(
            "AI 回覆與本機推理",
            _state_from_bool(local_llm.get("pass")) if local_llm else _state_from_bool(main_health.get("ok")),
            local_llm.get("detail") or main_health.get("summary") or "",
            "可先試：摘要一段文字，或詢問一個一般法律問題。",
        ),
        _beginner_capability(
            "案件、資料庫與所務資料",
            _state_from_bool(db.get("pass")),
            db.get("detail") or "",
            "查案件、案件待辦、帳務這類功能需要本機資料庫正常。",
        ),
        _beginner_capability(
            "記憶與知識庫",
            _state_from_bool(memory.get("pass")),
            memory.get("detail") or "",
            "用於回想、知識檢索與部分 RAG 回答。",
        ),
        _beginner_capability(
            "向量搜尋與 Embedding",
            _state_from_bool(embed.get("pass")) if embed else _state_from_bool(embed_health.get("ok")),
            embed.get("detail") or embed_health.get("summary") or "",
            "支撐相似案件、文件與知識庫搜尋。",
        ),
        _beginner_capability(
            "網路研究",
            _state_from_bool(research.get("pass")),
            research.get("detail") or "",
            "最新資訊、新聞與網頁內容仍要看外部來源是否可連。",
        ),
        _beginner_capability(
            "安全守門",
            _state_from_bool(iron_dome.get("pass")),
            iron_dome.get("detail") or "",
            "刪除、送出、外部 portal 操作會被額外限制或要求確認。",
        ),
        _beginner_capability(
            "排程與背景任務",
            _state_from_bool(schedule.get("pass")),
            schedule.get("detail") or "",
            "夜間整理、同步與巡檢依排程器狀態而定。",
        ),
    ]

    if live_health_ok:
        readiness = {
            "status": "pass",
            "title": "目前狀態正常",
            "detail": "即時 health 顯示核心服務可用；舊自測或 smoke 只作歷史證據，不代表當下異常。",
        }
    elif system_ok and main_health.get("ok") is True:
        readiness = {
            "status": "pass",
            "title": "今日可開始使用",
            "detail": "核心推理、資料庫與基礎服務有最近檢查紀錄；高風險流程仍需人工確認。",
        }
    elif system_ok:
        readiness = {
            "status": "warn",
            "title": "核心測試通過，仍需看即時健康",
            "detail": "最近系統測試通過，但整合 smoke 或即時 health 沒有全綠。",
        }
    elif system_total > 0 and not blocking_failures:
        readiness = {
            "status": "warn",
            "title": "核心可用，有項目需確認",
            "detail": "最近自測的核心推理、資料庫與知識功能沒有阻塞失敗；排程、憑證或外部流程請看下方警示。",
        }
    else:
        readiness = {
            "status": "fail",
            "title": "先檢查系統再開始",
            "detail": "最近系統測試不是全數通過，建議先看 NERV 或 /health。",
        }

    work_lanes = [
        {
            "name": "案件與行程",
            "entry": "/osc",
            "checks": "資料庫、行事曆、NAS",
            "examples": ["今天行程", "查案件 2026-0035", "案件待辦"],
            "risk": "資料不足或同名案件時會要求補資訊。",
        },
        {
            "name": "文件處理",
            "entry": "/golem",
            "checks": "OCR、摘要、翻譯、檔案路徑",
            "examples": ["摘要", "完整翻譯", "OCR"],
            "risk": "掃描件品質會影響結果，正式文件仍要人工確認。",
        },
        {
            "name": "法律研究",
            "entry": "/research",
            "checks": "判決搜尋、法規庫、網路來源",
            "examples": ["民法184條", "實務見解 侵權行為", "搜尋最高法院 通譯"],
            "risk": "找不到資料時應回報找不到，不應補編。",
        },
        {
            "name": "法扶、閱卷、筆錄",
            "entry": "/osc",
            "checks": "Portal 帳號、API key、人工確認碼",
            "examples": ["閱卷查核 <法院> <案號>", "下載筆錄 <案號>", "法扶監控"],
            "risk": "送出、下載與回報屬高風險流程，保留二階段確認。",
        },
    ]

    evidence = [
        {
            "name": "即時 health",
            "status": "pass" if live_health_ok else ("fail" if live_health else "warn"),
            "summary": "目前 operational" if live_health_ok else ("即時 health 顯示需檢查" if live_health else "無法讀取即時 health"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if live_health else "即時查詢未取得",
        },
        {
            "name": "系統自測",
            "status": "warn" if recovered_schedule_self_test else (_state_from_bool(system_ok) if system_fresh else "warn"),
            "summary": (
                f"{system_passed}/{system_total} 通過；舊失敗已由即時 health 確認恢復"
                if recovered_schedule_self_test
                else (f"{system_passed}/{system_total} 通過" if system_total else "尚無報告")
            ),
            "time": _format_report_time(system_report_path, system_report),
        },
        {
            "name": "整合 smoke",
            "status": _state_from_bool(smoke_overall) if smoke_fresh else "warn",
            "summary": (
                "全部通過"
                if smoke_overall is True
                else ("歷史報告已過期，請以即時 health 為準" if not smoke_fresh else "有項目需檢查")
            ),
            "time": _format_report_time(smoke_report_path, smoke_report),
        },
        {
            "name": "節點狀態檔",
            "status": "pass" if magi_status else "warn",
            "summary": "已讀取" if magi_status else "尚無狀態檔",
            "time": _format_report_time(magi_status_path, magi_status if isinstance(magi_status, dict) else {}),
        },
    ]

    return {
        "readiness": readiness,
        "capabilities": capabilities,
        "work_lanes": work_lanes,
        "failed_smoke": failed_smoke,
        "evidence": evidence,
        "links": [
            {"label": "MAGI 對話", "href": "/golem"},
            {"label": "案件系統", "href": "/osc"},
            {"label": "系統檢測", "href": "/status"},
            {"label": "即時 health", "href": "/health"},
        ],
    }


def _build_status_dashboard() -> dict:
    dashboard = _build_beginner_dashboard()
    dashboard.update(
        {
            "page_title": "系統檢測 / 狀態中心",
            "page_label": "MAGI 系統檢測",
            "mode": "status",
            "intro": "本頁把最近自測、整合 smoke、節點狀態與即時 health 整理成新手可讀狀態。",
            "capabilities_action": {"label": "MAGI 調整", "href": "/magi-adjust"},
            "show_work_lanes": False,
            "quick_links": [
                {"label": "即時 health", "href": "/health"},
                {"label": "SaaS readyz", "href": "/readyz?scope=saas"},
                {"label": "MAGI 調整", "href": "/magi-adjust"},
            ],
            "evidence_links": [
                {"label": "即時 health", "href": "/health"},
                {"label": "SaaS readyz", "href": "/readyz?scope=saas"},
            ],
            "footer_note": "系統檢測只整理健康狀態與檢查證據；技能、API、遠端操作與進階設定仍在 MAGI 調整頁面處理。",
            "links": [
                {"label": "MAGI", "href": "/golem"},
                {"label": "案件系統", "href": "/osc"},
                {"label": "MAGI 調整", "href": "/magi-adjust"},
                {"label": "即時 health", "href": "/health"},
            ],
        }
    )
    return dashboard


def _load_research_dashboard() -> dict:
    rb_root = _MAGI_ROOT / ".runtime" / "research_brief"
    ns_dir = rb_root / "namespaces"
    namespaces: list[dict] = []
    if ns_dir.is_dir():
        for entry in sorted(ns_dir.glob("*.json"), key=lambda p: p.stem):
            data = _read_json_file(entry, {})
            if not isinstance(data, dict):
                continue
            sources = data.get("sources") if isinstance(data.get("sources"), list) else []
            keywords = data.get("keywords") if isinstance(data.get("keywords"), list) else []
            namespaces.append({
                "name": data.get("namespace") or entry.stem,
                "topic_key": data.get("topic_key") or "research_daily",
                "keywords": [str(k) for k in keywords if str(k).strip()],
                "sources": [],
            })
            for s in sources:
                if not isinstance(s, dict):
                    continue
                source_url = str(s.get("url") or "").strip()
                if not source_url:
                    continue
                source_type = str(s.get("type") or "html").strip()
                is_feed = source_type.lower() in {"rss", "atom", "feed"}
                namespaces[-1]["sources"].append({
                    "url": source_url,
                    "open_url": (
                        "/research/rss-preview?" + urllib.parse.urlencode({"url": source_url})
                        if is_feed
                        else source_url
                    ),
                    "is_feed": is_feed,
                    "type": source_type,
                    "lang": str(s.get("lang") or "").strip(),
                    "note": str(s.get("note") or "").strip(),
                })

    crawler_state = _read_json_file(_MAGI_ROOT / "_crawl_targets.json", {"targets": []})
    crawl_targets = crawler_state.get("targets") if isinstance(crawler_state, dict) else []
    if not isinstance(crawl_targets, list):
        crawl_targets = []

    digest_rows: list[dict] = []
    last_digest = rb_root / "last_digest.jsonl"
    try:
        if last_digest.exists():
            rows = last_digest.read_text(encoding="utf-8").splitlines()[-12:]
            for raw in reversed(rows):
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if isinstance(item, dict):
                    digest_rows.append(item)
    except Exception:
        digest_rows = []

    source_total = sum(len(ns["sources"]) for ns in namespaces)
    return {
        "namespaces": namespaces,
        "crawl_targets": [t for t in crawl_targets if isinstance(t, dict)],
        "digests": digest_rows,
        "namespace_count": len(namespaces),
        "source_total": source_total,
    }


@dashboard_pages_bp.route("/research")
@dashboard_pages_bp.route("/magi-research")
@login_required
def research_panel():
    return render_template("research.html", research=_load_research_dashboard(), user=current_user)


@dashboard_pages_bp.route("/research/judgment-classifier")
@login_required
def research_judgment_classifier():
    return render_template("research_judgment_classifier.html", user=current_user)


@dashboard_pages_bp.route("/research/rss-preview")
@login_required
def research_rss_preview():
    source_url = str(request.args.get("url") or "").strip()
    known_sources = {
        _normalise_source_url(source.get("url"))
        for namespace in _load_research_dashboard().get("namespaces", [])
        for source in namespace.get("sources", [])
        if source.get("is_feed")
    }
    if not source_url or _normalise_source_url(source_url) not in known_sources:
        feed = {
            "title": "找不到研究來源",
            "source_url": source_url,
            "site_link": "",
            "updated": "",
            "items": [],
            "error": "這個 RSS 不在 MAGI 的研究來源清單中。",
        }
        return render_template("rss_preview.html", feed=feed, user=current_user), 404
    try:
        feed = _fetch_research_feed(source_url)
    except Exception as exc:
        feed = {
            "title": source_url,
            "source_url": source_url,
            "site_link": source_url,
            "updated": "",
            "items": [],
            "error": f"RSS 讀取失敗：{exc}",
        }
    return render_template("rss_preview.html", feed=feed, user=current_user)


@dashboard_pages_bp.route("/dashboard")
@login_required
def dashboard():
    return redirect(url_for("dashboard_pages.golem_console"))


@dashboard_pages_bp.route("/dashboard/legacy")
@login_required
def dashboard_legacy():
    return redirect(url_for("dashboard_pages.golem_console"))


@dashboard_pages_bp.route("/dashboard/beginner")
@dashboard_pages_bp.route("/start")
@login_required
def dashboard_beginner():
    return render_template("dashboard_beginner.html", user=current_user, dashboard=_build_beginner_dashboard())


@dashboard_pages_bp.route("/status")
@dashboard_pages_bp.route("/dashboard/status")
@login_required
def status_center():
    return render_template("dashboard_beginner.html", user=current_user, dashboard=_build_status_dashboard())


@dashboard_pages_bp.route("/dashboard/nerv")
@dashboard_pages_bp.route("/nerv")
@dashboard_pages_bp.route("/magi-adjust")
@dashboard_pages_bp.route("/magi-settings")
@login_required
def magi_adjust():
    return render_template("dashboard_nerv.html", user=current_user)


@dashboard_pages_bp.route("/golem")
@dashboard_pages_bp.route("/dashboard/golem")
@login_required
def golem_console():
    return render_template("golem_console.html", user=current_user)


@dashboard_pages_bp.route("/mobile")
@dashboard_pages_bp.route("/app")
@login_required
def mobile_home():
    forced = _maybe_force_mobile_app_login()
    if forced is not None:
        return forced
    return render_template("mobile_home.html", user=current_user, mobile=_build_mobile_app_config())


@dashboard_pages_bp.route("/mobile-admin")
@dashboard_pages_bp.route("/app-admin")
@login_required
def mobile_admin():
    forced = _maybe_force_mobile_app_login()
    if forced is not None:
        return forced
    return render_template("mobile_admin.html", user=current_user, mobile=_build_mobile_app_config())


@dashboard_pages_bp.route("/mobile/config.json")
@login_required
def mobile_config_json():
    return jsonify(_build_mobile_app_config())


@dashboard_pages_bp.route("/mobile/sw.js")
def mobile_service_worker():
    sw_path = Path(__file__).resolve().parents[2] / "static" / "mobile" / "sw.js"
    body = sw_path.read_text(encoding="utf-8")
    return Response(
        body,
        mimetype="application/javascript",
        headers={
            "Service-Worker-Allowed": "/mobile",
            "Cache-Control": "no-cache",
        },
    )


@dashboard_pages_bp.route("/mobile/manifest.webmanifest")
def mobile_manifest():
    config = _build_mobile_app_config()
    manifest = {
        "name": "MAGI Mobile",
        "short_name": "MAGI",
        "description": "MAGI 與 Paperclip 內部行動入口",
        "id": "/mobile",
        "start_url": "/mobile",
        "scope": "/mobile",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": "#0f766e",
        "background_color": "#f4f6f2",
        "icons": [
            {
                "src": "/static/mobile/magi-mobile.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
        "shortcuts": [
            {"name": item["label"], "url": item["path"]}
            for item in config["routes"]
            if item["kind"] in {"core", "admin"}
        ],
    }
    return jsonify(manifest)


@dashboard_pages_bp.route("/dashboard/website")
@login_required
def dashboard_website():
    """個人網站後台管理（反向代理到 localhost:8088）"""
    return render_template("dashboard_website.html", user=current_user)


# --- Website admin reverse proxy ---
_ADMIN_BASE = "http://127.0.0.1:8088"
_PROXY_PREFIX = "/wa"


@dashboard_pages_bp.route(f"{_PROXY_PREFIX}/", defaults={"path": ""})
@dashboard_pages_bp.route(f"{_PROXY_PREFIX}/<path:path>", methods=["GET", "POST"])
@login_required
def website_admin_proxy(path):
    """Reverse-proxy website admin server so it works over Tailscale funnel."""
    url = f"{_ADMIN_BASE}/{path}"
    try:
        if request.method == "POST":
            resp = _requests.post(
                url,
                data=request.get_data(),
                headers={k: v for k, v in request.headers if k.lower() not in ("host", "content-length")},
                cookies=request.cookies,
                timeout=30,
                allow_redirects=False,
            )
        else:
            resp = _requests.get(
                url,
                headers={k: v for k, v in request.headers if k.lower() not in ("host",)},
                cookies=request.cookies,
                timeout=15,
                allow_redirects=False,
            )
        excluded = {"transfer-encoding", "content-encoding", "content-length", "connection"}
        headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded]
        return Response(resp.content, status=resp.status_code, headers=headers)
    except _requests.ConnectionError:
        return Response("後台伺服器未啟動", status=503, content_type="text/plain; charset=utf-8")
