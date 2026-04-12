from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("OpenClawCodexBridge")

MAGI_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = Path(
    os.environ.get(
        "MAGI_CODEX_DISTRIBUTED_POLICY_PATH",
        str(MAGI_ROOT / ".agent" / "codex_distributed_policy.json"),
    )
)
RUNTIME_STATE_PATH = Path(
    os.environ.get(
        "MAGI_CODEX_DISTRIBUTED_RUNTIME_PATH",
        str(MAGI_ROOT / ".agent" / "codex_distributed_runtime.json"),
    )
)
OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
OPENCLAW_AUTH_PROFILES_PATH = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
OPENCLAW_PATH_EXTRAS = ["/opt/homebrew/bin", "/usr/local/bin"]

DEFAULT_AGENT_ID = os.environ.get("MAGI_CODEX_DISTRIBUTED_AGENT_ID", "codex-distributed").strip() or "codex-distributed"
DEFAULT_THINKING = os.environ.get("MAGI_CODEX_DISTRIBUTED_THINKING", "high").strip() or "high"
DEFAULT_TIMEOUT_SEC = max(30, int(os.environ.get("MAGI_CODEX_DISTRIBUTED_TIMEOUT_SEC", "180") or "180"))
DEFAULT_FAILURE_THRESHOLD = max(1, int(os.environ.get("MAGI_CODEX_DISTRIBUTED_FAIL_THRESHOLD", "3") or "3"))
DEFAULT_FAILURE_COOLDOWN_SEC = max(60, int(os.environ.get("MAGI_CODEX_DISTRIBUTED_FAIL_COOLDOWN_SEC", "120") or "120"))
TIMEOUT_COOLDOWN_SEC = max(60, int(os.environ.get("MAGI_CODEX_DISTRIBUTED_TIMEOUT_COOLDOWN_SEC", "180") or "180"))
AUTH_COOLDOWN_SEC = max(300, int(os.environ.get("MAGI_CODEX_DISTRIBUTED_AUTH_COOLDOWN_SEC", "3600") or "3600"))
INCLUDE_RAW = os.environ.get("MAGI_CODEX_DISTRIBUTED_INCLUDE_RAW", "0").strip().lower() in {"1", "true", "yes", "on"}

DEFAULT_FEATURES = {
    "summary": True,
    "translate": True,
    "vision": True,
    "intent": True,
    "transcript": True,
}

FEATURE_ALIASES = {
    "summarize": "summary",
    "summary": "summary",
    "translate": "translate",
    "translation": "translate",
    "vision": "vision",
    "ocr": "vision",
    "captcha": "vision",
    "image": "vision",
    "intent": "intent",
    "router": "intent",
    "routing": "intent",
    "transcript": "transcript",
    "transcribe": "transcript",
    "stt": "transcript",
    "audio": "transcript",
}

FEATURE_PROFILES = {
    "summary": {"thinking": "low", "timeout_sec": 90},
    "translate": {"thinking": "low", "timeout_sec": 90},
    "vision": {"thinking": "medium", "timeout_sec": 180},
    "intent": {"thinking": "minimal", "timeout_sec": 45},
    "transcript": {"thinking": "low", "timeout_sec": 120},
}
OCR_TASK_TYPES = {"ocr", "vision-ocr", "text", "read-text", "captcha", "date_extract", "stamp", "receipt"}


def _exec_env() -> dict[str, str]:
    env = os.environ.copy()
    base_path = env.get("PATH", "")
    env["PATH"] = ":".join([p for p in OPENCLAW_PATH_EXTRAS + [base_path] if p])
    return env


def _openclaw_bin() -> str:
    env = _exec_env()
    found = shutil.which("openclaw", path=env.get("PATH"))
    return found or "openclaw"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = payload if isinstance(payload, dict) else {}
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def _normalize_feature_name(feature: str) -> str:
    raw = str(feature or "").strip().lower()
    return FEATURE_ALIASES.get(raw, raw)


def normalize_feature_name(feature: str) -> str:
    return _normalize_feature_name(feature)


def _context_mode_override() -> str:
    mode = str(os.environ.get("MAGI_CODEX_CONTEXT_MODE") or "").strip().lower()
    if mode in {"auto", "local", "codex"}:
        return mode
    return ""


def _normalize_feature_updates(raw: Any, default_value: bool = True) -> dict[str, bool]:
    updates: dict[str, bool] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            name = _normalize_feature_name(str(key or ""))
            if name in DEFAULT_FEATURES:
                updates[name] = bool(value)
        return updates
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            name = _normalize_feature_name(str(item or ""))
            if name in DEFAULT_FEATURES:
                updates[name] = bool(default_value)
        return updates
    for token in str(raw or "").replace("|", ",").split(","):
        name = _normalize_feature_name(token)
        if name in DEFAULT_FEATURES:
            updates[name] = bool(default_value)
    return updates


def _default_policy() -> dict[str, Any]:
    return {
        "enabled": False,
        "agent_id": DEFAULT_AGENT_ID,
        "thinking": DEFAULT_THINKING,
        "timeout_sec": DEFAULT_TIMEOUT_SEC,
        "features": dict(DEFAULT_FEATURES),
        "updated_at": "",
    }


def _default_runtime_state() -> dict[str, Any]:
    return {
        "consecutive_failures": 0,
        "cooldown_until_ts": 0,
        "cooldown_reason": "",
        "last_feature": "",
        "last_error": "",
        "last_failure_at": "",
        "last_success_at": "",
        "last_duration_ms": 0,
        "last_provider": "",
        "last_model": "",
        "last_usage_total": 0,
        "last_system_prompt_chars": 0,
        "updated_at": "",
    }


def _normalize_policy(data: dict[str, Any] | None) -> dict[str, Any]:
    base = _default_policy()
    payload = data if isinstance(data, dict) else {}

    base["enabled"] = bool(payload.get("enabled", base["enabled"]))
    agent_id = str(payload.get("agent_id") or base["agent_id"]).strip()
    if agent_id:
        base["agent_id"] = agent_id
    thinking = str(payload.get("thinking") or base["thinking"]).strip().lower()
    if thinking in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        base["thinking"] = thinking
    try:
        timeout_sec = int(payload.get("timeout_sec") or base["timeout_sec"])
    except Exception:
        timeout_sec = int(base["timeout_sec"])
    base["timeout_sec"] = max(30, min(timeout_sec, 1800))
    if str(payload.get("updated_at") or "").strip():
        base["updated_at"] = str(payload.get("updated_at")).strip()

    features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    merged = dict(DEFAULT_FEATURES)
    for key, value in features.items():
        name = _normalize_feature_name(str(key or ""))
        if name in merged:
            merged[name] = bool(value)
    base["features"] = merged
    return base


def _normalize_runtime_state(data: dict[str, Any] | None) -> dict[str, Any]:
    base = _default_runtime_state()
    payload = data if isinstance(data, dict) else {}
    for key in ("cooldown_reason", "last_feature", "last_error", "last_failure_at", "last_success_at", "last_provider", "last_model", "updated_at"):
        if str(payload.get(key) or "").strip():
            base[key] = str(payload.get(key)).strip()
    for key in ("consecutive_failures", "cooldown_until_ts", "last_duration_ms", "last_usage_total", "last_system_prompt_chars"):
        try:
            value = int(payload.get(key) or 0)
        except Exception:
            value = 0
        base[key] = max(0, value)
    return base


def load_policy() -> dict[str, Any]:
    return _normalize_policy(_load_json(POLICY_PATH))


def load_runtime_state() -> dict[str, Any]:
    return _normalize_runtime_state(_load_json(RUNTIME_STATE_PATH))


def save_policy(policy: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_policy(policy)
    normalized["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return _save_json(POLICY_PATH, normalized)


def save_runtime_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_runtime_state(state)
    normalized["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return _save_json(RUNTIME_STATE_PATH, normalized)


def update_policy(*, enabled: Optional[bool] = None, features: dict[str, bool] | None = None) -> dict[str, Any]:
    policy = load_policy()
    if enabled is not None:
        policy["enabled"] = bool(enabled)
    merged_features = dict(policy.get("features") or {})
    if isinstance(features, dict):
        for key, value in features.items():
            name = _normalize_feature_name(str(key or ""))
            if name in DEFAULT_FEATURES:
                merged_features[name] = bool(value)
    policy["features"] = merged_features
    return save_policy(policy)


def feature_enabled(feature: str) -> bool:
    context_mode = _context_mode_override()
    if context_mode == "local":
        return False
    policy = load_policy()
    name = _normalize_feature_name(feature)
    return bool(policy.get("enabled")) and bool((policy.get("features") or {}).get(name, False))


def _runtime_ready(state: dict[str, Any]) -> tuple[bool, int]:
    now_ts = int(time.time())
    cooldown_until_ts = int(state.get("cooldown_until_ts") or 0)
    remaining = max(0, cooldown_until_ts - now_ts)
    return remaining == 0, remaining


def _feature_profile(feature_name: str) -> dict[str, Any]:
    base = dict(FEATURE_PROFILES.get(feature_name) or {})
    env_prefix = f"MAGI_CODEX_{feature_name.upper()}"
    thinking = str(os.environ.get(f"{env_prefix}_THINKING", base.get("thinking") or "")).strip().lower()
    if thinking not in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        thinking = str(base.get("thinking") or DEFAULT_THINKING).strip().lower() or DEFAULT_THINKING
    try:
        timeout_sec = int(os.environ.get(f"{env_prefix}_TIMEOUT_SEC", str(base.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)) or str(base.get("timeout_sec") or DEFAULT_TIMEOUT_SEC))
    except Exception:
        timeout_sec = int(base.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
    return {
        "thinking": thinking,
        "timeout_sec": max(30, min(timeout_sec, 1800)),
    }


def _system_prompt_stats(parsed: dict[str, Any]) -> dict[str, int]:
    meta = (parsed.get("meta") or (parsed.get("result") or {}).get("meta") or {}) if isinstance(parsed, dict) else {}
    report = meta.get("systemPromptReport") if isinstance(meta.get("systemPromptReport"), dict) else {}
    system_prompt = report.get("systemPrompt") if isinstance(report.get("systemPrompt"), dict) else {}
    skills = report.get("skills") if isinstance(report.get("skills"), dict) else {}
    return {
        "system_prompt_chars": int(system_prompt.get("chars") or 0),
        "project_context_chars": int(system_prompt.get("projectContextChars") or 0),
        "non_project_context_chars": int(system_prompt.get("nonProjectContextChars") or 0),
        "skill_prompt_chars": int(skills.get("promptChars") or 0),
    }


def _local_ocr_extract(image_path: str) -> dict[str, Any]:
    tesseract_bin = shutil.which("tesseract", path=_exec_env().get("PATH"))
    if not tesseract_bin:
        return {"success": False, "error": "tesseract_missing"}

    langs: list[str] = []
    for candidate in (
        os.environ.get("MAGI_CODEX_VISION_TESSERACT_LANG", "eng+chi_tra"),
        "eng+chi_tra",
        "eng",
        "chi_tra",
    ):
        value = str(candidate or "").strip()
        if value and value not in langs:
            langs.append(value)

    last_error = "tesseract_empty"
    for lang in langs:
        command = [tesseract_bin, str(image_path or "").strip(), "stdout", "-l", lang, "--psm", "6"]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                timeout=30,
                env=_exec_env(),
            )
        except subprocess.TimeoutExpired:
            last_error = "tesseract_timeout"
            continue
        except Exception as exc:
            last_error = f"tesseract_exec_failed:{type(exc).__name__}"
            continue

        stdout_text = bytes(proc.stdout or b"").decode("utf-8", "replace").strip()
        stderr_text = bytes(proc.stderr or b"").decode("utf-8", "replace").strip()
        text = stdout_text
        if proc.returncode == 0 and text:
            return {
                "success": True,
                "text": text,
                "lang": lang,
                "stderr_tail": stderr_text[-400:],
            }
        stderr = stderr_text.lower()
        if "failed loading language" in stderr:
            last_error = f"tesseract_lang_unavailable:{lang}"
        elif stderr:
            last_error = stderr[-200:]
    return {"success": False, "error": last_error}


def _cooldown_seconds_for_error(error: str) -> int:
    lowered = str(error or "").lower()
    if any(token in lowered for token in ("rate_limit", "quota", "billing", "oauth", "auth_unavailable")):
        return AUTH_COOLDOWN_SEC
    if "timeout" in lowered:
        return TIMEOUT_COOLDOWN_SEC
    return DEFAULT_FAILURE_COOLDOWN_SEC


def _record_failure(feature_name: str, error: str) -> dict[str, Any]:
    state = load_runtime_state()
    failures = int(state.get("consecutive_failures") or 0) + 1
    state["consecutive_failures"] = failures
    state["last_feature"] = feature_name
    state["last_error"] = str(error or "").strip()[:500]
    state["last_failure_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if failures >= DEFAULT_FAILURE_THRESHOLD:
        cooldown = _cooldown_seconds_for_error(error)
        state["cooldown_until_ts"] = max(int(state.get("cooldown_until_ts") or 0), int(time.time()) + cooldown)
        state["cooldown_reason"] = str(error or "").strip()[:200]
    return save_runtime_state(state)


def _record_success(feature_name: str, *, duration_ms: int, provider: str, model: str, usage_total: int, system_prompt_chars: int) -> dict[str, Any]:
    state = load_runtime_state()
    state["consecutive_failures"] = 0
    state["cooldown_until_ts"] = 0
    state["cooldown_reason"] = ""
    state["last_error"] = ""
    state["last_feature"] = feature_name
    state["last_success_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["last_duration_ms"] = max(0, int(duration_ms))
    state["last_provider"] = str(provider or "").strip()
    state["last_model"] = str(model or "").strip()
    state["last_usage_total"] = max(0, int(usage_total))
    state["last_system_prompt_chars"] = max(0, int(system_prompt_chars))
    return save_runtime_state(state)


def _codex_agent_info(agent_id: str) -> dict[str, Any]:
    cfg = _load_json(OPENCLAW_CONFIG_PATH)
    agents = ((cfg.get("agents") or {}).get("list") or []) if isinstance(cfg.get("agents"), dict) else []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if str(agent.get("id") or "").strip() == agent_id:
            return agent
    return {}


def _codex_oauth_ready() -> tuple[bool, str]:
    data = _load_json(OPENCLAW_AUTH_PROFILES_PATH)
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("provider") or "").strip() != "openai-codex":
            continue
        auth_type = str(profile.get("type") or profile.get("authType") or "").strip().lower()
        has_api_key = bool(str(profile.get("apiKey") or "").strip())
        if auth_type == "oauth" and not has_api_key:
            return True, str(name)
    return False, ""


def status_report() -> dict[str, Any]:
    policy = load_policy()
    runtime = load_runtime_state()
    agent_id = str(policy.get("agent_id") or DEFAULT_AGENT_ID).strip() or DEFAULT_AGENT_ID
    agent = _codex_agent_info(agent_id)
    oauth_ready, oauth_profile = _codex_oauth_ready()
    runtime_ready, cooldown_remaining_sec = _runtime_ready(runtime)
    return {
        "enabled": bool(policy.get("enabled")),
        "policy_path": str(POLICY_PATH),
        "policy": policy,
        "runtime_state_path": str(RUNTIME_STATE_PATH),
        "runtime_state": runtime,
        "runtime_ready": runtime_ready,
        "runtime_cooldown_remaining_sec": cooldown_remaining_sec,
        "agent_id": agent_id,
        "agent_available": bool(agent),
        "agent": agent,
        "oauth_ready": oauth_ready,
        "oauth_profile": oauth_profile,
        "feature_profiles": {name: _feature_profile(name) for name in sorted(DEFAULT_FEATURES)},
        "config_path": str(OPENCLAW_CONFIG_PATH),
        "auth_profiles_path": str(OPENCLAW_AUTH_PROFILES_PATH),
    }


def public_status_report(*, can_toggle: Optional[bool] = None) -> dict[str, Any]:
    report = status_report()
    policy = report.get("policy") if isinstance(report.get("policy"), dict) else {}
    runtime = report.get("runtime_state") if isinstance(report.get("runtime_state"), dict) else {}
    features = policy.get("features") if isinstance(policy.get("features"), dict) else {}
    agent = report.get("agent") if isinstance(report.get("agent"), dict) else {}

    if not report.get("enabled"):
        mode_code = "LOCAL_ONLY"
        mode_label = "Local only"
    elif not report.get("agent_available") or not report.get("oauth_ready"):
        mode_code = "CODEX_BLOCKED"
        mode_label = "Codex blocked"
    elif not report.get("runtime_ready"):
        mode_code = "WAITING_RECOVERY"
        mode_label = "Waiting recovery"
    else:
        mode_code = "CODEX_READY"
        mode_label = "Codex ready"

    return {
        "ok": True,
        "mode_code": mode_code,
        "mode_label": mode_label,
        "enabled": bool(report.get("enabled")),
        "can_toggle": bool(can_toggle) if can_toggle is not None else False,
        "features": {name: bool(features.get(name, False)) for name in sorted(DEFAULT_FEATURES)},
        "runtime_ready": bool(report.get("runtime_ready")),
        "runtime_cooldown_remaining_sec": int(report.get("runtime_cooldown_remaining_sec") or 0),
        "cooldown_reason": str(runtime.get("cooldown_reason") or ""),
        "last_feature": str(runtime.get("last_feature") or ""),
        "last_error": str(runtime.get("last_error") or ""),
        "last_success_at": str(runtime.get("last_success_at") or ""),
        "last_duration_ms": int(runtime.get("last_duration_ms") or 0),
        "last_system_prompt_chars": int(runtime.get("last_system_prompt_chars") or 0),
        "agent_id": str(report.get("agent_id") or ""),
        "agent_available": bool(report.get("agent_available")),
        "agent_model": str(agent.get("model") or ""),
        "agent_workspace": str(agent.get("workspace") or ""),
        "oauth_ready": bool(report.get("oauth_ready")),
        "oauth_profile": str(report.get("oauth_profile") or ""),
    }


def apply_manual_command(command: str, *, features: Any = None) -> dict[str, Any]:
    normalized = str(command or "").strip().lower()
    if normalized in {"status", "show"}:
        return status_report()

    policy = load_policy()
    merged = dict(policy.get("features") or {})

    if normalized in {"on", "enable"}:
        raw_features = list(features.keys()) if isinstance(features, dict) else features
        updates = _normalize_feature_updates(raw_features, default_value=True)
        if not updates:
            updates = dict(DEFAULT_FEATURES)
        for key, value in updates.items():
            merged[key] = bool(value)
        policy["enabled"] = True
        policy["features"] = merged
        save_policy(policy)
        return status_report()

    if normalized in {"off", "disable"}:
        raw_features = list(features.keys()) if isinstance(features, dict) else features
        updates = _normalize_feature_updates(raw_features, default_value=False)
        if not updates:
            policy["enabled"] = False
            policy["features"] = merged
            save_policy(policy)
            return status_report()
        for key, value in updates.items():
            merged[key] = bool(value)
        policy["enabled"] = True
        policy["features"] = merged
        save_policy(policy)
        return status_report()

    if normalized == "set_features":
        updates = _normalize_feature_updates(features, default_value=True)
        if not updates:
            raise ValueError("features_required")
        for key, value in updates.items():
            merged[key] = bool(value)
        policy["features"] = merged
        save_policy(policy)
        return status_report()

    raise ValueError(f"unsupported_command:{normalized}")


def _extract_payload_text(data: dict[str, Any]) -> str:
    """從 OpenClaw JSON 結構提取回應文字。

    嘗試多種格式：
    1. payloads[].text（標準格式）
    2. result.text / text（直接文字）
    3. summary（摘要欄位）
    4. messages[-1].content（對話格式）
    """
    if not isinstance(data, dict):
        return ""
    # 1. payloads[].text — 標準
    payloads: list = data.get("payloads") or (data.get("result") or {}).get("payloads") or []
    parts: list[str] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text") or "").strip()
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts).strip()

    # 2. result.text 或頂層 text
    result_obj = data.get("result") or {}
    for obj in (result_obj, data):
        if isinstance(obj, dict):
            t = str(obj.get("text") or "").strip()
            if t and len(t) > 20:
                return t

    # 3. summary 欄位（有時 Codex 把結果放在 summary）
    summary = str(data.get("summary") or "").strip()
    if summary and len(summary) > 50:
        return summary

    # 4. messages 對話格式 — 取最後一個 assistant message
    messages = data.get("messages") or (result_obj.get("messages") if isinstance(result_obj, dict) else None) or []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text_parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text_parts.append(str(c.get("text") or ""))
            joined = "\n\n".join(text_parts).strip()
            if joined:
                return joined

    return ""


def _cleanup_session_locks():
    """清除 openclaw session lock 檔案，防止孤兒進程殘留 lock。"""
    try:
        for lf in glob.glob(os.path.expanduser("~/.openclaw/agents/*/sessions/*.lock")):
            try:
                os.unlink(lf)
            except OSError:
                pass
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 554, exc_info=True)


def _kill_orphan_agents():
    """殺掉所有殘留的 openclaw-agent 進程，防止 session lock 卡住後續呼叫。"""
    try:
        import subprocess as _sp
        result = _sp.run(["pgrep", "-f", "openclaw-agent"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().splitlines():
            pid = int(line.strip())
            try:
                os.kill(pid, signal.SIGKILL)
                logging.getLogger(__name__).info("Killed orphan openclaw-agent pid=%d", pid)
            except OSError:
                pass
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_kill_orphan_agents", exc_info=True)


def run_prompt(
    *,
    feature: str,
    prompt: str,
    timeout_sec: Optional[int] = None,
    thinking: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    policy = load_policy()
    feature_name = _normalize_feature_name(feature)
    if feature_name not in DEFAULT_FEATURES:
        return {"success": False, "error": f"unsupported_feature:{feature_name}", "feature": feature_name}
    if not bool(policy.get("enabled")):
        return {"success": False, "error": "codex_policy_disabled", "feature": feature_name}
    if not bool((policy.get("features") or {}).get(feature_name, False)):
        return {"success": False, "error": f"codex_feature_disabled:{feature_name}", "feature": feature_name}
    runtime = load_runtime_state()
    runtime_ready, cooldown_remaining_sec = _runtime_ready(runtime)
    if not runtime_ready:
        return {
            "success": False,
            "error": f"codex_cooldown_active:{cooldown_remaining_sec}s",
            "feature": feature_name,
            "cooldown_remaining_sec": cooldown_remaining_sec,
            "cooldown_reason": str(runtime.get("cooldown_reason") or ""),
        }

    agent_id = str(policy.get("agent_id") or DEFAULT_AGENT_ID).strip() or DEFAULT_AGENT_ID
    if not _codex_agent_info(agent_id):
        return {"success": False, "error": f"codex_agent_missing:{agent_id}", "feature": feature_name}
    oauth_ready, oauth_profile = _codex_oauth_ready()
    if not oauth_ready:
        return {"success": False, "error": "codex_oauth_unavailable", "feature": feature_name}

    feature_profile = _feature_profile(feature_name)
    use_thinking = str(thinking or feature_profile.get("thinking") or policy.get("thinking") or DEFAULT_THINKING).strip().lower() or DEFAULT_THINKING
    if use_thinking not in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        use_thinking = DEFAULT_THINKING
    try:
        requested_timeout = int(timeout_sec or feature_profile.get("timeout_sec") or policy.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
    except Exception:
        requested_timeout = DEFAULT_TIMEOUT_SEC
    requested_timeout = max(30, min(requested_timeout, 1800))

    command = [
        _openclaw_bin(),
        "agent",
        "--agent",
        agent_id,
        "--message",
        str(prompt or "").strip(),
        "--thinking",
        use_thinking,
        "--timeout",
        str(requested_timeout),
        "--json",
    ]
    if session_id:
        command.extend(["--session-id", str(session_id)])

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_exec_env(),
            start_new_session=True,  # 獨立 process group，方便整組清掉
        )
        real_timeout = max(60, requested_timeout + 30)
        try:
            stdout_data, stderr_data = proc.communicate(timeout=real_timeout)
        except subprocess.TimeoutExpired:
            # 超時：清掉整個 process group（含 openclaw-agent 子進程）
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 686, exc_info=True)
            # SIGTERM 不夠 — 用 SIGKILL 確保整個 process group 死透
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            # 清掉所有殘留 openclaw-agent 進程
            _kill_orphan_agents()
            # 清 session lock
            _cleanup_session_locks()
            _record_failure(feature_name, f"codex_timeout:{requested_timeout}s")
            return {
                "success": False,
                "error": f"codex_timeout:{requested_timeout}s",
                "feature": feature_name,
                "agent_id": agent_id,
                "oauth_profile": oauth_profile,
                "stdout": "",
                "stderr": "",
            }
        # 正常結束後也清 session lock（以防萬一）
        _cleanup_session_locks()
    except Exception as exc:
        _cleanup_session_locks()
        _record_failure(feature_name, f"codex_exec_failed:{type(exc).__name__}:{exc}")
        return {
            "success": False,
            "error": f"codex_exec_failed:{type(exc).__name__}:{exc}",
            "feature": feature_name,
            "agent_id": agent_id,
            "oauth_profile": oauth_profile,
        }

    stdout = str(stdout_data or "").strip()
    stderr = str(stderr_data or "").strip()
    parsed: dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout)
        except Exception:
            parsed = {}

    text = _extract_payload_text(parsed)
    # fallback: JSON 解析失敗但 stdout 有足夠長度的純文字 → 直接採用
    if not text and not parsed and stdout and len(stdout) > 50 and proc.returncode == 0:
        # 不是 JSON 格式，直接當純文字使用
        text = stdout.strip()
    # debug: 記錄 Codex 回傳結構以便診斷格式問題
    if not text and parsed:
        _keys = list(parsed.keys())[:15]
        _result_obj = parsed.get("result") or {}
        _result_keys = list(_result_obj.keys())[:15] if isinstance(_result_obj, dict) else []
        _result_sample = str(_result_obj)[:800] if _result_obj else ""
        logging.getLogger(__name__).warning(
            "Codex no text. keys=%s, result_keys=%s, result=%s, stdout_tail=%s",
            _keys, _result_keys, _result_sample, stdout[-800:] if stdout else ""
        )
    # meta 可能在頂層或 result 底下
    _meta_root = (parsed.get("meta") or (parsed.get("result") or {}).get("meta") or {}) if isinstance(parsed, dict) else {}
    agent_meta = (_meta_root.get("agentMeta") or {}) if isinstance(_meta_root, dict) else {}
    prompt_stats = _system_prompt_stats(parsed)
    usage = agent_meta.get("usage") if isinstance(agent_meta.get("usage"), dict) else {}
    ok = proc.returncode == 0 and bool(text)
    error_text = "" if ok else (str(parsed.get("summary") or "") or stderr[-400:] or stdout[-400:] or f"returncode:{proc.returncode}")
    if ok:
        _record_success(
            feature_name,
            duration_ms=int((time.monotonic() - started) * 1000),
            provider=str(agent_meta.get("provider") or "openai-codex"),
            model=str(agent_meta.get("model") or "gpt-5.4"),
            usage_total=int(usage.get("total") or 0),
            system_prompt_chars=int(prompt_stats.get("system_prompt_chars") or 0),
        )
    else:
        _record_failure(feature_name, error_text)
    result = {
        "success": ok,
        "text": text,
        "error": error_text,
        "feature": feature_name,
        "agent_id": agent_id,
        "oauth_profile": oauth_profile,
        "thinking": use_thinking,
        "timeout_sec": requested_timeout,
        "returncode": proc.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "provider": str(agent_meta.get("provider") or "openai-codex"),
        "model": str(agent_meta.get("model") or "gpt-5.4"),
        "session_id": str(agent_meta.get("sessionId") or ""),
        "usage": usage,
        "system_prompt_chars": int(prompt_stats.get("system_prompt_chars") or 0),
        "project_context_chars": int(prompt_stats.get("project_context_chars") or 0),
        "non_project_context_chars": int(prompt_stats.get("non_project_context_chars") or 0),
        "skill_prompt_chars": int(prompt_stats.get("skill_prompt_chars") or 0),
        "stdout_tail": stdout[-1200:],
        "stderr_tail": stderr[-1200:],
    }
    if INCLUDE_RAW:
        result["raw"] = parsed
    return result


def translate_with_codex(text: str, *, source_lang: str = "auto", target_lang: str = "繁體中文", timeout_sec: Optional[int] = None) -> dict[str, Any]:
    prompt = (
        "你是 MAGI 的翻譯引擎。請把以下內容翻譯成指定語言。\n"
        f"- 來源語言：{source_lang}\n"
        f"- 目標語言：{target_lang}\n"
        "- 保留原意、段落、條列、日期、數字、法條與專有名詞。\n"
        "- 不要摘要，不要解釋，不要加前言，只輸出翻譯結果。\n\n"
        f"{str(text or '').strip()}"
    )
    return run_prompt(feature="translate", prompt=prompt, timeout_sec=timeout_sec)


def summarize_with_codex(text: str, *, summary_length: str = "medium", timeout_sec: Optional[int] = None) -> dict[str, Any]:
    hint_map = {
        "short": "3-5 點",
        "medium": "5-8 點",
        "long": "10-15 點",
    }
    hint = hint_map.get(str(summary_length or "medium").strip().lower(), hint_map["medium"])
    prompt = (
        "你是 MAGI 的重點整理引擎。請用繁體中文整理以下內容。\n"
        f"- 請輸出 {hint} 條列重點\n"
        "- 保留事實、關鍵數字、日期、人物、法條、結論與風險\n"
        "- 不要客套，不要加前言，只輸出摘要內容\n\n"
        f"{str(text or '').strip()}"
    )
    return run_prompt(feature="summary", prompt=prompt, timeout_sec=timeout_sec)


def classify_intent_with_codex(text: str, *, timeout_sec: Optional[int] = None) -> dict[str, Any]:
    prompt = (
        "請把下面使用者訊息分類成且只能分類成以下其中一種：CHAT、QUERY、CMD、DANGER。\n"
        "- CHAT: 閒聊、寒暄、非執行性對話\n"
        "- QUERY: 詢問資訊、查詢、需要回答知識問題\n"
        "- CMD: 要求系統執行動作、改設定、跑工具\n"
        "- DANGER: 明顯破壞性或高風險指令\n"
        "只輸出一個大寫分類詞，不要解釋。\n\n"
        f"{str(text or '').strip()}"
    )
    result = run_prompt(feature="intent", prompt=prompt, timeout_sec=timeout_sec)
    label = str(result.get("text") or "").strip().upper()
    for token in ("DANGER", "CMD", "QUERY", "CHAT"):
        if token in label:
            result["label"] = token
            result["success"] = True
            result["text"] = token
            return result
    result["success"] = False
    result["error"] = result.get("error") or "invalid_intent_label"
    return result


def analyze_image_with_codex(image_path: str, *, user_prompt: str, task_type: str = "vision", timeout_sec: Optional[int] = None) -> dict[str, Any]:
    normalized_task = str(task_type or "vision").strip().lower() or "vision"
    if normalized_task in OCR_TASK_TYPES:
        ocr = _local_ocr_extract(image_path)
        if ocr.get("success"):
            prompt = (
                "你是 MAGI 的 OCR 校對與抽取引擎。以下文字是以本機 tesseract 從圖片擷取出的原始 OCR 結果。\n"
                f"- 圖片路徑：{str(image_path or '').strip()}\n"
                f"- 任務類型：{normalized_task}\n"
                f"- 使用者需求：{str(user_prompt or '').strip()}\n"
                f"- OCR 語言設定：{str(ocr.get('lang') or '').strip()}\n"
                "- 請根據原始 OCR 內容做最小必要校對，避免憑空補寫未出現的資訊。\n"
                "- 如果使用者要求逐字輸出，請只輸出校對後文字；不要解釋。\n\n"
                "[RAW OCR]\n"
                f"{str(ocr.get('text') or '').strip()}"
            )
            return run_prompt(feature="vision", prompt=prompt, timeout_sec=timeout_sec)

    prompt = (
        "你在 MAGI 工作區內執行。請分析這個本機圖片檔，但不得修改原檔：\n"
        f"- 圖片路徑：{str(image_path or '').strip()}\n"
        f"- 任務類型：{normalized_task}\n"
        f"- 使用者需求：{str(user_prompt or '').strip()}\n"
        "- 僅輸出最終答案，使用繁體中文。\n"
    )
    return run_prompt(feature="vision", prompt=prompt, timeout_sec=timeout_sec)


def refine_ocr_with_codex(ocr_text: str, *, user_prompt: str, timeout_sec: Optional[int] = None) -> dict[str, Any]:
    prompt = (
        "你是 MAGI 的 OCR 校對與抽取引擎。以下內容是由本地 OCR 模型擷取出的原始文字。\n"
        f"- 使用者需求：{str(user_prompt or '').strip()}\n"
        "- 請在不憑空補寫的前提下，做最小必要校對。\n"
        "- 如果需求是逐字輸出，就只輸出校對後的文字；不要解釋。\n"
        "- 如果需求是抽取日期、編號或欄位，請只輸出提取結果。\n\n"
        "[RAW OCR]\n"
        f"{str(ocr_text or '').strip()}"
    )
    return run_prompt(feature="vision", prompt=prompt, timeout_sec=timeout_sec)


def polish_transcript_with_codex(text: str, *, timeout_sec: Optional[int] = None) -> dict[str, Any]:
    prompt = (
        "你是 MAGI 的逐字稿整理引擎。請整理以下逐字稿：\n"
        "- 只修正標點、斷句、段落與可合理推定的說話者標記\n"
        "- 不得捏造內容，不得省略事實，不得總結\n"
        "- 若無法確定說話者，保留原文內容即可\n"
        "- 只輸出整理後逐字稿，不要解釋\n\n"
        f"{str(text or '').strip()}"
    )
    return run_prompt(feature="transcript", prompt=prompt, timeout_sec=timeout_sec)
