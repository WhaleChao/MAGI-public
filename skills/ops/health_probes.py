#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared oMLX / TAIDE probe helpers.

These helpers keep the runtime checks in magi-doctor and system_test aligned
without duplicating the request / retry / model-resolution logic.
"""

from __future__ import annotations

import os
import time
from typing import Any


def _build_omlx_base_url(base_url: str | None = None, port_env: str = "MAGI_OMLX_PORT") -> str:
    if base_url:
        return str(base_url).rstrip("/")
    port = int(os.environ.get(port_env, "8080"))
    return f"http://127.0.0.1:{port}"


def extract_model_labels(payload: Any) -> list[str]:
    """Normalize OpenAI-style /v1/models payloads into a flat label list."""
    if isinstance(payload, dict):
        raw_items = payload.get("data")
        if raw_items is None:
            raw_items = payload.get("models")
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    labels: list[str] = []
    for item in raw_items or []:
        if isinstance(item, str):
            label = item.strip()
        elif isinstance(item, dict):
            label = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        else:
            label = ""
        if label and label not in labels:
            labels.append(label)
    return labels


def _fetch_omlx_models(timeout_sec: int = 5, *, base_url: str | None = None, port_env: str = "MAGI_OMLX_PORT") -> tuple[int, list[str], str]:
    try:
        import requests
    except Exception as exc:  # pragma: no cover - import failure is environment specific
        return 0, [], str(exc)

    url = f"{_build_omlx_base_url(base_url, port_env)}/v1/models"
    try:
        r = requests.get(url, timeout=min(timeout_sec, 5))
        status_code = int(getattr(r, "status_code", 0) or 0)
        if status_code != 200:
            return status_code, [], ""
        try:
            payload = r.json()
        except Exception:
            payload = {}
        return status_code, extract_model_labels(payload), ""
    except Exception as exc:
        return 0, [], str(exc)


def probe_omlx_models(timeout_sec: int = 8, *, base_url: str | None = None, port_env: str = "MAGI_OMLX_PORT") -> dict:
    """Probe the oMLX model registry."""
    status_code, models, error = _fetch_omlx_models(timeout_sec=timeout_sec, base_url=base_url, port_env=port_env)
    if status_code == 200 and models:
        return {"pass": True, "status_code": 200, "models": models, "error": ""}
    if status_code == 200:
        return {"pass": False, "status_code": 200, "models": [], "error": "empty_model_list"}
    return {"pass": False, "status_code": status_code, "models": [], "error": error}


def resolve_omlx_model(
    default_model: str = "TAIDE-12b-Chat-mlx-4bit",
    *,
    base_url: str | None = None,
    timeout_sec: int = 5,
    requested_env: str = "CASPER_LOCAL_MODEL",
) -> str:
    """Choose the best available oMLX model label."""
    requested = (os.environ.get(requested_env) or default_model or "").strip()
    status_code, models, _ = _fetch_omlx_models(timeout_sec=timeout_sec, base_url=base_url)
    if status_code != 200 or not models:
        return requested or default_model

    if requested in models:
        return requested

    req_low = requested.lower()
    for model in models:
        low = model.lower()
        if req_low and (req_low == low or req_low in low or low.startswith(req_low)):
            return model
    for model in models:
        if "taide" in model.lower():
            return model
    return models[0]


def probe_local_chat(
    timeout_sec: int = 30,
    retries: int = 2,
    backoff_sec: float = 1.5,
    *,
    base_url: str | None = None,
    default_model: str = "TAIDE-12b-Chat-mlx-4bit",
    models_timeout_sec: int = 8,
    requested_env: str = "CASPER_LOCAL_MODEL",
    prompt: str = "請只回答 OK",
    max_tokens: int = 4,
) -> dict:
    """Run a bounded TAIDE chat probe with retry/backoff."""
    try:
        import requests
    except Exception as exc:  # pragma: no cover - environment specific
        return {"pass": False, "model": None, "response": "", "attempt": 0, "status_code": 0, "error": str(exc), "models_probe": None}

    models_probe = probe_omlx_models(timeout_sec=models_timeout_sec, base_url=base_url)
    if not models_probe.get("pass"):
        return {
            "pass": False,
            "model": None,
            "response": "",
            "attempt": 0,
            "status_code": int(models_probe.get("status_code") or 0),
            "error": str(models_probe.get("error") or "oMLX unavailable"),
            "models_probe": models_probe,
        }

    omlx_base = _build_omlx_base_url(base_url)
    resolved_model = resolve_omlx_model(
        default_model,
        base_url=base_url,
        timeout_sec=models_timeout_sec,
        requested_env=requested_env,
    )
    payload = {
        "model": resolved_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        base_timeout = max(10, int(timeout_sec))
    except Exception:
        base_timeout = 30
    max_retries = max(1, int(retries))
    last_error = f"empty response (model={resolved_model})"
    retryable_markers = ("timeout", "timed out", "temporarily unavailable", "connection aborted")

    for attempt in range(1, max_retries + 1):
        attempt_timeout = min(60, base_timeout + max(0, attempt - 1) * 15)
        try:
            r = requests.post(f"{omlx_base}/v1/chat/completions", json=payload, timeout=attempt_timeout)
            status_code = int(getattr(r, "status_code", 0) or 0)
            if status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                choices = data.get("choices") or []
                response = choices[0].get("message", {}).get("content", "") if choices else ""
                response = str(response or "").strip()
                if response:
                    return {
                        "pass": True,
                        "model": resolved_model,
                        "response": response,
                        "attempt": attempt,
                        "status_code": 200,
                        "error": "",
                        "models_probe": models_probe,
                    }
                last_error = f"empty response (model={resolved_model})"
            else:
                last_error = f"HTTP {status_code} (model={resolved_model})"
        except Exception as exc:
            last_error = str(exc)
        if attempt < max_retries and any(marker in last_error.lower() for marker in retryable_markers):
            time.sleep(backoff_sec * attempt)
            continue
        break

    return {
        "pass": False,
        "model": resolved_model,
        "response": "",
        "attempt": max_retries,
        "status_code": 0,
        "error": last_error,
        "models_probe": models_probe,
    }
