from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from api.runtime_paths import get_config_path, get_magi_root_dir


PRODUCT_RUNTIME_PATH = Path(
    os.environ.get(
        "MAGI_PRODUCT_RUNTIME_PATH",
        str(get_magi_root_dir() / ".agent" / "product_runtime.json"),
    )
)

VALID_CODEX_MODES = {"auto", "local", "codex"}
VALID_LAF_PORTAL_ENVS = {"production", "test", "compare"}

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "file_review": {
        "codex_mode": "auto",
    },
    "transcript": {
        "codex_mode": "auto",
    },
    "laf": {
        "codex_mode": "auto",
        "portal_env": "production",
        "prod_base_url": "https://lawyer.laf.org.tw",
        "test_base_url": "http://127.0.0.1:17002",
        "compare_base_url": "",
    },
}


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
    data = payload if isinstance(payload, dict) else {}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _load_config() -> dict[str, Any]:
    cfg_path = get_config_path("config.json")
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _normalize_codex_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in VALID_CODEX_MODES:
        return mode
    return "auto"


def _normalize_portal_env(value: Any) -> str:
    portal_env = str(value or "").strip().lower()
    if portal_env in VALID_LAF_PORTAL_ENVS:
        return portal_env
    return "production"


def load_product_runtime() -> dict[str, Any]:
    return _load_json(PRODUCT_RUNTIME_PATH)


def save_product_runtime(data: dict[str, Any]) -> dict[str, Any]:
    return _save_json(PRODUCT_RUNTIME_PATH, data)


def update_product_runtime(product: str, **updates: Any) -> dict[str, Any]:
    name = str(product or "").strip().lower()
    if name not in DEFAULT_PROFILES:
        raise ValueError(f"unsupported_product:{name}")
    current = load_product_runtime()
    product_data = current.get(name) if isinstance(current.get(name), dict) else {}
    merged = dict(product_data or {})
    for key, value in updates.items():
        if value is None:
            continue
        merged[key] = value
    current[name] = merged
    save_product_runtime(current)
    return get_product_profile(name)


def _config_profile(product: str, config: dict[str, Any]) -> dict[str, Any]:
    root = config.get("product_runtime") if isinstance(config.get("product_runtime"), dict) else {}
    profile = root.get(product) if isinstance(root.get(product), dict) else {}
    out = dict(profile or {})

    if product == "laf":
        laf_cfg = config.get("laf") if isinstance(config.get("laf"), dict) else {}
        if str(laf_cfg.get("base_url") or "").strip():
            out.setdefault("prod_base_url", str(laf_cfg.get("base_url")).strip())
        for key in ("test_base_url", "sandbox_base_url", "mock_base_url"):
            if str(laf_cfg.get(key) or "").strip():
                out.setdefault("test_base_url", str(laf_cfg.get(key)).strip())
                break
        if str(laf_cfg.get("compare_base_url") or "").strip():
            out.setdefault("compare_base_url", str(laf_cfg.get("compare_base_url")).strip())
        if str(laf_cfg.get("portal_env") or "").strip():
            out.setdefault("portal_env", str(laf_cfg.get("portal_env")).strip())

    return out


def get_product_profile(product: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(product or "").strip().lower()
    if name not in DEFAULT_PROFILES:
        raise ValueError(f"unsupported_product:{name}")

    cfg = config if isinstance(config, dict) else _load_config()
    merged = dict(DEFAULT_PROFILES[name])
    merged.update(_config_profile(name, cfg))

    runtime = load_product_runtime()
    runtime_profile = runtime.get(name) if isinstance(runtime.get(name), dict) else {}
    merged.update(runtime_profile or {})

    env_prefix = f"MAGI_{name.upper()}_"
    codex_mode = os.environ.get(f"{env_prefix}CODEX_MODE")
    if codex_mode:
        merged["codex_mode"] = codex_mode

    if name == "laf":
        for env_name, field in (
            ("MAGI_LAF_PORTAL_ENV", "portal_env"),
            ("LAF_BASE_URL_PROD", "prod_base_url"),
            ("LAF_BASE_URL_TEST", "test_base_url"),
            ("MAGI_LAF_COMPARE_BASE_URL", "compare_base_url"),
        ):
            raw = os.environ.get(env_name)
            if raw:
                merged[field] = raw

    merged["codex_mode"] = _normalize_codex_mode(merged.get("codex_mode"))
    if name == "laf":
        merged["portal_env"] = _normalize_portal_env(merged.get("portal_env"))
        merged["prod_base_url"] = str(merged.get("prod_base_url") or DEFAULT_PROFILES["laf"]["prod_base_url"]).strip()
        merged["test_base_url"] = str(merged.get("test_base_url") or DEFAULT_PROFILES["laf"]["test_base_url"]).strip()
        merged["compare_base_url"] = str(merged.get("compare_base_url") or "").strip()

    return merged


def resolve_laf_portal_targets(
    config: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config if isinstance(config, dict) else _load_config()
    active = dict(profile or get_product_profile("laf", config=cfg))

    portal_env = _normalize_portal_env(active.get("portal_env"))
    prod_base_url = str(active.get("prod_base_url") or DEFAULT_PROFILES["laf"]["prod_base_url"]).strip()
    test_base_url = str(active.get("test_base_url") or DEFAULT_PROFILES["laf"]["test_base_url"]).strip()
    compare_base_url = str(active.get("compare_base_url") or "").strip()

    execute_env = "production"
    execute_base_url = prod_base_url
    if portal_env in {"test", "compare"}:
        execute_env = "test"
        execute_base_url = test_base_url

    if portal_env == "compare" and not compare_base_url:
        compare_base_url = prod_base_url

    execute_host = execute_base_url.lower()
    execute_mock_mode = "127.0.0.1" in execute_host or "localhost" in execute_host

    return {
        "portal_env": portal_env,
        "execute_env": execute_env,
        "execute_base_url": execute_base_url,
        "execute_mock_mode": execute_mock_mode,
        "prod_base_url": prod_base_url,
        "test_base_url": test_base_url,
        "compare_base_url": compare_base_url,
        "compare_enabled": bool(compare_base_url and compare_base_url != execute_base_url),
    }


def apply_product_runtime_env(
    product: str,
    *,
    env: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = str(product or "").strip().lower()
    profile = get_product_profile(name, config=config)
    target_env = env if env is not None else os.environ

    target_env["MAGI_CODEX_CONTEXT"] = name
    target_env["MAGI_CODEX_CONTEXT_MODE"] = str(profile.get("codex_mode") or "auto")

    out = {"product": name, "profile": profile}
    if name == "laf":
        portal = resolve_laf_portal_targets(config=config, profile=profile)
        target_env["MAGI_LAF_PORTAL_ENV"] = str(portal.get("portal_env") or "production")
        target_env["LAF_BASE_URL"] = str(portal.get("execute_base_url") or "")
        target_env["LAF_MOCK_MODE"] = "1" if portal.get("execute_mock_mode") else "0"
        if portal.get("compare_base_url"):
            target_env["MAGI_LAF_COMPARE_BASE_URL"] = str(portal.get("compare_base_url"))
        out["portal"] = portal
    return out


def product_profile_report(product: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = get_product_profile(product, config=config)
    report = {"product": str(product or "").strip().lower(), "profile": profile}
    if report["product"] == "laf":
        report["portal"] = resolve_laf_portal_targets(config=config, profile=profile)
    return report
