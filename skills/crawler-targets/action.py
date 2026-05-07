#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawler-targets / action.py

Persist user-provided crawl targets and ingest into vector DB (best-effort).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_magi_root_dir, get_orch_dir, get_skill_python

CODE_DIR = str(get_orch_dir())
_MAGI_ROOT = str(get_magi_root_dir())
STATE_PATH = os.environ.get("MAGI_CRAWL_TARGETS_PATH", os.path.join(_MAGI_ROOT, "_crawl_targets.json"))
_VENV_PY = str(get_skill_python())
_FETCH_DELAY_SEC = float(os.environ.get("MAGI_CRAWL_FETCH_DELAY", "1.5"))
_ALLOWED_SCHEMES = {"http", "https"}

logger = logging.getLogger("crawler-targets")


def _maybe_reexec_venv() -> None:
    if os.environ.get("MAGI_CRAWL_TARGETS_NO_VENV", "").strip() == "1":
        return
    try:
        if os.path.exists(_VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PY):
            os.execv(_VENV_PY, [_VENV_PY, __file__, *sys.argv[1:]])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 43, exc_info=True)


def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _load_state() -> dict:
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                if not isinstance(d.get("targets"), list):
                    d["targets"] = []
                return d
    except Exception as e:
        logger.warning("Failed to load state from %s: %s", STATE_PATH, e)
    st = {"targets": []}
    _save_state(st)
    return st


def _save_state(st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        logger.warning("Failed to save state to %s: %s", STATE_PATH, e)


def _load_jsonish(text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {}
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"value": t}


def _norm_url(u: str) -> str:
    s = (u or "").strip()
    if not s:
        return ""
    # Normalize: lowercase scheme+netloc, strip trailing slash on path
    try:
        p = urlparse(s)
        if p.scheme and p.netloc:
            normalized = p._replace(
                scheme=p.scheme.lower(),
                netloc=p.netloc.lower(),
            ).geturl()
            return normalized.rstrip("/") if normalized.endswith("/") and p.path in ("", "/") else normalized
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 103, exc_info=True)
    return s


def _validate_url(u: str) -> tuple[bool, str]:
    """Validate URL: must be http(s), have a netloc, max 2048 chars."""
    if not u:
        return False, "empty url"
    if len(u) > 2048:
        return False, "url exceeds 2048 chars"
    try:
        p = urlparse(u)
    except Exception:
        return False, "url parse failed"
    if p.scheme not in _ALLOWED_SCHEMES:
        return False, f"scheme '{p.scheme}' not allowed (http/https only)"
    if not p.netloc or "." not in p.netloc:
        return False, "invalid netloc"
    return True, ""


def list_targets() -> dict:
    st = _load_state()
    items = st.get("targets") if isinstance(st.get("targets"), list) else []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = _norm_url(it.get("url") or "")
        if not url:
            continue
        out.append({"url": url, "note": (it.get("note") or ""), "added_ts": it.get("added_ts") or ""})
    return {"success": True, "count": len(out), "targets": out, "state_path": STATE_PATH}


def add_target(url: str, note: str = "") -> dict:
    u = _norm_url(url)
    if not u:
        return {"success": False, "error": "missing url"}
    ok, err = _validate_url(u)
    if not ok:
        return {"success": False, "error": err}
    st = _load_state()
    items = st.get("targets") if isinstance(st.get("targets"), list) else []
    # de-dupe
    for it in items:
        if isinstance(it, dict) and _norm_url(it.get("url") or "") == u:
            it["note"] = (note or it.get("note") or "")
            _save_state({"targets": items})
            return {"success": True, "action": "updated", "url": u, "state_path": STATE_PATH}
    items.append({"url": u, "note": (note or ""), "added_ts": datetime.now().isoformat()})
    _save_state({"targets": items})
    return {"success": True, "action": "added", "url": u, "state_path": STATE_PATH}


def remove_target(url: str) -> dict:
    u = _norm_url(url)
    if not u:
        return {"success": False, "error": "missing url"}
    st = _load_state()
    items = st.get("targets") if isinstance(st.get("targets"), list) else []
    kept = []
    removed = 0
    for it in items:
        if isinstance(it, dict) and _norm_url(it.get("url") or "") == u:
            removed += 1
            continue
        kept.append(it)
    _save_state({"targets": kept})
    return {"success": True, "removed": removed, "url": u, "state_path": STATE_PATH}


def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    try:
        if CODE_DIR not in sys.path:
            sys.path.insert(0, CODE_DIR)
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags=tags or {}, source="crawler_targets")
    except Exception:
        return


def run_daily(max_targets: int = 20, max_sections: int = 10) -> dict:
    """
    Fetch target URLs and ingest into vector DB.
    Best-effort:
    - If a target fails (Iron Dome block / fetch error), continue with others.
    """
    sys.path.insert(0, _MAGI_ROOT)
    from skills.research.web_research import fetch_url_sections  # type: ignore
    from skills.documents.vector_pipeline import ingest_sections_to_vector_memory  # type: ignore

    st = _load_state()
    items = st.get("targets") if isinstance(st.get("targets"), list) else []
    targets = []
    for it in items:
        if not isinstance(it, dict):
            continue
        u = _norm_url(it.get("url") or "")
        if u:
            targets.append({"url": u, "note": (it.get("note") or "")})
    targets = targets[: max(0, int(max_targets))]

    results = []
    ok_count = 0
    for idx, t in enumerate(targets):
        url = t["url"]
        note = t.get("note") or ""
        # Rate limiting between fetches
        if idx > 0 and _FETCH_DELAY_SEC > 0:
            time.sleep(_FETCH_DELAY_SEC)
        logger.info("[%d/%d] Fetching: %s", idx + 1, len(targets), url[:100])
        try:
            fetched = fetch_url_sections(url, max_length=120000, max_sections=int(max_sections))
            if not fetched.get("success"):
                results.append({"url": url, "ok": False, "error": fetched.get("error", "fetch_failed")})
                _eventlog("crawl_target:fetch", ok=False, payload={"url": url, "error": fetched.get("error", "")[:240]})
                continue
            title = (fetched.get("title") or "").strip() or urlparse(url).netloc
            sections = fetched.get("sections") or []
            if not sections:
                results.append({"url": url, "ok": False, "error": "no_sections_extracted"})
                _eventlog("crawl_target:fetch", ok=False, payload={"url": url, "error": "no_sections_extracted"})
                continue
            ing = ingest_sections_to_vector_memory(url=url, title=title, sections=sections)
            if ing.get("success"):
                ok_count += 1
                results.append({"url": url, "ok": True, "title": title, "doc_key": ing.get("doc_key", ""), "note": note})
                _eventlog("crawl_target:ingest", ok=True, payload={"url": url, "title": title, "doc_key": ing.get("doc_key", "")})
            else:
                results.append({"url": url, "ok": False, "title": title, "error": ing.get("error", "ingest_failed")})
                _eventlog("crawl_target:ingest", ok=False, payload={"url": url, "title": title, "error": ing.get("error", "")[:240]})
        except Exception as e:
            logger.warning("crawl_target exception on %s: %s", url[:100], e)
            results.append({"url": url, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            _eventlog("crawl_target:exception", ok=False, payload={"url": url, "error": f"{type(e).__name__}: {str(e)[:240]}"})

    return {"success": True, "targets": len(targets), "ok": ok_count, "results": results, "state_path": STATE_PATH}


def main() -> int:
    _maybe_reexec_venv()
    ap = argparse.ArgumentParser(description="crawler-targets skill")
    ap.add_argument("--task", required=True, help="list|add {...}|remove {...}|run_daily {...}|self_test")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary"}:
        return _ok({"success": True, "commands": ["help", "list", "add {..json..}", "remove {..json..}", "run_daily {..json..}", "self_test"]})

    if task == "list":
        return _ok(list_targets())

    if task.startswith("add"):
        p = _load_jsonish(task[len("add") :].strip())
        url = (p.get("url") or p.get("value") or "").strip()
        note = (p.get("note") or "").strip()
        return _ok(add_target(url, note=note))

    if task.startswith("remove"):
        p = _load_jsonish(task[len("remove") :].strip())
        url = (p.get("url") or p.get("value") or "").strip()
        return _ok(remove_target(url))

    if task.startswith("run_daily"):
        p = _load_jsonish(task[len("run_daily") :].strip())
        try:
            mt = int(p.get("max_targets") or 20)
        except (ValueError, TypeError):
            mt = 20
        try:
            ms = int(p.get("max_sections") or 10)
        except (ValueError, TypeError):
            ms = 10
        return _ok(run_daily(max_targets=mt, max_sections=ms))

    if task == "self_test":
        # A tiny smoke test: list state file.
        res = list_targets()
        return _ok({"success": True, "state_path": res.get("state_path", ""), "count": res.get("count", 0)})

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())
