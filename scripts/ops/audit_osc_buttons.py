#!/usr/bin/env python3
"""audit_osc_buttons.py — 列出 OSC 所有 button 與 onclick handler，對照 backend route 是否存在。

輸出：
  ok:              button → handler 路徑對得起來
  missing_route:   前端呼叫了不存在的 /api/osc/... route
  no_handler:      button 找不到對應的 fetch/onclick
  info:            參考資訊

用法：
  python scripts/ops/audit_osc_buttons.py
  python scripts/ops/audit_osc_buttons.py --html-only
  python scripts/ops/audit_osc_buttons.py --js-only
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── 搜尋範圍 ──────────────────────────────────────────────────────────────────
TEMPLATE_DIRS = [
    REPO_ROOT / "templates" / "partials" / "osc",
    REPO_ROOT / "templates",
]
TEMPLATE_PATTERNS = ["osc*.html", "partials/osc/*.html"]
JS_DIRS = [REPO_ROOT / "static" / "osc"]
BACKEND_FILES = list((REPO_ROOT / "api" / "blueprints").glob("osc_*.py"))
BACKEND_FILES += list((REPO_ROOT / "api").glob("osc_*.py"))
# 也掃其他包含 /api/osc/ route 的 blueprint（如 web_runtime.py）
BACKEND_FILES += list((REPO_ROOT / "api" / "blueprints").glob("*.py"))

# ── 結果型別 ──────────────────────────────────────────────────────────────────
class Finding(NamedTuple):
    status: str       # "ok" | "missing_route" | "no_handler" | "info"
    source: str       # 來源檔案:行號
    description: str  # 描述
    detail: str = ""  # 詳細資訊


def _collect_html_files() -> list[Path]:
    files: list[Path] = []
    # osc*.html in templates/
    for f in (REPO_ROOT / "templates").glob("osc*.html"):
        files.append(f)
    # partials/osc/*.html
    partial_dir = REPO_ROOT / "templates" / "partials" / "osc"
    if partial_dir.is_dir():
        files.extend(partial_dir.glob("*.html"))
    return sorted(set(files))


def _collect_js_files() -> list[Path]:
    files: list[Path] = []
    js_dir = REPO_ROOT / "static" / "osc"
    if js_dir.is_dir():
        files.extend(js_dir.glob("*.js"))
        files.extend((js_dir / "tabs").glob("*.js"))
    return sorted(set(files))


# ── 從 HTML 抽取 button + data-act / onclick ─────────────────────────────────
_RE_HTML_BUTTON = re.compile(
    r"<button\b([^>]*)>([^<]*)</button>",
    re.IGNORECASE | re.DOTALL,
)
_RE_HTML_ID = re.compile(r'\bid="([^"]+)"')
_RE_HTML_DATA_ACT = re.compile(r'\bdata-act="([^"]+)"')
_RE_HTML_ONCLICK = re.compile(r'\bonclick="([^"]+)"')


def _extract_html_buttons(path: Path) -> list[dict]:
    results = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _RE_HTML_BUTTON.finditer(line):
            attrs, label = m.group(1), m.group(2).strip()
            bid = (_RE_HTML_ID.search(attrs) or type("", (), {"group": lambda s, n: ""})()).group(1)
            data_act = (_RE_HTML_DATA_ACT.search(attrs) or type("", (), {"group": lambda s, n: ""})()).group(1)
            onclick = (_RE_HTML_ONCLICK.search(attrs) or type("", (), {"group": lambda s, n: ""})()).group(1)
            results.append({
                "file": str(path.relative_to(REPO_ROOT)),
                "line": lineno,
                "id": bid,
                "label": label,
                "data_act": data_act,
                "onclick": onclick,
            })
    return results


# ── 從 JS 抽取 fetch("/api/osc/...") 呼叫 ───────────────────────────────────
_RE_JS_FETCH = re.compile(
    r'(?:fetch|api)\(\s*["\']([^"\']*?/api/osc/[^"\']*?)["\']',
    re.IGNORECASE,
)
_RE_JS_FETCH2 = re.compile(
    # Template literal — 擷取到第一個 $ 或結尾（排除 query string 部分）
    r'(?:fetch|api)\(\s*`([^`$"\']*?/api/osc/[^`$"\']*?)(?:\$|\`)',
    re.IGNORECASE,
)


def _extract_js_api_calls(path: Path) -> list[dict]:
    results = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        for pattern in (_RE_JS_FETCH, _RE_JS_FETCH2):
            for m in pattern.finditer(line):
                url = m.group(1)
                # 正規化：去掉 template literal 佔位符
                url_norm = re.sub(r'\$\{[^}]+\}', '<id>', url)
                results.append({
                    "file": str(path.relative_to(REPO_ROOT)),
                    "line": lineno,
                    "url": url_norm,
                })
    return results


# ── 從 backend 抽取所有 /api/osc/... route ───────────────────────────────────
_RE_ROUTE = re.compile(
    r'@\w+\.route\(\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _extract_backend_routes() -> list[str]:
    routes: list[str] = []
    for bf in BACKEND_FILES:
        if not bf.exists():
            continue
        text = bf.read_text(encoding="utf-8", errors="replace")
        for m in _RE_ROUTE.finditer(text):
            url = m.group(1)
            if "/api/osc/" in url or url.startswith("/osc"):
                # 正規化：<path:x> → <id>，<int:x> → <id>，<x> → <id>
                url_norm = re.sub(r"<[^>]+>", "<id>", url)
                routes.append(url_norm)
    return list(set(routes))


def _url_match(call_url: str, backend_routes: list[str]) -> bool:
    """模糊比對：處理 template literal 佔位符與 trailing slash。"""
    # 正規化：去掉 query string 和 trailing slash
    def norm(u: str) -> str:
        u = u.rstrip("?#").split("?")[0].rstrip("/")
        return u

    call_n = norm(call_url)
    for route in backend_routes:
        route_n = norm(route)
        # 字串完全比對（含 trailing slash 正規化）
        if call_n == route_n:
            return True
        # call_url 是 route 的前綴（例如 /api/osc/cases 對應 /api/osc/cases/<id>）
        if route_n.startswith(call_n + "/") or call_n.startswith(route_n + "/"):
            return True
        # regex 比對（處理 <id> 萬用字元）
        call_re = re.sub(r"<id>", "[^/]+", re.escape(call_n))
        route_re = re.sub(r"<id>", "[^/]+", re.escape(route_n))
        try:
            if re.fullmatch(route_re, call_n):
                return True
            if re.fullmatch(call_re, route_n):
                return True
        except re.error:
            pass
    return False


def run_audit(html_only: bool = False, js_only: bool = False) -> list[Finding]:
    findings: list[Finding] = []

    backend_routes = _extract_backend_routes()
    findings.append(Finding("info", "backend", f"找到 {len(backend_routes)} 個 /api/osc/* route", "\n".join(sorted(backend_routes))))

    html_files = [] if js_only else _collect_html_files()
    js_files = [] if html_only else _collect_js_files()

    # 1. HTML button audit
    total_buttons = 0
    for hf in html_files:
        buttons = _extract_html_buttons(hf)
        total_buttons += len(buttons)
        for btn in buttons:
            src = f"{btn['file']}:{btn['line']}"
            label = btn['label'] or "(無文字)"
            # 只報告有 id 或 data-act 的按鈕（功能性按鈕）
            bid = btn['id'] or btn['data_act']
            if not bid and not btn['onclick']:
                # 純顯示按鈕（例如 chip, tab-btn），跳過
                continue
            findings.append(Finding(
                "info",
                src,
                f"HTML button id={bid!r} label={label!r}",
                f"data-act={btn['data_act']!r} onclick={btn['onclick']!r}"
            ))

    # 2. JS fetch API call audit
    total_api_calls = 0
    missing = 0
    ok_count = 0
    for jf in js_files:
        calls = _extract_js_api_calls(jf)
        total_api_calls += len(calls)
        for call in calls:
            src = f"{call['file']}:{call['line']}"
            url = call['url']
            if _url_match(url, backend_routes):
                ok_count += 1
                findings.append(Finding("ok", src, f"fetch → {url}", ""))
            else:
                missing += 1
                findings.append(Finding("missing_route", src, f"fetch → {url} ❌ 後端無對應 route", ""))

    findings.append(Finding("info", "summary",
        f"HTML button 掃描：{total_buttons} 個 | JS API 呼叫：{total_api_calls} 個 | ok={ok_count} | missing_route={missing}",
        ""
    ))
    return findings


def main():
    parser = argparse.ArgumentParser(description="OSC button → backend route audit")
    parser.add_argument("--html-only", action="store_true")
    parser.add_argument("--js-only", action="store_true")
    parser.add_argument("--missing-only", action="store_true", help="只輸出 missing_route")
    parser.add_argument("--summary", action="store_true", help="只輸出 summary")
    args = parser.parse_args()

    findings = run_audit(html_only=args.html_only, js_only=args.js_only)

    for f in findings:
        if args.summary and f.status != "info":
            continue
        if args.missing_only and f.status != "missing_route":
            if f.status != "info" or "summary" not in f.source:
                continue
        prefix = {
            "ok": "✅",
            "missing_route": "❌",
            "no_handler": "⚠️",
            "info": "ℹ️",
        }.get(f.status, "  ")
        print(f"{prefix} [{f.status}] {f.source} — {f.description}")
        if f.detail and f.status in ("missing_route", "info") and "backend" in f.source:
            for line in f.detail.splitlines()[:10]:
                print(f"    {line}")


if __name__ == "__main__":
    main()
