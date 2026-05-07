# -*- coding: utf-8 -*-
"""
export_text.py
==============
將長文字輸出成 /static/exports 下的 TXT，並產生可下載連結（若可推得 public base URL）。

設計目標：
- 供 Orchestrator / skills 共用，避免只有 LINE webhook server 才能 export。
- 不要求外網；只是一個本機檔案輸出器。

依據：
- MAGI_PUBLIC_BASE_URL（若有設定則優先）
- 否則讀取 MAGI/.agent/line_last_base_url.json（由 LINE webhook 回呼自動記錄）
"""

from __future__ import annotations
import logging

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AGENT_DIR = os.path.abspath(os.path.join(MAGI_ROOT, ".agent"))
LINE_LAST_BASE_URL_FILE = os.environ.get(
    "MAGI_LINE_LAST_BASE_URL_FILE",
    os.path.join(AGENT_DIR, "line_last_base_url.json"),
)

EXPORTS_DIR = os.environ.get(
    "MAGI_EXPORTS_DIR",
    os.path.abspath(os.path.join(MAGI_ROOT, "static", "exports")),
)


def _is_loopback_base_url(base: str) -> bool:
    s = (base or "").strip()
    if not s:
        return True
    if "://" not in s:
        s = "https://" + s
    try:
        host = (urlparse(s).hostname or "").strip().lower()
    except Exception:
        return True
    if not host:
        return True
    if host == "localhost" or host == "::1":
        return True
    if host.startswith("127."):
        return True
    return False


def _normalize_base_url(base: str) -> str:
    s = (base or "").strip().strip("'\"")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    return s.rstrip("/") + "/"


def _load_dotenv_value(key: str) -> str:
    env_path = os.path.join(MAGI_ROOT, ".env")
    if not os.path.exists(env_path):
        return ""
    try:
        for raw in open(env_path, "r", encoding="utf-8").read().splitlines():
            line = raw.strip()
            if (not line) or line.startswith("#") or ("=" not in line):
                continue
            k, v = line.split("=", 1)
            if k.strip() != key:
                continue
            return v.strip().strip("'\"")
    except Exception:
        return ""
    return ""


def _base_from_webhook_url(url: str) -> str:
    s = (url or "").strip().strip("'\"")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    try:
        p = urlparse(s)
        if not p.scheme or not p.netloc:
            return ""
        return f"{p.scheme}://{p.netloc}/"
    except Exception:
        return ""


def _build_tailscale_base_url() -> str:
    """Build base URL from Tailscale IP if configured."""
    ts_ip = (
        os.environ.get("MAGI_TAILSCALE_IP")
        or _load_dotenv_value("MAGI_TAILSCALE_IP")
        or ""
    ).strip()
    if not ts_ip:
        return ""
    ts_port = (
        os.environ.get("MAGI_TAILSCALE_PORT")
        or _load_dotenv_value("MAGI_TAILSCALE_PORT")
        or "18790"
    ).strip()
    return f"http://{ts_ip}:{ts_port}/"


def _load_public_base_url() -> str:
    # 1. Explicit override
    env_base = _normalize_base_url(os.environ.get("MAGI_PUBLIC_BASE_URL") or "")
    if env_base and (not _is_loopback_base_url(env_base)):
        return env_base

    dot_base = _normalize_base_url(_load_dotenv_value("MAGI_PUBLIC_BASE_URL"))
    if dot_base and (not _is_loopback_base_url(dot_base)):
        return dot_base

    # 2. Tailscale (stable, always-on VPN — preferred for internal access)
    ts_base = _build_tailscale_base_url()
    if ts_base:
        return ts_base

    # 3. LINE webhook domain (Cloudflare tunnel, may rotate)
    env_webhook = _base_from_webhook_url(os.environ.get("MAGI_LINE_WEBHOOK_ENDPOINT") or "")
    if env_webhook and (not _is_loopback_base_url(env_webhook)):
        return env_webhook

    dot_webhook = _base_from_webhook_url(_load_dotenv_value("MAGI_LINE_WEBHOOK_ENDPOINT"))
    if dot_webhook and (not _is_loopback_base_url(dot_webhook)):
        return dot_webhook

    # 4. Cached base URL from last webhook
    try:
        if os.path.exists(LINE_LAST_BASE_URL_FILE):
            with open(LINE_LAST_BASE_URL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            base = _normalize_base_url(data.get("base_url") or "")
            if base and (not _is_loopback_base_url(base)):
                return base
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 150, exc_info=True)
    return ""


def export_txt(text: str, *, prefix: str = "casper") -> dict:
    s = (text or "").strip()
    if not s:
        return {"success": False, "error": "empty text"}
    # Strip Markdown formatting — TXT is plain text
    try:
        from api.tw_output_guard import strip_markdown_for_chat
        s = strip_markdown_for_chat(s)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 163, exc_info=True)
    try:
        Path(EXPORTS_DIR).mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:10]
        filename = f"{prefix}_{stamp}_{token}.txt"
        path = os.path.join(EXPORTS_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(s + "\n")
        base = _load_public_base_url()
        url = (base.rstrip("/") + f"/static/exports/{filename}") if base else ""
        return {"success": True, "path": path, "filename": filename, "url": url}
    except Exception as e:
        return {"success": False, "error": str(e)}
