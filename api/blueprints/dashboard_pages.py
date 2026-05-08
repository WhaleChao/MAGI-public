"""
Dashboard / Intel / OpenClaw page routes
========================================

First modularization slice for the page layer that was previously embedded in
`api/server.py`.

This blueprint keeps the existing behavior for:
  - /static/worldmonitor_reports -> /intel
  - /worldmonitor -> /intel
  - /openclaw -> /magi-adjust
  - /intel -> worldmonitor report index
  - /dashboard
  - /dashboard/nerv
  - /magi-adjust

The module is intentionally dependency-light and does not import server.py.
"""

from __future__ import annotations

import json
import html
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

import requests as _requests

dashboard_pages_bp = Blueprint("dashboard_pages", __name__)

_MAGI_ROOT = Path(__file__).resolve().parents[2]
_WORLDMONITOR_REPORT_DIR = _MAGI_ROOT / "static" / "worldmonitor_reports"


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
        pass
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
        pass
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
        pass
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


@dashboard_pages_bp.route("/openclaw")
@dashboard_pages_bp.route("/openclaw-gateway")
def openclaw_entry():
    return redirect("/magi-adjust")


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
    """Compatibility for the old Global News button; generic skill runs stay on Tools API."""
    data = request.get_json(silent=True) if request.is_json else None
    data = data if isinstance(data, dict) else request.form
    skill = str(data.get("skill") or "").strip()
    task = str(data.get("task") or "").strip()
    if skill == "worldmonitor-intel" and task == "collect":
        ok, message = _run_worldmonitor_collect()
        return _intel_refresh_response(ok, message)
    return jsonify({
        "ok": False,
        "error": "unsupported_main_site_skill_route",
        "message": "主網站只保留全球新聞網舊按鈕相容；其他技能請使用 Tools API /skills/run。",
    }), 400


def _read_json_file(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return default


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


@dashboard_pages_bp.route("/dashboard/nerv")
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
    return render_template("mobile_home.html", user=current_user, mobile=_build_mobile_app_config())


@dashboard_pages_bp.route("/mobile-admin")
@dashboard_pages_bp.route("/app-admin")
@login_required
def mobile_admin():
    return render_template("mobile_admin.html", user=current_user, mobile=_build_mobile_app_config())


@dashboard_pages_bp.route("/mobile/config.json")
@login_required
def mobile_config_json():
    return jsonify(_build_mobile_app_config())


@dashboard_pages_bp.route("/mobile/manifest.webmanifest")
def mobile_manifest():
    config = _build_mobile_app_config()
    manifest = {
        "name": "MAGI Mobile",
        "short_name": "MAGI",
        "description": "MAGI 與 Paperclip 內部行動入口",
        "id": "/mobile",
        "start_url": "/mobile",
        "scope": "/",
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
