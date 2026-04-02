import json
import os
from typing import Any

import requests


DEFAULT_BASE = os.environ.get("CASPER_TOOLS_API_BASE", "http://127.0.0.1:5003").rstrip("/")


def _post_json(path: str, payload: dict, timeout_sec: int = 60) -> dict:
    url = DEFAULT_BASE + path
    r = requests.post(url, json=payload, timeout=int(timeout_sec))
    # Keep behavior simple for legacy apps: return JSON even on non-200.
    try:
        data = r.json()
    except Exception:
        data = {"success": False, "error": f"non-json response: {r.status_code}", "text": (r.text or "")[:800]}
    if isinstance(data, dict):
        data.setdefault("http_status", r.status_code)
    return data


def casper_chat(prompt: str, timeout_sec: int = 180) -> dict:
    """
    General-purpose text generation via CASPER (tri-sage distributed inference).
    """
    p = (prompt or "").strip()
    if not p:
        return {"success": False, "error": "missing prompt"}
    return _post_json("/collab/chat", {"prompt": p, "timeout_sec": int(timeout_sec)}, timeout_sec=max(5, int(timeout_sec) + 5))


def casper_summarize(text: str, timeout_sec: int = 120) -> dict:
    t = (text or "").strip()
    if not t:
        return {"success": False, "error": "missing text"}
    return _post_json("/summarize", {"text": t}, timeout_sec=int(timeout_sec))


def casper_translate(
    text: str,
    target_lang: str = "繁體中文",
    source_lang: str = "auto",
    timeout_sec: int = 180,
    mode: str = "auto",
) -> dict:
    t = (text or "").strip()
    if not t:
        return {"success": False, "error": "missing text"}
    payload = {"text": t, "target_lang": target_lang, "source_lang": source_lang, "mode": mode}
    return _post_json("/collab/translate", payload, timeout_sec=int(timeout_sec))


def casper_fetch_url(url: str, timeout_sec: int = 60) -> dict:
    u = (url or "").strip()
    if not u:
        return {"success": False, "error": "missing url"}
    return _post_json("/fetch", {"url": u}, timeout_sec=int(timeout_sec))


def casper_research(topic: str, depth: int = 3, timeout_sec: int = 180) -> dict:
    t = (topic or "").strip()
    if not t:
        return {"success": False, "error": "missing topic"}
    return _post_json("/research", {"topic": t, "depth": int(depth)}, timeout_sec=int(timeout_sec))
