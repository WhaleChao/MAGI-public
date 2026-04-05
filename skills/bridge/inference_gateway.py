"""
Unified inference gateway for MAGI.

Routing order (local-first, NO distributed inference):
1) local_ollama (primary — best local models available)
2) remote_melchior (acceleration only, if reachable)
3) remote_balthasar (backup remote, if reachable)
"""

from __future__ import annotations

import base64
import datetime
import json as _json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter
from typing import List, Optional, Tuple

import requests

from api.model_config import (
    CODE_MODEL as DEFAULT_CODE_MODEL,
    EMBED_MODEL as DEFAULT_EMBED_MODEL,
    GENERAL_MODEL as DEFAULT_GENERAL_MODEL,
    OCR_MODEL as DEFAULT_OCR_MODEL,
    SUMMARY_MODEL as DEFAULT_SUMMARY_MODEL,
    TEXT_PRIMARY_MODEL,
    TEXT_REVIEW_MODEL,
    VISION_MODEL as DEFAULT_VISION_MODEL,
    default_local_chat_models,
    default_local_vision_models,
    resolve_text_model,
)

try:
    from providers import build_provider_registry as _build_provider_registry
except Exception:
    _build_provider_registry = None

# ---------------------------------------------------------------------------
# Local Ollama concurrency guard — prevent 503 "maximum pending requests"
# ---------------------------------------------------------------------------
_OLLAMA_MAX_CONCURRENT = int(os.environ.get("OLLAMA_MAX_CONCURRENT", "2"))
_ollama_semaphore = threading.Semaphore(_OLLAMA_MAX_CONCURRENT)

from skills.bridge import melchior_client

try:
    from skills.bridge import balthasar_bridge  # type: ignore
except Exception:
    balthasar_bridge = None


logger = logging.getLogger("InferenceGateway")

# ---------------------------------------------------------------------------
# Night window (default 22:00-06:00)
# ---------------------------------------------------------------------------
_NIGHT_START = int(os.environ.get("MAGI_NIGHT_START_HOUR", "22"))
_NIGHT_END = int(os.environ.get("MAGI_NIGHT_END_HOUR", "6"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def _is_night() -> bool:
    h = datetime.datetime.now().hour
    if _NIGHT_START > _NIGHT_END:
        return h >= _NIGHT_START or h < _NIGHT_END
    return _NIGHT_START <= h < _NIGHT_END


# ---------------------------------------------------------------------------
# Intent Classification
# ---------------------------------------------------------------------------
_INTENT_RULES: List[Tuple[str, List[str], Optional[re.Pattern]]] = [
    # (task_type, keywords, optional compiled regex)
    ("captcha",    ["captcha", "驗證碼", "verification code", "digits"],
                   re.compile(r"(?i)(captcha|驗證碼|read.*digits|辨識.*\d)")),
    ("vision",     ["圖片", "image", "照片", "photo", "掃描", "scan", "截圖", "screenshot", "OCR"],
                   re.compile(r"(?i)(image|圖|照片|掃描|screenshot|ocr)")),
    ("summary",    ["摘要", "summarize", "summary", "判決摘要", "重點整理", "歸納", "精簡", "整理", "要旨"],
                   re.compile(r"(?i)(摘要|summar|歸納|重點整理|判決.*要旨|整理.*出來|爭點.*整理)")),
    ("translate",  ["翻譯", "translate", "translation", "英翻中", "中翻英", "日翻中"],
                   re.compile(r"(?i)(翻譯|translat|英翻|中翻|日翻)")),
    ("transcribe", ["逐字稿", "transcript", "transcribe", "語音轉文字", "錄音"],
                   re.compile(r"(?i)(逐字稿|transcript|transcrib|語音.*文字|錄音)")),
    ("tc_review",  ["校正", "正體", "繁中", "台灣用語", "簡轉繁", "用語檢查"],
                   re.compile(r"(?i)(校正|正體|繁中|台灣用語|簡轉繁|用語.*檢查)")),
    ("legal_analysis", ["法律分析", "法條", "判例", "實務見解", "法律問題", "構成要件"],
                   re.compile(r"(?i)(法律分析|法條適用|判例.*分析|構成要件|條文.*解釋)")),
]


def classify_intent(prompt: str, image_path: str = "", explicit_task_type: str = "") -> str:
    """
    Smart intent classification from prompt text.
    Returns: task_type string.
    Priority: explicit > keyword/regex > structural analysis > default.
    """
    # 1) Explicit override always wins
    if explicit_task_type and explicit_task_type.strip():
        return explicit_task_type.strip()

    # 2) If image provided → vision or captcha
    if image_path and str(image_path).strip():
        p_lower = (prompt or "").lower()
        if any(k in p_lower for k in ("captcha", "驗證碼", "digits", "characters")):
            return "captcha"
        return "vision"

    text = str(prompt or "").strip()
    if not text:
        return "general"

    # 3) Keyword + regex matching (first match wins, ordered by specificity)
    text_lower = text.lower()
    for task_type, keywords, pattern in _INTENT_RULES:
        if any(kw in text_lower for kw in keywords):
            return task_type
        if pattern and pattern.search(text):
            return task_type

    # 4) Structural heuristics
    if len(text) > 2000:
        # Long text input → likely needs summarization
        return "summary"
    if len(text) > 500 and any(k in text for k in ("判決", "裁定", "起訴", "被告", "原告", "爭點")):
        return "legal_analysis"

    return "general"


# ---------------------------------------------------------------------------
# Model Roster: task_type → {day_model, night_model, local_model}
# ---------------------------------------------------------------------------
_MODEL_ROSTER = {
    "general":        {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_GENERAL_MODEL},
    "summary":        {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_SUMMARY_MODEL},
    "translate":      {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_GENERAL_MODEL},
    "transcribe":     {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_GENERAL_MODEL},
    "captcha":        {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_GENERAL_MODEL},
    "vision":         {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_VISION_MODEL},
    "ocr":            {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_OCR_MODEL},
    "coding":         {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_CODE_MODEL},
    "tc_review":      {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": TEXT_REVIEW_MODEL},
    "legal_analysis": {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_GENERAL_MODEL},
    "repair_insight_summary": {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_GENERAL_MODEL},
    "reflection":     {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_GENERAL_MODEL},
    "night_talk":     {"day": TEXT_PRIMARY_MODEL, "night": TEXT_PRIMARY_MODEL, "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_GENERAL_MODEL},
    "date_extract":   {"day": "", "night": "", "local": TEXT_PRIMARY_MODEL, "omlx": DEFAULT_OCR_MODEL},
    "embedding":      {"day": "", "night": "", "local": "nomic-embed-text", "omlx": DEFAULT_EMBED_MODEL},
}

# Tasks that benefit from cross-validation (CAPTCHA, date extraction)
_CROSS_VALIDATE_TASKS = {"captcha", "date_extract"}

# Tasks that should get TAIDE review pass
_TAIDE_REVIEW_TASKS = {"summary", "translate", "transcribe", "legal_analysis", "reflection"}


def select_model_for_task(task_type: str, force_quality: bool = False) -> str:
    """Pick the best local model for a task based on time of day."""
    entry = _MODEL_ROSTER.get(task_type, _MODEL_ROSTER.get("general", {}))
    if force_quality or _is_night():
        return entry.get("night", "") or entry.get("local", TEXT_PRIMARY_MODEL)
    return entry.get("day", "") or entry.get("local", TEXT_PRIMARY_MODEL)


class InferenceGateway:
    def __init__(self):
        try:
            from skills.bridge.http_pool import get_session
            self.session = get_session()
        except ImportError:
            self.session = requests.Session()
        self.connect_timeout = float(os.environ.get("INFERENCE_CONNECT_TIMEOUT_SEC", "1.2"))

        self.melchior_url = (getattr(melchior_client, "MELCHIOR_BASE_URL", "") or "").rstrip("/")
        if not self.melchior_url:
            host = os.environ.get("MELCHIOR_HOST", "100.116.54.16")
            port = int(os.environ.get("MELCHIOR_PORT", "5002"))
            self.melchior_url = f"http://{host}:{port}"

        b_host = (os.environ.get("BALTHASAR_HOST") or str(getattr(balthasar_bridge, "BALTHASAR_HOST", "100.118.235.126"))).strip()
        b_port = int(os.environ.get("BALTHASAR_PORT") or str(getattr(balthasar_bridge, "BALTHASAR_PORT", "5002")) or "5002")
        self.balthasar_url = f"http://{b_host}:{b_port}"

        self.local_ollama = (os.environ.get("INFERENCE_LOCAL_OLLAMA_BASE", "http://127.0.0.1:8080") or "http://127.0.0.1:8080").rstrip("/")

        self.melchior_models = self._split_models(os.environ.get("INFERENCE_MELCHIOR_MODELS", TEXT_PRIMARY_MODEL))
        self.balthasar_models = self._split_models(os.environ.get("INFERENCE_BALTHASAR_MODELS", TEXT_PRIMARY_MODEL))
        self.local_chat_models = self._split_models(
            os.environ.get("INFERENCE_LOCAL_CHAT_MODELS", ",".join(default_local_chat_models()))
        )
        self.local_vision_models = self._split_models(
            os.environ.get("INFERENCE_LOCAL_VISION_MODELS", ",".join(default_local_vision_models()))
        )

        try:
            from providers import AnthropicProvider, OllamaProvider, OmlxProvider, OpenAIProvider

            self.provider_adapters = {
                "omlx": OmlxProvider(base_url=self.local_ollama, model=self.local_chat_models[0] if self.local_chat_models else TEXT_PRIMARY_MODEL),
                "ollama": OllamaProvider(base_url=self.local_ollama, model=self.local_chat_models[0] if self.local_chat_models else TEXT_PRIMARY_MODEL),
                "openai": OpenAIProvider(model="gpt-4.1-mini"),
                "anthropic": AnthropicProvider(model="claude-3-5-sonnet-latest"),
            }
        except Exception:
            self.provider_adapters = {}
        self.provider_registry = self._build_provider_registry()

    def classify_intent(self, prompt: str, image_path: str = "", explicit_task_type: str = "") -> str:
        return classify_intent(prompt=prompt, image_path=image_path, explicit_task_type=explicit_task_type)

    def select_model_for_task(self, task_type: str, force_quality: bool = False) -> str:
        return select_model_for_task(task_type=task_type, force_quality=force_quality)

    @staticmethod
    def _split_models(raw: Optional[str]) -> List[str]:
        out: List[str] = []
        for x in str(raw or "").split(","):
            m = x.strip()
            if m and m not in out:
                out.append(m)
        return out

    def _build_provider_registry(self) -> dict[str, object]:
        if callable(_build_provider_registry):
            try:
                return _build_provider_registry(session=self.session)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 211, exc_info=True)
        return {}

    def list_provider_adapters(self) -> list[str]:
        return sorted(str(name) for name in getattr(self, "provider_registry", {}).keys())

    def get_provider_adapter(self, name: str):
        return getattr(self, "provider_registry", {}).get(str(name or "").strip().lower())

    def provider_health_snapshot(self, *, timeout: int = 3) -> dict[str, dict]:
        snapshot: dict[str, dict] = {}
        for name, adapter in getattr(self, "provider_registry", {}).items():
            health = getattr(adapter, "health_check", None)
            if callable(health):
                try:
                    snapshot[name] = health(timeout=timeout).to_dict()
                    continue
                except Exception as exc:
                    snapshot[name] = {"provider": name, "available": False, "detail": str(exc)}
                    continue
            snapshot[name] = {"provider": name, "available": False, "detail": "no_health_check"}
        return snapshot

    @staticmethod
    def _result(
        *,
        success: bool,
        route: str,
        degraded: bool,
        response: str = "",
        analysis: str = "",
        summary: str = "",
        error: str = "",
        model: str = "",
        **extra,
    ) -> dict:
        out = {
            "success": bool(success),
            "route": str(route or "").strip(),
            "degraded": bool(degraded),
            "response": str(response or "").strip(),
            "analysis": str(analysis or "").strip(),
            "summary": str(summary or "").strip(),
            "text": "",
            "error": str(error or "").strip(),
            "model": str(model or "").strip(),
        }
        if not out["analysis"] and out["response"]:
            out["analysis"] = out["response"]
        if not out["response"] and out["analysis"]:
            out["response"] = out["analysis"]
        if not out["summary"] and out["response"]:
            out["summary"] = out["response"]
        out["text"] = out["summary"] or out["response"] or out["analysis"]
        out.update(extra)
        # ── gpt-oss:20b migration watchdog ──────────────────────────
        _used_model = out.get("model", "")
        if "gpt-oss" in _used_model.lower():
            _caller = extra.get("task_type", "unknown")
            _route = out.get("route", "unknown")
            logger.warning(
                "🚨 [MODEL-MIGRATION] gpt-oss:20b was invoked! "
                "task_type=%s route=%s model=%s — this model should have been replaced by %s",
                _caller, _route, _used_model, TEXT_PRIMARY_MODEL,
            )
            try:
                import json as _json
                _log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".agent", "gpt_oss_migration_watchdog.jsonl")
                with open(_log_path, "a", encoding="utf-8") as _f:
                    _f.write(_json.dumps({
                        "ts": datetime.datetime.now().isoformat(),
                        "model": _used_model,
                        "task_type": _caller,
                        "route": _route,
                        "prompt_head": str(extra.get("_prompt_head", ""))[:120],
                    }, ensure_ascii=False) + "\n")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 261, exc_info=True)
        # ─────────────────────────────────────────────────────────────
        return out

    def _timeout_tuple(self, timeout: int) -> Tuple[float, float]:
        rt = max(2.0, float(timeout))
        ct = max(0.4, min(self.connect_timeout, rt))
        return (ct, rt)

    @staticmethod
    def _force_local() -> bool:
        return _env_bool("MELCHIOR_FORCE_LOCAL", False) or _env_bool("INFERENCE_FORCE_LOCAL", False)

    @staticmethod
    def _extract_text(data: dict) -> str:
        if not isinstance(data, dict):
            return ""
        return str(
            data.get("response")
            or data.get("analysis")
            or data.get("text")
            or data.get("summary")
            or ""
        ).strip()

    def _post_json(self, url: str, payload: dict, timeout: int) -> Tuple[bool, dict, str]:
        try:
            r = self.session.post(url, json=payload, timeout=self._timeout_tuple(timeout))
            if r.status_code != 200:
                return False, {}, f"http_{r.status_code}:{(r.text or '')[:220]}"
            data = r.json() if r.text else {}
            if not isinstance(data, dict):
                return False, {}, "non_dict_response"
            return True, data, ""
        except Exception as e:
            return False, {}, str(e)

    def _safe_cb_trip(self, reason: str) -> None:
        fn = getattr(melchior_client, "_cb_trip", None)
        if callable(fn):
            try:
                fn((reason or "unknown")[:200])
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 304, exc_info=True)

    def _safe_cb_reset(self) -> None:
        fn = getattr(melchior_client, "_cb_reset", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 312, exc_info=True)

    def _can_try_remote_melchior(self) -> Tuple[bool, str]:
        if self._force_local():
            return False, "force_local"
        try:
            cb = melchior_client.get_circuit_breaker_status()
            if isinstance(cb, dict) and cb.get("open"):
                return False, "circuit_open"
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 322, exc_info=True)
        probe = getattr(melchior_client, "_remote_online_quick", None)
        if callable(probe):
            try:
                if not bool(probe()):
                    return False, "remote_probe_failed"
            except Exception as e:
                return False, f"remote_probe_error:{e}"
        return True, "ok"

    def _can_try_remote_balthasar(self) -> Tuple[bool, str]:
        if self._force_local():
            return False, "force_local"
        try:
            r = self.session.get(f"{self.balthasar_url}/health", timeout=self._timeout_tuple(3))
            if r.status_code == 200:
                return True, "ok"
            return False, f"health_status_{r.status_code}"
        except Exception as e:
            return False, str(e)

    def _local_ollama_online(self) -> Tuple[bool, List[str], str]:
        # oMLX uses OpenAI-compatible /v1/models (Ollama /api/tags is retired)
        try:
            r = self.session.get(f"{self.local_ollama}/v1/models", timeout=self._timeout_tuple(3))
            if r.status_code != 200:
                return False, [], f"http_{r.status_code}"
            data = r.json() if r.text else {}
            models = []
            for m in (data or {}).get("data", []):
                name = str((m or {}).get("id") or "").strip()
                if name:
                    models.append(name)
            return True, models, ""
        except Exception as e:
            return False, [], str(e)

    def _read_image_b64(self, image_path: str) -> Tuple[Optional[str], str]:
        p = str(image_path or "").strip()
        if not p:
            return None, "missing_image_path"
        if not os.path.exists(p):
            return None, f"file_not_found:{p}"
        try:
            with open(p, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8"), ""
        except Exception as e:
            return None, str(e)

    def _remote_chat_melchior(self, prompt: str, timeout: int, model: str = "") -> dict:
        candidates = [model] if model else (self.melchior_models or [TEXT_PRIMARY_MODEL])
        errors: List[str] = []
        for m in candidates:
            payload = {
                "prompt": prompt,
                "model": m,
                "timeout": int(timeout),
                "keep_alive": os.environ.get("MELCHIOR_KEEP_ALIVE", "10m"),
                "options": {
                    "temperature": float(os.environ.get("MELCHIOR_TEMPERATURE", "0.2") or "0.2"),
                    "num_ctx": int(os.environ.get("MELCHIOR_NUM_CTX", "6144") or "6144"),
                },
            }
            ok, data, err = self._post_json(f"{self.melchior_url}/api/chat", payload, timeout=max(8, int(timeout)))
            if not ok:
                errors.append(f"{m}:{err}")
                continue
            text = self._extract_text(data)
            if text:
                # Detect Melchior backend Ollama being down (returns error text with HTTP 200)
                if "Error connecting to Ollama" in text or "HTTPConnectionPool" in text:
                    errors.append(f"{m}:melchior_backend_down:{text[:120]}")
                    self._safe_cb_trip("melchior_backend_ollama_down")
                    break  # All models on this node will fail the same way
                self._safe_cb_reset()
                return self._result(success=True, route="remote_melchior", degraded=False, response=text, model=m)
            errors.append(f"{m}:empty_response")
        self._safe_cb_trip("gateway_melchior_chat_failed")
        return self._result(success=False, route="remote_melchior", degraded=True, error=" | ".join(errors)[:1000])

    def _remote_vision_melchior(self, image_path: str, prompt: str, timeout: int) -> dict:
        image_b64, read_err = self._read_image_b64(image_path)
        if not image_b64:
            return self._result(success=False, route="remote_melchior", degraded=True, error=read_err)
        # Cap remote vision timeout: if Melchior's Ollama backend is down, it will hang.
        # Use a shorter ceiling so we fall through to local vision sooner.
        mel_vision_timeout = int(os.environ.get("MELCHIOR_VISION_TIMEOUT", "18"))
        ok, data, err = self._post_json(
            f"{self.melchior_url}/api/vision",
            {"prompt": prompt, "image": image_b64},
            timeout=max(8, min(mel_vision_timeout, int(timeout))),
        )
        if not ok:
            self._safe_cb_trip("gateway_melchior_vision_failed")
            return self._result(success=False, route="remote_melchior", degraded=True, error=err)
        text = self._extract_text(data)
        if not text:
            return self._result(success=False, route="remote_melchior", degraded=True, error="empty_vision_response")
        self._safe_cb_reset()
        return self._result(success=True, route="remote_melchior", degraded=False, analysis=text)

    def _remote_chat_balthasar(self, prompt: str, timeout: int, model: str = "") -> dict:
        candidates = [model] if model else (self.balthasar_models or [TEXT_PRIMARY_MODEL])
        errors: List[str] = []
        for m in candidates:
            ok, data, err = self._post_json(
                f"{self.balthasar_url}/api/chat",
                {
                    "prompt": prompt,
                    "model": m,
                    "timeout": int(timeout),
                    "options": {"temperature": 0.2, "num_ctx": 6144},
                },
                timeout=max(8, int(timeout)),
            )
            if ok:
                text = self._extract_text(data)
                if text:
                    return self._result(success=True, route="remote_balthasar", degraded=False, response=text, model=m)
                errors.append(f"/api/chat:{m}:empty_response")
                continue

            errors.append(f"/api/chat:{m}:{err}")
            ok2, data2, err2 = self._post_json(
                f"{self.balthasar_url}/api/generate",
                {"prompt": prompt, "model": m, "stream": False},
                timeout=max(8, int(timeout)),
            )
            if not ok2:
                errors.append(f"/api/generate:{m}:{err2}")
                continue
            text2 = self._extract_text(data2)
            if text2:
                return self._result(success=True, route="remote_balthasar", degraded=False, response=text2, model=m)
            errors.append(f"/api/generate:{m}:empty_response")

        return self._result(success=False, route="remote_balthasar", degraded=True, error=" | ".join(errors)[:1000])

    def _remote_vision_balthasar(self, image_path: str, prompt: str, timeout: int) -> dict:
        image_b64, read_err = self._read_image_b64(image_path)
        if not image_b64:
            return self._result(success=False, route="remote_balthasar", degraded=True, error=read_err)

        ok, data, err = self._post_json(
            f"{self.balthasar_url}/api/vision",
            {"prompt": prompt, "image": image_b64},
            timeout=max(8, int(timeout)),
        )
        if ok:
            text = self._extract_text(data)
            if text:
                return self._result(success=True, route="remote_balthasar", degraded=False, analysis=text)

        ok2, data2, err2 = self._post_json(
            f"{self.balthasar_url}/api/generate",
            {
                "prompt": prompt,
                "images": [image_b64],
                "model": TEXT_PRIMARY_MODEL,
                "stream": False,
            },
            timeout=max(8, int(timeout)),
        )
        if not ok2:
            return self._result(success=False, route="remote_balthasar", degraded=True, error=f"{err} | {err2}".strip(" |"))
        text2 = self._extract_text(data2)
        if not text2:
            return self._result(success=False, route="remote_balthasar", degraded=True, error="empty_vision_response")
        return self._result(success=True, route="remote_balthasar", degraded=False, analysis=text2)

    # Task-specific temperature: summary/translate 需要穩定輸出，chat 需要多樣性
    _TASK_TEMPERATURE = {
        "summary": 0.2,
        "translate": 0.15,
        "legal_analysis": 0.2,
        "tc_review": 0.15,
        "general": 0.3,
        "reflection": 0.4,
    }

    def _omlx_chat(self, prompt: str, timeout: int, model: str = "", task_type: str = "general") -> dict:
        """Try oMLX for chat inference (TAIDE-12b etc.)."""
        chat_omlx = getattr(melchior_client, "_chat_omlx", None)
        omlx_avail = getattr(melchior_client, "_omlx_available", None)
        if not callable(chat_omlx) or not callable(omlx_avail) or not omlx_avail():
            return self._result(success=False, route="omlx", degraded=False, error="omlx_unavailable")

        use_model = model or _MODEL_ROSTER.get(task_type, {}).get("omlx", "")
        if not use_model:
            return self._result(success=False, route="omlx", degraded=False, error="no_omlx_model_for_task")

        temp = self._TASK_TEMPERATURE.get(task_type, 0.3)
        r = chat_omlx(prompt=prompt, model=use_model, timeout=max(10, int(timeout)), temperature=temp, max_tokens=2048)
        if r.get("success"):
            return self._result(success=True, route="omlx", degraded=False, response=r.get("response", ""), model=use_model)
        return self._result(success=False, route="omlx", degraded=False, error=r.get("error", "omlx_failed"))

    def _omlx_vision(self, image_path: str, prompt: str, timeout: int, task_type: str = "vision") -> dict:
        """Try oMLX for vision inference. Routes GLM-OCR to vision port (8082), others to main port (8080)."""
        chat_omlx = getattr(melchior_client, "_chat_omlx", None)
        omlx_avail = getattr(melchior_client, "_omlx_available", None)
        if not callable(chat_omlx) or not callable(omlx_avail) or not omlx_avail():
            return self._result(success=False, route="omlx", degraded=False, error="omlx_unavailable")

        image_b64, read_err = self._read_image_b64(image_path)
        if not image_b64:
            return self._result(success=False, route="omlx", degraded=False, error=read_err)

        use_model = _MODEL_ROSTER.get(task_type, {}).get("omlx", "") or TEXT_PRIMARY_MODEL
        # Vision now uses the same 26B model on port 8080 (GLM-OCR retired)
        kwargs = {}
        vision_base = getattr(melchior_client, "OMLX_VISION_BASE", None)
        if vision_base and vision_base != getattr(melchior_client, "OMLX_CHAT_BASE", ""):
            kwargs["base_url"] = vision_base
        r = chat_omlx(prompt=prompt, model=use_model, timeout=max(10, int(timeout)), temperature=0.3, max_tokens=2048, images=[image_b64], **kwargs)
        if r.get("success"):
            return self._result(success=True, route="omlx", degraded=False, analysis=r.get("response", ""), model=use_model)
        return self._result(success=False, route="omlx", degraded=False, error=r.get("error", "omlx_vision_failed"))

    def _ollama_26b_chat(self, prompt: str, timeout: int, task_type: str = "general",
                         progress_fn=None) -> dict:
        """Route to Ollama Gemma4:26B for heavy tasks."""
        from skills.bridge.tier_router import ensure_26b_ready, OLLAMA_BASE, OLLAMA_MODEL, OLLAMA_KEEP_ALIVE
        try:
            from skills.bridge.http_pool import get_session
        except Exception:
            import requests as _rq
            get_session = _rq.Session

        if not ensure_26b_ready(progress_fn):
            return self._result(success=False, route="ollama_26b", degraded=False, error="26b_load_failed")

        payload = {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "temperature": 0.3,
            "top_p": 0.88,
            "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
        }
        try:
            r = get_session().post(
                f"{OLLAMA_BASE}/v1/chat/completions",
                json=payload,
                timeout=max(30, int(timeout)),
            )
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices") or []
                text = ""
                if choices:
                    msg = choices[0].get("message") or {}
                    text = (msg.get("content") or "").strip()
                if text:
                    return self._result(success=True, route="ollama_26b", degraded=False,
                                        response=text, model=OLLAMA_MODEL, task_type=task_type)
            return self._result(success=False, route="ollama_26b", degraded=False,
                                error=f"ollama_26b_http_{r.status_code}")
        except Exception as e:
            logger.warning("ollama_26b_chat failed: %s", e)
            return self._result(success=False, route="ollama_26b", degraded=False, error=str(e)[:200])

    def _local_chat(self, prompt: str, timeout: int, model_hint: str = "",
                    num_ctx: int = 0, num_predict: int = 0) -> dict:
        ok, models, err = self._local_ollama_online()
        if not ok:
            return self._result(success=False, route="local_ollama", degraded=True, error=f"local_unavailable:{err}")

        candidates = []
        for m in ([model_hint] + self.local_chat_models):
            x = str(m or "").strip()
            if x and x not in candidates:
                candidates.append(x)
        # Resolve legacy aliases (taide / gemma shorthand) to the configured local text model.
        alias_map = getattr(melchior_client, "_OMLX_MODEL_ALIAS", {})
        candidates = [alias_map.get(m, m) for m in candidates]
        if models:
            filtered = [m for m in candidates if m in models]
            if filtered:
                candidates = filtered

        batch = max(1, min(4, len(candidates[:4]) or 1))
        per_try_timeout = max(12, min(45, int(max(12, int(timeout) // batch))))
        errors: List[str] = []
        acquired = _ollama_semaphore.acquire(blocking=True, timeout=max(8, per_try_timeout))
        if not acquired:
            return self._result(success=False, route="local_ollama", degraded=True, error="omlx_queue_full")
        try:
            temperature = float(os.environ.get("MELCHIOR_TEMPERATURE", "0.25") or "0.25")
            max_tokens = num_predict if num_predict > 0 else 2048
            for m in candidates[:4]:
                ok_call, data, call_err = self._post_json(
                    f"{self.local_ollama}/v1/chat/completions",
                    {
                        "model": m,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "top_p": 0.88,
                        "repetition_penalty": 1.1,
                    },
                    timeout=per_try_timeout,
                )
                if not ok_call:
                    if "http_503" in call_err:
                        errors.append(f"{m}:omlx_busy")
                        break
                    errors.append(f"{m}:{call_err}")
                    continue
                # Extract from OpenAI-compatible response
                choices = (data or {}).get("choices") or []
                text = ""
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message") or {}
                    text = str(msg.get("content") or "").strip()
                if text:
                    return self._result(success=True, route="local_ollama", degraded=False, response=text, model=m)
                errors.append(f"{m}:empty_response")
        finally:
            _ollama_semaphore.release()

        return self._result(success=False, route="local_ollama", degraded=True, error=" | ".join(errors)[:1000])

    def _local_vision(self, image_path: str, prompt: str, timeout: int, model_hint: str = "", task_type: str = "vision") -> dict:
        image_b64, read_err = self._read_image_b64(image_path)
        if not image_b64:
            return self._result(success=False, route="local_ollama", degraded=True, error=read_err)

        ok, models, err = self._local_ollama_online()
        if not ok:
            return self._result(success=False, route="local_ollama", degraded=True, error=f"local_unavailable:{err}")

        candidates: List[str] = []
        mh = str(model_hint or "").strip()
        if mh:
            candidates.append(mh)
        for m in self.local_vision_models:
            x = str(m or "").strip()
            if x and x not in candidates:
                candidates.append(x)
        # Resolve model aliases
        alias_map = getattr(melchior_client, "_OMLX_MODEL_ALIAS", {})
        candidates = [alias_map.get(m, m) for m in candidates]
        if models:
            filtered = [m for m in candidates if m in models]
            if filtered:
                candidates = filtered
        if not candidates:
            candidates = [TEXT_PRIMARY_MODEL]

        # For OCR-specific tasks, prefer glm-ocr first (per MODEL_RECOMMENDATIONS)
        if task_type in ("ocr", "date_extract", "stamp", "captcha", "receipt"):
            ocr_first = [m for m in candidates if "glm-ocr" in m.lower() or "GLM-OCR" in m]
            rest = [m for m in candidates if m not in ocr_first]
            candidates = ocr_first + rest

        per_try_timeout = max(25, min(40, int(timeout)))
        errors: List[str] = []
        acquired = _ollama_semaphore.acquire(blocking=True, timeout=max(6, per_try_timeout))
        if not acquired:
            return self._result(success=False, route="local_ollama", degraded=True, error="omlx_queue_full")
        try:
            # Detect image MIME type from extension (imghdr removed in Python 3.13+)
            _ext = os.path.splitext(image_path)[1].lower().lstrip(".")
            img_type = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp", "bmp": "bmp"}.get(_ext, "png")
            mime = f"image/{img_type}"
            for m in candidates[:4]:
                ok_call, data, call_err = self._post_json(
                    f"{self.local_ollama}/v1/chat/completions",
                    {
                        "model": m,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                            ],
                        }],
                        "stream": False,
                        "max_tokens": 2048,
                    },
                    timeout=per_try_timeout,
                )
                if not ok_call:
                    if "http_503" in call_err:
                        errors.append(f"{m}:omlx_busy")
                        break
                    errors.append(f"{m}:{call_err}")
                    continue
                choices = (data or {}).get("choices") or []
                text = ""
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message") or {}
                    text = str(msg.get("content") or "").strip()
                if text:
                    return self._result(success=True, route="local_ollama", degraded=False, analysis=text, model=m)
                errors.append(f"{m}:empty_response")
        finally:
            _ollama_semaphore.release()

        return self._result(success=False, route="local_ollama", degraded=True, error=" | ".join(errors)[:1000])

    def chat(self, prompt: str, task_type: str = "general", timeout: int = 90, model: str = "", **kwargs) -> dict:
        request_id = kwargs.pop("request_id", None) or uuid.uuid4().hex[:12]
        _t0 = time.monotonic()
        result = self._chat_inner(prompt, task_type=task_type, timeout=timeout, model=model, **kwargs)
        _dur_ms = int((time.monotonic() - _t0) * 1000)
        result["request_id"] = request_id
        result["duration_ms"] = _dur_ms
        try:
            logger.info(
                "inference_chat %s",
                _json.dumps({
                    "request_id": request_id,
                    "task_type": task_type,
                    "success": result.get("success"),
                    "route": result.get("route", ""),
                    "model": result.get("model", ""),
                    "degraded": result.get("degraded", False),
                    "duration_ms": _dur_ms,
                    "prompt_len": len(prompt or ""),
                    "response_len": len(result.get("response") or ""),
                    "error": (result.get("error") or "")[:200] if not result.get("success") else "",
                }, ensure_ascii=False),
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 693, exc_info=True)
        return result

    def _chat_inner(self, prompt: str, task_type: str = "general", timeout: int = 90, model: str = "", **kwargs) -> dict:
        prompt = str(prompt or "").strip()
        if not prompt:
            return self._result(success=False, route="failed_all", degraded=True, error="missing_prompt", task_type=task_type)

        errors: List[str] = []
        allow_synthetic_fallback = bool(kwargs.get("allow_synthetic_fallback", True))

        # Try oMLX first for tasks that have an oMLX model configured
        # OCR/date extraction stay on OCR model; review/classify still use the configured local text model.
        omlx_model = _MODEL_ROSTER.get(task_type, {}).get("omlx", "")
        if omlx_model and task_type not in ("tc_review", "captcha", "date_extract"):
            r = self._omlx_chat(prompt, timeout=max(10, int(timeout)), model=omlx_model, task_type=task_type)
            if r.get("success"):
                r["task_type"] = task_type
                return r
            errors.append(f"omlx:{r.get('error','')}")

        # tc_review is a lightweight guard rail. Route it directly to local oMLX/TAIDE
        # instead of probing remotes or the Ollama-style local endpoint.
        if task_type == "tc_review":
            review_model = model or _MODEL_ROSTER.get("tc_review", {}).get("omlx", "") or self.select_model_for_task("tc_review")
            review = self._omlx_chat(
                prompt,
                timeout=max(8, int(timeout)),
                model=review_model,
                task_type="tc_review",
            )
            if review.get("success"):
                review["task_type"] = task_type
                return review
            errors.append(f"omlx:{review.get('error','')}")
            merged_error = " | ".join([e for e in errors if e])[:1200] or "all_routes_failed"
            allow_synthetic_fallback = bool(kwargs.get("allow_synthetic_fallback", True))
            if (_env_bool("INFERENCE_ALLOW_TEXT_FALLBACK", True) or self._force_local()) and allow_synthetic_fallback:
                return self._result(
                    success=True,
                    route="omlx",
                    degraded=True,
                    response="（系統降級回覆）本機模型逾時，請稍後重試。",
                    error=merged_error,
                    task_type=task_type,
                    synthetic_fallback=True,
                )
            return self._result(
                success=False,
                route="failed_all",
                degraded=True,
                error=merged_error,
                task_type=task_type,
            )

        # ── Codex OAuth fallback: when oMLX is down, try Codex before giving up ──
        codex_chat_fallback_enabled = _env_bool("MAGI_CODEX_CHAT_FALLBACK", True)
        if codex_chat_fallback_enabled and allow_synthetic_fallback and errors and task_type not in ("tc_review", "captcha"):
            try:
                from skills.bridge.llm_direct import (
                    feature_enabled as _codex_feat,
                    run_prompt as _codex_run_prompt,
                )
                codex_feature = "summary" if task_type in ("summary", "summarize") else "intent"
                if _codex_feat(codex_feature):
                    logger.info(f"inference_chat: oMLX down, trying Codex OAuth fallback (feature={codex_feature})")
                    codex_r = _codex_run_prompt(
                        feature=codex_feature,
                        prompt=prompt,
                        timeout_sec=max(30, int(timeout)),
                    )
                    codex_text = str(codex_r.get("text") or "").strip()
                    if codex_r.get("success") and codex_text:
                        return self._result(
                            success=True,
                            route="openclaw_codex",
                            degraded=True,
                            response=codex_text,
                            model=str(codex_r.get("model") or "codex"),
                            provider="openai-codex",
                            task_type=task_type,
                        )
                    errors.append(f"codex_fallback:{codex_r.get('error', 'empty')}")
            except Exception as codex_err:
                errors.append(f"codex_fallback:exception:{codex_err}")

        # NOTE: Melchior / Balthasar distributed nodes are not in active use.
        # Skipping remote probe to avoid unnecessary latency.
        # To re-enable, set MAGI_AVOID_DISTRIBUTED=0
        if not _env_bool("MAGI_AVOID_DISTRIBUTED", True):
            mel_ok, mel_reason = self._can_try_remote_melchior()
            if mel_ok:
                r = self._remote_chat_melchior(prompt, timeout=max(8, int(timeout)), model=model)
                if r.get("success"):
                    r["task_type"] = task_type
                    return r
                errors.append(f"remote_melchior:{r.get('error','')}")
            else:
                errors.append(f"remote_melchior:skipped:{mel_reason}")

            bal_ok, bal_reason = self._can_try_remote_balthasar()
            if bal_ok:
                r = self._remote_chat_balthasar(prompt, timeout=max(8, int(timeout)), model=model)
                if r.get("success"):
                    r["task_type"] = task_type
                    return r
                errors.append(f"remote_balthasar:{r.get('error','')}")
            else:
                errors.append(f"remote_balthasar:skipped:{bal_reason}")

        local = self._local_chat(
            prompt, timeout=max(8, int(timeout)), model_hint=model,
            num_ctx=int(kwargs.get("num_ctx") or 0),
            num_predict=int(kwargs.get("num_predict") or 0),
        )
        if local.get("success"):
            local["task_type"] = task_type
            return local
        errors.append(f"local_ollama:{local.get('error','')}")

        merged_error = " | ".join([e for e in errors if e])[:1200] or "all_routes_failed"
        allow_text_fallback = (_env_bool("INFERENCE_ALLOW_TEXT_FALLBACK", True) or self._force_local()) and allow_synthetic_fallback
        if allow_text_fallback:
            return self._result(
                success=True,
                route="local_ollama",
                degraded=True,
                response="（系統降級回覆）本機模型逾時，請稍後重試。",
                error=merged_error,
                task_type=task_type,
                synthetic_fallback=True,
            )

        return self._result(
            success=False,
            route="failed_all",
            degraded=True,
            error=merged_error,
            task_type=task_type,
        )

    def vision(self, image_path: str, prompt: str, timeout: int = 45, task_type: str = "vision", **kwargs) -> dict:
        request_id = kwargs.pop("request_id", None) or uuid.uuid4().hex[:12]
        _t0 = time.monotonic()
        result = self._vision_inner(image_path, prompt, timeout=timeout, task_type=task_type, **kwargs)
        _dur_ms = int((time.monotonic() - _t0) * 1000)
        result["request_id"] = request_id
        result["duration_ms"] = _dur_ms
        try:
            logger.info(
                "inference_vision %s",
                _json.dumps({
                    "request_id": request_id,
                    "task_type": task_type,
                    "success": result.get("success"),
                    "route": result.get("route", ""),
                    "model": result.get("model", ""),
                    "duration_ms": _dur_ms,
                    "error": (result.get("error") or "")[:200] if not result.get("success") else "",
                }, ensure_ascii=False),
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 855, exc_info=True)
        return result

    def _vision_inner(self, image_path: str, prompt: str, timeout: int = 45, task_type: str = "vision", **kwargs) -> dict:
        errors: List[str] = []
        force_local = bool(kwargs.get("force_local", False)) or self._force_local()
        model_hint = str(kwargs.get("model") or "").strip()
        codex_ocr_tasks = {"ocr", "vision-ocr", "captcha", "date_extract", "stamp", "receipt", "read-text", "text"}
        codex_prompt_raw = str(prompt or "")
        codex_prompt_lower = codex_prompt_raw.lower()
        codex_ocr_like = (
            task_type in codex_ocr_tasks
            or "ocr" in codex_prompt_lower
            or "辨識文字" in codex_prompt_raw
            or "讀取文字" in codex_prompt_raw
            or "擷取文字" in codex_prompt_raw
        )

        # OCR-first path for text-heavy image tasks. This gives Codex structured text instead
        # of making it infer directly from a local file path, which is less reliable.
        ocr_context = ""
        if codex_ocr_like or task_type in ("vision",):
            ocr_r = self._omlx_vision(image_path, "Extract all text from this image exactly as shown.", timeout=min(20, max(8, int(timeout) // 2)), task_type="ocr")
            if ocr_r.get("success") and ocr_r.get("analysis", "").strip():
                ocr_context = ocr_r["analysis"].strip()
                logger.info("vision: GLM-OCR pre-scan extracted %d chars", len(ocr_context))

        try:
            from skills.bridge.llm_direct import (
                analyze_image_with_codex,
                feature_enabled as _codex_feature_enabled,
                refine_ocr_with_codex,
            )

            # Allow Codex vision when: (a) OCR-like task, (b) explicitly enabled,
            # or (c) oMLX is down (detected by prior oMLX OCR scan failure)
            omlx_appears_down = not ocr_context and (codex_ocr_like or task_type in ("vision",))
            codex_vision_allowed = _codex_feature_enabled("vision") and (
                codex_ocr_like
                or omlx_appears_down
                or os.environ.get("MAGI_CODEX_DIRECT_VISION_ENABLE", "0").strip().lower() in {"1", "true", "yes", "on"}
            )
            if codex_vision_allowed:
                if codex_ocr_like and ocr_context:
                    codex_res = refine_ocr_with_codex(
                        ocr_context,
                        user_prompt=prompt,
                        timeout_sec=int(os.environ.get("MAGI_CODEX_VISION_TIMEOUT_SEC", str(max(60, int(timeout) * 2))) or str(max(60, int(timeout) * 2))),
                    )
                else:
                    codex_res = analyze_image_with_codex(
                        image_path,
                        user_prompt=prompt,
                        task_type=task_type,
                        timeout_sec=int(os.environ.get("MAGI_CODEX_VISION_TIMEOUT_SEC", str(max(60, int(timeout) * 2))) or str(max(60, int(timeout) * 2))),
                    )
                codex_text = str(codex_res.get("text") or "").strip()
                if codex_res.get("success") and codex_text:
                    return self._result(
                        success=True,
                        route="openclaw_codex",
                        degraded=False,
                        analysis=codex_text,
                        model=str(codex_res.get("model") or "gpt-5.4"),
                        provider="openai-codex",
                        task_type=task_type,
                    )
                if codex_res.get("error"):
                    errors.append(f"openclaw_codex:{codex_res.get('error')}")
        except Exception as codex_err:
            errors.append(f"openclaw_codex:exception:{codex_err}")

        # Try oMLX vision (Gemma-3 multimodal) with OCR context
        omlx_model = _MODEL_ROSTER.get(task_type, {}).get("omlx", "")
        if omlx_model:
            enriched_prompt = prompt
            if ocr_context:
                enriched_prompt = f"{prompt}\n\n[OCR 參考文字]\n{ocr_context[:2000]}"
            r = self._omlx_vision(image_path, enriched_prompt, timeout=max(10, int(timeout)), task_type=task_type)
            if r.get("success"):
                r["task_type"] = task_type
                if ocr_context:
                    r["ocr_context"] = ocr_context
                # Vision model 輸出可能含簡體 → TAIDE TC review 轉繁體（Gemma-3 通常直接輸出繁體）
                raw = r.get("analysis", "")
                if raw and task_type != "ocr":
                    try:
                        from skills.bridge.balthasar_bridge import _tc_review_pass
                        reviewed = _tc_review_pass(raw, timeout=25)
                        if reviewed and len(reviewed) > len(raw) * 0.5:
                            r["analysis"] = reviewed
                            r["tc_reviewed"] = True
                            logger.info("vision: TC review applied (%d → %d chars)", len(raw), len(reviewed))
                    except Exception as _tc_e:
                        logger.warning("vision: TC review failed, using raw: %s", _tc_e)
                return r
            errors.append(f"omlx:{r.get('error','')}")

        if not force_local:
            mel_ok, mel_reason = self._can_try_remote_melchior()
            if mel_ok:
                r = self._remote_vision_melchior(image_path, prompt, timeout=max(8, int(timeout)))
                if r.get("success"):
                    r["task_type"] = task_type
                    return r
                errors.append(f"remote_melchior:{r.get('error','')}")
            else:
                errors.append(f"remote_melchior:skipped:{mel_reason}")

            bal_ok, bal_reason = self._can_try_remote_balthasar()
            if bal_ok:
                r = self._remote_vision_balthasar(image_path, prompt, timeout=max(8, int(timeout)))
                if r.get("success"):
                    r["task_type"] = task_type
                    return r
                errors.append(f"remote_balthasar:{r.get('error','')}")
            else:
                errors.append(f"remote_balthasar:skipped:{bal_reason}")
        else:
            errors.append("remote_melchior:skipped:force_local")
            errors.append("remote_balthasar:skipped:force_local")

        local = self._local_vision(image_path, prompt, timeout=max(8, int(timeout)), model_hint=model_hint, task_type=task_type)
        if local.get("success"):
            local["task_type"] = task_type
            return local
        errors.append(f"local_ollama:{local.get('error','')}")

        return self._result(
            success=False,
            route="failed_all",
            degraded=True,
            error=" | ".join([e for e in errors if e])[:1200] or "all_routes_failed",
            task_type=task_type,
        )

    def summarize(self, text: str, context: str = "", timeout: int = 120, task_type: str = "summary", **kwargs) -> dict:
        content = str(text or "").strip()
        if not content:
            return self._result(success=False, route="failed_all", degraded=True, error="missing_text", task_type=task_type)

        context_text = str(context or "").strip()
        prompt = (
            "Please summarize in Traditional Chinese and keep: facts, issues, court reasoning, statutes, and outcome.\n\n"
            + (f"Context: {context_text}\n\n" if context_text else "")
            + "Source:\n"
            + content
        )
        model_hint = str(kwargs.get("model") or self.select_model_for_task(task_type)).strip()
        r = self.chat(
            prompt=prompt,
            task_type=task_type,
            timeout=max(20, int(timeout)),
            model=model_hint,
            allow_synthetic_fallback=False,
        )
        if r.get("success"):
            summary = str(r.get("response") or r.get("text") or "").strip()
            r["summary"] = summary
            r["text"] = summary
            return r

        compact = " ".join(content.replace("\n", " ").split())
        fallback = compact[:260] + ("…" if len(compact) > 260 else "")
        return self._result(
            success=True,
            route="local_ollama",
            degraded=True,
            response=f"（系統降級回覆）{fallback}",
            summary=f"（系統降級回覆）{fallback}",
            error=str(r.get("error") or "summarize_failed"),
            task_type=task_type,
        )

    @staticmethod
    def _normalize_vote(text: str, task_type: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        tt = str(task_type or "").lower()
        if "captcha" in tt:
            chars = re.findall(r"[A-Za-z0-9]", raw)
            return "".join(chars).upper() if chars else ""
        if "date" in tt:
            m = re.search(r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", raw)
            if m:
                x = m.group(1).replace("年", "-").replace("月", "-").replace("日", "")
                x = x.replace("/", "-").replace(".", "-")
                return re.sub(r"-+", "-", x).strip("-")
        return re.sub(r"\s+", " ", raw).strip().lower()

    def cross_validate(
        self,
        *,
        task_type: str,
        prompt: str,
        timeout: int = 45,
        image_path: str = "",
        mode: str = "chat",
    ) -> dict:
        started = time.time()
        mode = str(mode or "chat").strip().lower()
        votes: List[dict] = []

        mel_ok, _ = self._can_try_remote_melchior()
        if mel_ok:
            r = self._remote_vision_melchior(image_path, prompt, timeout) if mode == "vision" else self._remote_chat_melchior(prompt, timeout)
            if r.get("success"):
                raw = r.get("analysis") if mode == "vision" else r.get("response")
                votes.append({"route": "remote_melchior", "raw": str(raw or ""), "norm": self._normalize_vote(raw or "", task_type)})

        bal_ok, _ = self._can_try_remote_balthasar()
        if bal_ok:
            r = self._remote_vision_balthasar(image_path, prompt, timeout) if mode == "vision" else self._remote_chat_balthasar(prompt, timeout)
            if r.get("success"):
                raw = r.get("analysis") if mode == "vision" else r.get("response")
                votes.append({"route": "remote_balthasar", "raw": str(raw or ""), "norm": self._normalize_vote(raw or "", task_type)})

        local = self._local_vision(image_path, prompt, timeout) if mode == "vision" else self._local_chat(prompt, timeout)
        if local.get("success"):
            raw = local.get("analysis") if mode == "vision" else local.get("response")
            votes.append({"route": "local_ollama", "raw": str(raw or ""), "norm": self._normalize_vote(raw or "", task_type)})

        valid = [v for v in votes if v.get("norm")]
        if not valid:
            return self._result(
                success=False,
                route="cross_validate",
                degraded=True,
                error="no_successful_votes",
                confidence="low",
                votes=votes,
                task_type=task_type,
            )

        counts = Counter(v["norm"] for v in valid)
        winner_norm, winner_count = counts.most_common(1)[0]
        winner = next((v for v in valid if v["norm"] == winner_norm), valid[0])
        confidence = "high" if winner_count >= 2 else "low"
        route = str(winner["route"])
        degraded = route == "local_ollama"

        out = self._result(
            success=True,
            route=route,
            degraded=degraded,
            response=str(winner["raw"]),
            analysis=str(winner["raw"]) if mode == "vision" else "",
            cross_validated=True,
            confidence=confidence,
            agree_count=int(winner_count),
            winner_norm=str(winner_norm),
            votes=valid,
            mode=mode,
            task_type=task_type,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return out

    # ------------------------------------------------------------------
    # Smart Dispatch: auto-classify intent → pick model → route
    # ------------------------------------------------------------------

    def dispatch(self, prompt: str, image_path: str = "",
                 task_type: str = "", timeout: int = 90,
                 force_quality: bool = False,
                 tc_review: bool = None,
                 cross_validate: bool = None,
                 **kwargs) -> dict:
        """
        Smart entry point: auto-classifies intent and routes to the best
        handler with the right model.

        Args:
            prompt: The prompt / input text
            image_path: If provided, routes to vision pipeline
            task_type: Optional explicit override (skips classifier)
            timeout: Max seconds
            force_quality: True = always use night (heavy) model
            tc_review: True/False/None. None = auto (apply for summary/translate)
            cross_validate: True/False/None. None = auto (apply for captcha/date)
        """
        # Step 1: Classify intent
        detected = self.classify_intent(prompt, image_path, task_type)
        logger.info("dispatch: intent=%s (explicit=%s) night=%s force_q=%s",
                    detected, task_type, _is_night(), force_quality)

        # Step 2: Pick model based on task + time
        model_hint = self.select_model_for_task(detected, force_quality=force_quality)

        # Step 3: Decide whether to cross-validate
        do_xv = cross_validate if cross_validate is not None else (detected in _CROSS_VALIDATE_TASKS)
        if do_xv:
            mode = "vision" if image_path else "chat"
            result = self.cross_validate(
                task_type=detected, prompt=prompt, timeout=timeout,
                image_path=image_path, mode=mode,
            )
            result["intent"] = detected
            result["model_hint"] = model_hint
            return result

        # Step 4: Route to the right handler
        if image_path and detected in ("vision", "captcha"):
            result = self.vision(image_path, prompt, timeout=timeout, task_type=detected)
        elif detected == "summary":
            result = self.summarize(prompt, timeout=timeout, task_type=detected, model=model_hint)
        else:
            result = self.chat(
                prompt=prompt, task_type=detected,
                timeout=timeout, model=model_hint,
            )

        result["intent"] = detected
        result["model_hint"] = model_hint

        # Step 5: TAIDE review pass
        do_tc = tc_review if tc_review is not None else (detected in _TAIDE_REVIEW_TASKS)
        if do_tc and result.get("success"):
            result = self._apply_taide_review(result)

        return result

    def _apply_taide_review(self, result: dict, timeout: int = 60) -> dict:
        """
        Post-process: send the output through TAIDE to correct
        Traditional Chinese (Taiwan) terminology.
        """
        raw_text = str(result.get("response") or result.get("text") or "").strip()
        if not raw_text or len(raw_text) < 10:
            result["tc_reviewed"] = False
            return result

        taide_model = self.select_model_for_task("tc_review")
        review_prompt = (
            "你是台灣繁體中文校正助理。請檢查以下文字的用語是否符合台灣正體中文習慣，"
            "修正：簡體字→正體字、中國用語→台灣用語（如 信息→資訊、軟件→軟體、數據→資料）、"
            "法律術語需符合台灣法律慣用語。\n\n"
            f"待檢查文字：\n{raw_text}\n\n"
            "請直接輸出校正後的完整文字，不需解釋修改處。"
        )

        try:
            reviewed = self._local_chat(review_prompt, timeout=timeout, model_hint=taide_model)
            if reviewed.get("success"):
                corrected = str(reviewed.get("response") or "").strip()
                # Sanity check: corrected text should be similar length
                if corrected and len(corrected) > len(raw_text) * 0.3:
                    result["response"] = corrected
                    result["summary"] = corrected
                    result["text"] = corrected
                    result["tc_reviewed"] = True
                    result["tc_model"] = taide_model
                    return result
        except Exception as e:
            logger.warning("TAIDE review failed (non-fatal): %s", e)

        result["tc_reviewed"] = False
        return result

    def check_topology(self) -> dict:
        mel_ok, mel_reason = self._can_try_remote_melchior()
        bal_ok, bal_reason = self._can_try_remote_balthasar()
        loc_ok, loc_models, loc_reason = self._local_ollama_online()

        cb = {}
        try:
            cb = melchior_client.get_circuit_breaker_status()
        except Exception:
            cb = {}

        available = []
        if mel_ok:
            available.append("remote_melchior")
        if bal_ok:
            available.append("remote_balthasar")
        if loc_ok:
            available.append("local_ollama")

        return {
            "success": True,
            "order": ["remote_melchior", "remote_balthasar", "local_ollama"],
            "available_routes": available,
            "nodes": {
                "remote_melchior": {"online": bool(mel_ok), "reason": mel_reason},
                "remote_balthasar": {"online": bool(bal_ok), "reason": bal_reason},
                "local_ollama": {"online": bool(loc_ok), "reason": loc_reason, "models": loc_models[:20]},
            },
            "circuit_breaker": cb,
            "forced_local": self._force_local(),
        }
