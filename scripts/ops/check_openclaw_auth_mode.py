#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse


SAFE_EXIT = 0
WARNING_EXIT = 10
RISK_EXIT = 20
UNKNOWN_EXIT = 30

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
SAFE_LOCAL_KEYS = {"", "local", "omlx-local", "dummy", "none", "null"}
LOCAL_PRIMARY_PROVIDERS = {"omlx", "ollama"}
RISK_ENV_VARS = [
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "XAI_API_KEY",
]


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def is_local_base_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (parsed.hostname or "").lower() in LOCAL_HOSTS


def summarize_profile(name: str, profile_cfg: dict, stored_profile: dict) -> dict:
    return {
        "name": name,
        "provider": profile_cfg.get("provider") or stored_profile.get("provider"),
        "mode": profile_cfg.get("mode"),
        "authType": stored_profile.get("authType") or stored_profile.get("type"),
        "hasApiKey": bool(stored_profile.get("apiKey")),
        "hasAccessToken": bool(stored_profile.get("accessToken")),
        "hasRefreshToken": bool(stored_profile.get("refreshToken")),
    }


def build_report() -> dict:
    openclaw_dir = Path.home() / ".openclaw"
    config_path = Path(
        os.environ.get(
            "MAGI_OPENCLAW_CONFIG_PATH",
            str(openclaw_dir / "openclaw.json"),
        )
    )
    auth_profiles_path = Path(
        os.environ.get(
            "MAGI_OPENCLAW_AUTH_PROFILES_PATH",
            str(openclaw_dir / "agents" / "main" / "agent" / "auth-profiles.json"),
        )
    )

    config = load_json(config_path)
    stored_auth = load_json(auth_profiles_path)
    auth_profile_cfg = ((config.get("auth") or {}).get("profiles") or {})
    stored_profiles = stored_auth.get("profiles") or {}

    primary_model = (
        (((config.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary")
        or ""
    ).strip()
    primary_provider = primary_model.split("/", 1)[0] if "/" in primary_model else primary_model
    fallback_models = (
        (((config.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("fallbacks")
        or []
    )

    matching_profiles = []
    for name, profile_cfg in auth_profile_cfg.items():
        if not isinstance(profile_cfg, dict):
            continue
        if (profile_cfg.get("provider") or "").strip() == primary_provider:
            matching_profiles.append(
                summarize_profile(name, profile_cfg, stored_profiles.get(name) or {})
            )

    risky_provider_keys = []
    local_provider_keys = []
    providers = ((config.get("models") or {}).get("providers") or {})
    for provider_name, provider_cfg in providers.items():
        if not isinstance(provider_cfg, dict):
            continue
        api_key = str(provider_cfg.get("apiKey") or "").strip()
        base_url = str(provider_cfg.get("baseUrl") or "").strip()
        if not api_key:
            continue
        row = {
            "provider": provider_name,
            "baseUrl": base_url or None,
            "localBaseUrl": is_local_base_url(base_url),
            "maskedKey": "***" if api_key else None,
        }
        if row["localBaseUrl"] and api_key.lower() in SAFE_LOCAL_KEYS:
            local_provider_keys.append(row)
        elif row["localBaseUrl"] and provider_name in {"omlx", "ollama"}:
            local_provider_keys.append(row)
        else:
            risky_provider_keys.append(row)

    risky_profile_keys = []
    for name, stored_profile in stored_profiles.items():
        if not isinstance(stored_profile, dict):
            continue
        api_key = str(stored_profile.get("apiKey") or "").strip()
        if not api_key:
            continue
        risky_profile_keys.append(
            {
                "name": name,
                "provider": stored_profile.get("provider"),
                "maskedKey": "***",
            }
        )

    risky_env_vars = [name for name in RISK_ENV_VARS if os.environ.get(name)]
    oauth_match = any(
        (p.get("mode") == "oauth") or (p.get("authType") == "oauth")
        for p in matching_profiles
    )
    api_match = any(
        (p.get("mode") == "apiKey")
        or (p.get("authType") == "apiKey")
        or p.get("hasApiKey")
        for p in matching_profiles
    )

    primary_provider_cfg = providers.get(primary_provider) or {}
    primary_provider_is_local = (
        isinstance(primary_provider_cfg, dict)
        and is_local_base_url(str(primary_provider_cfg.get("baseUrl") or "").strip())
        and primary_provider in LOCAL_PRIMARY_PROVIDERS
    )

    status = "UNCONFIRMED"
    reasons = []
    if primary_provider == "openai-codex" and oauth_match and not api_match:
        status = "SAFE_OAUTH_ONLY"
        reasons.append("主要模型是 openai-codex，且主用 auth profile 標示為 OAuth。")
    elif primary_provider_is_local:
        status = "SAFE_LOCAL_ONLY"
        reasons.append("主要模型是本地 provider，且 baseUrl 指向本機。")
    elif api_match:
        status = "API_KEY_ACTIVE"
        reasons.append("主用 provider 的 auth profile 顯示為 API key 模式或帶有 API key。")

    if risky_provider_keys or risky_profile_keys or risky_env_vars:
        if status in {"SAFE_OAUTH_ONLY", "SAFE_LOCAL_ONLY"}:
            status = "MIXED_RISK"
        elif status == "UNCONFIRMED":
            status = "API_KEY_ACTIVE"
        reasons.append("偵測到外部 provider 的 API key 痕跡，存在計費風險。")

    if not matching_profiles and not primary_provider_is_local:
        reasons.append("找不到與主要 provider 對應的 auth profile。")
    if not reasons:
        reasons.append("設定可讀，但還需要人工確認目前帳務政策。")

    exit_code = {
        "SAFE_OAUTH_ONLY": SAFE_EXIT,
        "SAFE_LOCAL_ONLY": SAFE_EXIT,
        "MIXED_RISK": WARNING_EXIT,
        "API_KEY_ACTIVE": RISK_EXIT,
        "UNCONFIRMED": UNKNOWN_EXIT,
    }[status]

    return {
        "status": status,
        "exit_code": exit_code,
        "summary": {
            "primary_model": primary_model,
            "primary_provider": primary_provider,
            "fallback_models": fallback_models,
            "config_path": str(config_path),
            "auth_profiles_path": str(auth_profiles_path),
        },
        "matching_profiles": matching_profiles,
        "risky_provider_keys": risky_provider_keys,
        "risky_profile_keys": risky_profile_keys,
        "local_provider_keys": local_provider_keys,
        "risky_env_vars": risky_env_vars,
        "reasons": reasons,
    }


def print_human(report: dict) -> None:
    print("OpenClaw Auth Mode Check")
    print(f"status: {report['status']}")
    print(f"primary model: {report['summary']['primary_model'] or '(missing)'}")
    print(f"primary provider: {report['summary']['primary_provider'] or '(missing)'}")

    if report["matching_profiles"]:
        print("matching auth profiles:")
        for profile in report["matching_profiles"]:
            bits = [
                profile["name"],
                f"mode={profile.get('mode') or '-'}",
                f"authType={profile.get('authType') or '-'}",
                f"hasAccessToken={'yes' if profile.get('hasAccessToken') else 'no'}",
                f"hasApiKey={'yes' if profile.get('hasApiKey') else 'no'}",
            ]
            print(f"  - {' | '.join(bits)}")
    else:
        print("matching auth profiles: none")

    if report["local_provider_keys"]:
        print("local provider keys:")
        for item in report["local_provider_keys"]:
            print(
                f"  - {item['provider']} @ {item['baseUrl'] or '(no baseUrl)'}"
                " (local-only, not treated as external billing risk)"
            )

    if report["risky_provider_keys"]:
        print("risky provider api keys:")
        for item in report["risky_provider_keys"]:
            print(f"  - {item['provider']} @ {item['baseUrl'] or '(no baseUrl)'}")

    if report["risky_profile_keys"]:
        print("risky auth profile api keys:")
        for item in report["risky_profile_keys"]:
            print(f"  - {item['name']} ({item.get('provider') or 'unknown provider'})")

    if report["risky_env_vars"]:
        print("risky environment variables:")
        for name in report["risky_env_vars"]:
            print(f"  - {name}=set")

    print("reasons:")
    for reason in report["reasons"]:
        print(f"  - {reason}")

    print(f"exit code: {report['exit_code']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether OpenClaw is currently using OAuth or an API key path."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
