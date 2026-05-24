from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from api.model_config import (
    DEFAULT_EMBED_MODEL,
    DEFAULT_TEXT_MODEL,
    TEXT_HEAVY_MODEL,
    TEXT_PRIMARY_MODEL,
    is_disallowed_model,
    resolve_text_model,
)


MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[1]))).resolve()
REGISTRY_PATH = Path(os.environ.get("MAGI_MODEL_REGISTRY", str(MAGI_ROOT / "config" / "model_registry.json")))

QUALITY_TASKS = {
    "summary",
    "translate",
    "transcribe",
    "legal_analysis",
    "repair_insight_summary",
    "reflection",
    "night_talk",
}
LIGHTWEIGHT_TASKS = {"general", "tc_review", "captcha", "date_extract", "ocr", "vision"}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str = "omlx"
    origin: str = ""
    tier: str = ""
    tasks: tuple[str, ...] = ()
    public_allowed: bool = True
    private_allowed: bool = True
    safe_context_tokens: int = 4096
    max_concurrency: int = 1
    gates: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelSpec":
        return cls(
            id=str(data.get("id") or "").strip(),
            provider=str(data.get("provider") or "omlx").strip(),
            origin=str(data.get("origin") or "").strip(),
            tier=str(data.get("tier") or "").strip(),
            tasks=tuple(str(x).strip() for x in data.get("tasks") or () if str(x).strip()),
            public_allowed=bool(data.get("public_allowed", True)),
            private_allowed=bool(data.get("private_allowed", True)),
            safe_context_tokens=int(data.get("safe_context_tokens") or 4096),
            max_concurrency=int(data.get("max_concurrency") or 1),
            gates=dict(data.get("gates") or {}),
        )


@dataclass(frozen=True)
class ResourceView:
    ok: bool = True
    level: str = "unknown"
    disk_free_gb: float = -1.0
    swap_used_gb: float = -1.0
    free_plus_inactive_gb: float = -1.0
    memory_free_percent: float = -1.0
    reasons: tuple[str, ...] = ()

    @classmethod
    def from_decision(cls, decision: Any) -> "ResourceView":
        if hasattr(decision, "snapshot"):
            snapshot = getattr(decision, "snapshot")
            return cls(
                ok=bool(getattr(decision, "ok", False)),
                level=str(getattr(decision, "level", "unknown") or "unknown"),
                disk_free_gb=float(getattr(snapshot, "disk_free_gb", -1.0)),
                swap_used_gb=float(getattr(snapshot, "swap_used_gb", -1.0)),
                free_plus_inactive_gb=float(getattr(snapshot, "free_plus_inactive_gb", -1.0)),
                memory_free_percent=float(getattr(snapshot, "memory_free_percent", -1.0)),
                reasons=tuple(str(x) for x in getattr(decision, "reasons", ()) or ()),
            )
        data = decision if isinstance(decision, dict) else {}
        snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        return cls(
            ok=bool(data.get("ok", False)),
            level=str(data.get("level", "unknown") or "unknown"),
            disk_free_gb=float(snapshot.get("disk_free_gb", -1.0)),
            swap_used_gb=float(snapshot.get("swap_used_gb", -1.0)),
            free_plus_inactive_gb=float(snapshot.get("free_plus_inactive_gb", -1.0)),
            memory_free_percent=float(snapshot.get("memory_free_percent", -1.0)),
            reasons=tuple(str(x) for x in data.get("reasons", ()) or ()),
        )


@dataclass(frozen=True)
class ModelRouteDecision:
    selected_model: str
    tier: str
    reason: str
    task_type: str
    provider: str = "omlx"
    active_models: tuple[str, ...] = ()
    preferred_model: str = ""
    blocked_reasons: tuple[str, ...] = ()
    resource_level: str = "unknown"
    safe_context_tokens: int = 4096
    should_queue: bool = False
    cloud_heavy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_REGISTRY_CACHE: tuple[float, dict[str, ModelSpec]] = (0.0, {})
_RUNTIME_CACHE: tuple[float, tuple[tuple[str, ...], ResourceView]] = (0.0, ((), ResourceView()))
_LOCK = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return bool(default)
    return value in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        return float(default)
    try:
        return float(value)
    except ValueError:
        return float(default)


def load_registry(path: Path | None = None) -> dict[str, ModelSpec]:
    global _REGISTRY_CACHE
    registry_path = path or REGISTRY_PATH
    cache_ttl = _env_float("MAGI_MODEL_REGISTRY_CACHE_SEC", 30.0)
    now = time.monotonic()
    with _LOCK:
        cached_at, cached = _REGISTRY_CACHE
        if cached and now - cached_at <= cache_ttl and registry_path == REGISTRY_PATH:
            return cached
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        specs = {
            spec.id: spec
            for spec in (ModelSpec.from_dict(item) for item in payload.get("models") or [])
            if spec.id and not is_disallowed_model(spec.id)
        }
    except Exception:
        specs = {}
    if DEFAULT_TEXT_MODEL not in specs:
        specs[DEFAULT_TEXT_MODEL] = ModelSpec(
            id=DEFAULT_TEXT_MODEL,
            provider="omlx",
            origin="google",
            tier="stable_local",
            tasks=tuple(QUALITY_TASKS | LIGHTWEIGHT_TASKS),
            safe_context_tokens=8192,
        )
    with _LOCK:
        if registry_path == REGISTRY_PATH:
            _REGISTRY_CACHE = (now, specs)
    return specs


def probe_active_models(base_url: str = "", timeout: float = 1.2) -> tuple[str, ...]:
    url_base = (base_url or os.environ.get("INFERENCE_LOCAL_OLLAMA_BASE") or os.environ.get("MAGI_OMLX_CHAT_URL") or "http://127.0.0.1:8080").rstrip("/")
    if url_base.endswith("/v1/chat/completions"):
        url_base = url_base.rsplit("/v1/chat/completions", 1)[0]
    try:
        with urllib.request.urlopen(f"{url_base}/v1/models", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return ()
    except Exception:
        return ()
    models = []
    for item in payload.get("data") if isinstance(payload, dict) else []:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            if model_id and not is_disallowed_model(model_id):
                models.append(model_id)
    return tuple(models)


def collect_resource_view() -> ResourceView:
    try:
        from scripts.ops import resource_governor

        return ResourceView.from_decision(resource_governor.classify(resource_governor.collect_snapshot()))
    except Exception:
        return ResourceView(ok=True, level="unknown")


def get_runtime_state(*, active_models: Iterable[str] | None = None, resource: ResourceView | dict[str, Any] | None = None) -> tuple[tuple[str, ...], ResourceView]:
    global _RUNTIME_CACHE
    if active_models is not None and resource is not None:
        rv = resource if isinstance(resource, ResourceView) else ResourceView.from_decision(resource)
        return tuple(str(x) for x in active_models if str(x).strip() and not is_disallowed_model(str(x))), rv

    ttl = _env_float("MAGI_MODEL_ROUTER_RUNTIME_CACHE_SEC", 10.0)
    now = time.monotonic()
    with _LOCK:
        cached_at, cached = _RUNTIME_CACHE
        if now - cached_at <= ttl:
            return cached

    models = tuple(str(x) for x in (active_models if active_models is not None else probe_active_models()) if str(x).strip())
    rv = resource if isinstance(resource, ResourceView) else collect_resource_view()
    if not isinstance(rv, ResourceView):
        rv = ResourceView.from_decision(rv)
    with _LOCK:
        _RUNTIME_CACHE = (now, (models, rv))
    return models, rv


def _active_has(active_models: Iterable[str], needle: str) -> bool:
    low = str(needle or "").lower()
    return any(low in str(model).lower() for model in active_models)


def _spec_for_task(task_type: str, tier: str, registry: dict[str, ModelSpec]) -> ModelSpec | None:
    task = str(task_type or "general").strip() or "general"
    candidates = []
    for spec in registry.values():
        if tier and spec.tier != tier:
            continue
        if task in spec.tasks or "*" in spec.tasks:
            candidates.append(spec)
    if candidates:
        return candidates[0]
    for spec in registry.values():
        if spec.tier == tier:
            return spec
    return None


def _evaluate_gates(spec: ModelSpec, resource: ResourceView, active_models: tuple[str, ...], prompt_len: int) -> tuple[bool, tuple[str, ...], bool]:
    gates = dict(spec.gates or {})
    blocked: list[str] = []
    should_queue = False

    if gates.get("require_model_live", False) and not _active_has(active_models, "26b"):
        blocked.append("26b_not_live")

    min_disk = _env_float("MAGI_ROUTER_26B_MIN_DISK_GB", float(gates.get("min_disk_free_gb", 70) or 70))
    if resource.disk_free_gb >= 0 and resource.disk_free_gb < min_disk:
        blocked.append(f"disk_free<{min_disk:g}GB")
        should_queue = True

    min_free = _env_float("MAGI_ROUTER_26B_MIN_FREE_GB", float(gates.get("min_free_plus_inactive_gb", 8) or 8))
    if resource.free_plus_inactive_gb >= 0 and resource.free_plus_inactive_gb < min_free:
        blocked.append(f"free_plus_inactive<{min_free:g}GB")
        should_queue = True

    max_swap = _env_float("MAGI_ROUTER_26B_MAX_SWAP_GB", float(gates.get("max_swap_used_gb", 20) or 20))
    if resource.swap_used_gb >= 0 and resource.swap_used_gb > max_swap:
        blocked.append(f"swap_used>{max_swap:g}GB")
        should_queue = True

    allowed_levels = tuple(str(x) for x in gates.get("allowed_resource_levels") or ("normal",))
    if resource.level not in allowed_levels and resource.level != "unknown":
        blocked.append(f"resource_level={resource.level}")
        should_queue = True

    max_prompt = int(os.environ.get("MAGI_ROUTER_26B_MAX_PROMPT_CHARS", "60000") or "60000")
    if prompt_len > max_prompt:
        blocked.append(f"prompt_len>{max_prompt}")
        should_queue = True

    return not blocked, tuple(blocked), should_queue


def choose_model_for_request(
    *,
    task_type: str = "general",
    prompt: str = "",
    requested_model: str = "",
    heavy_opt_in: bool = False,
    force_quality: bool = False,
    active_models: Iterable[str] | None = None,
    resource: ResourceView | dict[str, Any] | None = None,
    registry: dict[str, ModelSpec] | None = None,
) -> ModelRouteDecision:
    task = str(task_type or "general").strip() or "general"
    prompt_len = len(str(prompt or ""))
    reg = registry or load_registry()
    active, rv = get_runtime_state(active_models=active_models, resource=resource) if active_models is not None or resource is not None else get_runtime_state()

    if task == "embedding":
        return ModelRouteDecision(
            selected_model=DEFAULT_EMBED_MODEL,
            tier="embedding_local",
            reason="embedding tasks must keep the embedding model",
            task_type=task,
            provider="omlx",
            active_models=active,
            resource_level=rv.level,
            safe_context_tokens=8192,
        )

    if requested_model and not is_disallowed_model(requested_model):
        resolved = resolve_text_model(requested_model, available=active or None)
        spec = reg.get(resolved) or reg.get(requested_model)
        if resolved and (not active or resolved in active):
            return ModelRouteDecision(
                selected_model=resolved,
                tier=(spec.tier if spec else "explicit"),
                reason="explicit model request accepted",
                task_type=task,
                provider=(spec.provider if spec else "omlx"),
                active_models=active,
                preferred_model=resolved,
                resource_level=rv.level,
                safe_context_tokens=(spec.safe_context_tokens if spec else 4096),
            )

    if heavy_opt_in:
        return ModelRouteDecision(
            selected_model="nvidia-nim-heavy-non-china" if _env_bool("NVIDIA_NIM_ENABLE", False) else TEXT_PRIMARY_MODEL,
            tier="cloud_heavy" if _env_bool("NVIDIA_NIM_ENABLE", False) else "stable_local",
            reason="@heavy explicitly requested; use NIM when enabled, otherwise local fallback",
            task_type=task,
            provider="nvidia_nim" if _env_bool("NVIDIA_NIM_ENABLE", False) else "omlx",
            active_models=active,
            resource_level=rv.level,
            cloud_heavy=_env_bool("NVIDIA_NIM_ENABLE", False),
        )

    quality_needed = force_quality or task in QUALITY_TASKS or prompt_len >= int(os.environ.get("MAGI_ROUTER_QUALITY_PROMPT_CHARS", "6000") or "6000")
    stable_spec = _spec_for_task(task, "stable_local", reg) or reg.get(TEXT_PRIMARY_MODEL) or reg.get(DEFAULT_TEXT_MODEL)
    stable_model = stable_spec.id if stable_spec else TEXT_PRIMARY_MODEL
    if active:
        stable_model = resolve_text_model(stable_model, available=active)

    if not quality_needed or task in LIGHTWEIGHT_TASKS:
        return ModelRouteDecision(
            selected_model=stable_model,
            tier="stable_local",
            reason="lightweight or routine task; prefer stable local model",
            task_type=task,
            provider="omlx",
            active_models=active,
            resource_level=rv.level,
            safe_context_tokens=(stable_spec.safe_context_tokens if stable_spec else 8192),
        )

    heavy_spec = reg.get(TEXT_HEAVY_MODEL) or _spec_for_task(task, "heavy_local_moe", reg)
    if heavy_spec and not is_disallowed_model(heavy_spec.id):
        ok, blocked, should_queue = _evaluate_gates(heavy_spec, rv, active, prompt_len)
        if ok:
            return ModelRouteDecision(
                selected_model=heavy_spec.id,
                tier=heavy_spec.tier or "heavy_local_moe",
                reason="quality task and 26B-A4B gates passed",
                task_type=task,
                provider=heavy_spec.provider,
                active_models=active,
                preferred_model=heavy_spec.id,
                resource_level=rv.level,
                safe_context_tokens=heavy_spec.safe_context_tokens,
            )
        return ModelRouteDecision(
            selected_model=stable_model,
            tier="stable_local",
            reason="quality task but 26B-A4B was blocked by resource or live-model gates",
            task_type=task,
            provider="omlx",
            active_models=active,
            preferred_model=heavy_spec.id,
            blocked_reasons=blocked,
            resource_level=rv.level,
            safe_context_tokens=(stable_spec.safe_context_tokens if stable_spec else 8192),
            should_queue=should_queue,
        )

    return ModelRouteDecision(
        selected_model=stable_model,
        tier="stable_local",
        reason="no safe heavy local model registered; use stable local model",
        task_type=task,
        provider="omlx",
        active_models=active,
        resource_level=rv.level,
        safe_context_tokens=(stable_spec.safe_context_tokens if stable_spec else 8192),
    )


def decision_summary(decision: ModelRouteDecision) -> str:
    parts = [decision.selected_model, decision.tier, decision.reason]
    if decision.blocked_reasons:
        parts.append("blocked=" + ",".join(decision.blocked_reasons))
    if decision.resource_level:
        parts.append("resource=" + decision.resource_level)
    return " | ".join(str(x) for x in parts if x)
