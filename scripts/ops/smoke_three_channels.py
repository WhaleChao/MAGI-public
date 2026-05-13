#!/usr/bin/env python3
"""
Three-channel smoke test for MAGI chat channels: LINE / Discord / Telegram.

Checks include:
- Local service health (api/server and optional tools_api)
- Channel credential presence
- Official provider API probe without sending user-facing messages
- Basic routing readiness (channel id/admin list/webhook config)

Usage:
  python3 scripts/ops/smoke_three_channels.py
  python3 scripts/ops/smoke_three_channels.py --json-out /tmp/smoke.json
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import requests
except Exception as e:  # pragma: no cover - direct runtime guard
    print(f"FATAL: requests import failed: {e}", file=sys.stderr)
    sys.exit(3)


PROJECT_ROOT = Path(_MAGI_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.runtime_paths import get_config_path

CONFIG_PATH = get_config_path("config.json")

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass
OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
AGENT_DIR = PROJECT_ROOT / ".agent"


@dataclass
class Check:
    channel: str
    name: str
    status: str  # PASS / WARN / FAIL / SKIP
    detail: str
    latency_ms: int | None = None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def _mask(value: str, keep_tail: int = 6) -> str:
    s = str(value or "").strip()
    if not s:
        return "(empty)"
    if len(s) <= keep_tail:
        return "*" * len(s)
    return "*" * (len(s) - keep_tail) + s[-keep_tail:]


def _is_privateish_webhook_endpoint(url: str) -> bool:
    host = str(urlparse(str(url or "")).hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host.endswith(".local") or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local)
    except Exception:
        return False


def _http_json(
    method: str,
    url: str,
    timeout_sec: int,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[bool, int, dict[str, Any] | None, str, int]:
    start = time.time()
    try:
        resp = requests.request(
            method=method.upper(),
            url=url,
            headers=headers or {},
            json=json_body,
            timeout=timeout_sec,
        )
        elapsed = int((time.time() - start) * 1000)
        try:
            body_json = resp.json()
        except Exception:
            body_json = None
        body_text = (resp.text or "").strip()
        return True, int(resp.status_code), body_json, body_text, elapsed
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        return False, 0, None, str(e), elapsed


def _parse_webhook_id_token(webhook_url: str) -> tuple[str, str]:
    s = str(webhook_url or "").strip()
    if not s:
        return "", ""
    parts = s.split("/")
    # https://discord.com/api/webhooks/{id}/{token}
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", ""


def _line_channel_access_token(code_cfg: dict[str, Any], openclaw_cfg: dict[str, Any]) -> str:
    env_keys = [
        "MAGI_LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_CHANNEL_ACCESS_TOKEN",
    ]
    for k in env_keys:
        v = str(os.environ.get(k, "")).strip()
        if v:
            return v

    v = str(code_cfg.get("line_channel_access_token") or "").strip()
    if v:
        return v

    line_cfg = (openclaw_cfg.get("channels") or {}).get("line") or {}
    candidates = [
        line_cfg.get("channelAccessToken"),
        line_cfg.get("accessToken"),
        line_cfg.get("token"),
    ]
    for c in candidates:
        v = str(c or "").strip()
        if v:
            return v
    return ""


def _line_channel_secret(code_cfg: dict[str, Any], openclaw_cfg: dict[str, Any]) -> str:
    env_keys = [
        "MAGI_LINE_CHANNEL_SECRET",
        "LINE_CHANNEL_SECRET",
    ]
    for k in env_keys:
        v = str(os.environ.get(k, "")).strip()
        if v:
            return v

    v = str(code_cfg.get("line_channel_secret") or "").strip()
    if v:
        return v

    line_cfg = (openclaw_cfg.get("channels") or {}).get("line") or {}
    candidates = [
        line_cfg.get("channelSecret"),
        line_cfg.get("webhookSecret"),
        line_cfg.get("secret"),
    ]
    for c in candidates:
        v = str(c or "").strip()
        if v:
            return v
    return ""


def _discord_bot_token(code_cfg: dict[str, Any], openclaw_cfg: dict[str, Any]) -> str:
    env = str(os.environ.get("DISCORD_BOT_TOKEN", "")).strip()
    if env:
        return env
    v = str(code_cfg.get("discord_bot_token") or "").strip()
    if v:
        return v
    v = str(((openclaw_cfg.get("channels") or {}).get("discord") or {}).get("token") or "").strip()
    return v


def _discord_webhook(code_cfg: dict[str, Any], openclaw_cfg: dict[str, Any]) -> str:
    for k in ("MAGI_DISCORD_WEBHOOK", "MAGI_DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        v = str(os.environ.get(k, "")).strip()
        if v:
            return v

    for k in ("discord_webhook_url", "discord_filescan_webhook_url", "discord_checklist_webhook_url"):
        v = str(code_cfg.get(k) or "").strip()
        if v:
            return v

    v = str(((openclaw_cfg.get("channels") or {}).get("discord") or {}).get("webhook_url") or "").strip()
    return v


def _discord_channel_id() -> str:
    v = str(os.environ.get("DISCORD_CHANNEL_ID", "")).strip()
    if v:
        return v
    last_channel_obj = _load_json(AGENT_DIR / "discord_last_channel.json")
    return str(last_channel_obj.get("channel_id") or "").strip()


def _telegram_token(openclaw_cfg: dict[str, Any]) -> str:
    for k in ("MAGI_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "OPENCLAW_TELEGRAM_BOT_TOKEN"):
        v = str(os.environ.get(k, "")).strip()
        if v:
            return v
    return str(((openclaw_cfg.get("channels") or {}).get("telegram") or {}).get("botToken") or "").strip()


def _telegram_admin_ids(openclaw_cfg: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    csv_env = str(os.environ.get("MAGI_ADMIN_TELEGRAM_IDS", "")).strip()
    if csv_env:
        ids.extend([x.strip() for x in csv_env.split(",") if x.strip()])

    allow_from = ((openclaw_cfg.get("channels") or {}).get("telegram") or {}).get("allowFrom") or []
    if isinstance(allow_from, list):
        ids.extend([str(x).strip() for x in allow_from if str(x).strip()])
    elif isinstance(allow_from, str):
        ids.extend([x.strip() for x in allow_from.split(",") if x.strip()])

    uniq = []
    seen = set()
    for x in ids:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _check_local_process(pattern: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and (result.stdout or "").strip():
            pids = [x.strip() for x in result.stdout.splitlines() if x.strip()]
            return True, ",".join(pids[:5])
        return False, ""
    except Exception:
        return False, ""


def _line_checks(
    checks: list[Check],
    code_cfg: dict[str, Any],
    openclaw_cfg: dict[str, Any],
    server_url: str,
    timeout_sec: int,
) -> None:
    ch = "LINE"
    ok, status, _, body, ms = _http_json("GET", f"{server_url.rstrip('/')}/health", timeout_sec=timeout_sec)
    if ok and status == 200:
        checks.append(Check(ch, "Local webhook server", "PASS", f"GET /health => 200 ({body[:80]})", ms))
    else:
        checks.append(Check(ch, "Local webhook server", "FAIL", f"GET /health failed (status={status}, err={body[:140]})", ms))

    token = _line_channel_access_token(code_cfg, openclaw_cfg)
    secret = _line_channel_secret(code_cfg, openclaw_cfg)
    if token and secret:
        checks.append(Check(ch, "Credentials", "PASS", f"token={_mask(token)} secret={_mask(secret)}"))
    else:
        checks.append(Check(ch, "Credentials", "FAIL", "LINE token/secret missing"))
        return

    headers = {"Authorization": f"Bearer {token}"}
    ok, status, j, body, ms = _http_json("GET", "https://api.line.me/v2/bot/info", headers=headers, timeout_sec=timeout_sec)
    if ok and status == 200 and isinstance(j, dict):
        display_name = str(j.get("displayName") or "").strip() or "(unknown)"
        user_id = str(j.get("userId") or "").strip()
        checks.append(Check(ch, "Official API auth", "PASS", f"displayName={display_name} userId={_mask(user_id)}", ms))
    else:
        checks.append(Check(ch, "Official API auth", "FAIL", f"/v2/bot/info status={status} err={body[:140]}", ms))

    ok, status, j, body, ms = _http_json(
        "GET",
        "https://api.line.me/v2/bot/channel/webhook/endpoint",
        headers=headers,
        timeout_sec=timeout_sec,
    )
    if not (ok and status == 200 and isinstance(j, dict)):
        checks.append(Check(ch, "Webhook endpoint query", "FAIL", f"status={status} err={body[:140]}", ms))
        return

    endpoint = str(j.get("endpoint") or "").strip()
    active = bool(j.get("active"))
    if endpoint:
        checks.append(Check(ch, "Webhook endpoint query", "PASS", f"active={active} endpoint={endpoint}", ms))
    else:
        checks.append(Check(ch, "Webhook endpoint query", "WARN", f"active={active}, endpoint not set", ms))
        return

    callback_obj = _load_json(AGENT_DIR / "line_last_callback.json")
    ts = int(callback_obj.get("updated_at") or 0)
    callback_age = max(0, int(time.time()) - ts) if ts > 0 else None
    callback_fresh = callback_age is not None and callback_age <= 3600

    ok, status, j, body, ms = _http_json(
        "POST",
        "https://api.line.me/v2/bot/channel/webhook/test",
        headers=headers,
        json_body={"endpoint": endpoint},
        timeout_sec=timeout_sec,
    )
    webhook_test_passed = False
    if ok and status == 200 and isinstance(j, dict):
        success = bool(j.get("success"))
        reason = str(j.get("reason") or "").strip()
        if success:
            webhook_test_passed = True
            checks.append(Check(ch, "Webhook test", "PASS", "LINE webhook test success=true", ms))
        elif callback_fresh and reason == "COULD_NOT_CONNECT":
            webhook_test_passed = True
            checks.append(
                Check(
                    ch,
                    "Webhook test",
                    "PASS",
                    f"LINE official test false-negative; real callback age={callback_age}s path={callback_obj.get('path') or ''}",
                    ms,
                )
            )
        elif "invalid webhook endpoint url" in reason.lower() and _is_privateish_webhook_endpoint(endpoint):
            checks.append(
                Check(
                    ch,
                    "Webhook test",
                    "WARN",
                    f"private endpoint not externally verifiable by LINE test API ({endpoint})",
                    ms,
                )
            )
        else:
            checks.append(Check(ch, "Webhook test", "FAIL", f"success=false reason={reason or '(none)'}", ms))
    else:
        body_l = str(body or "").lower()
        if "invalid webhook endpoint url" in body_l and _is_privateish_webhook_endpoint(endpoint):
            checks.append(
                Check(
                    ch,
                    "Webhook test",
                    "WARN",
                    f"private endpoint not externally verifiable by LINE test API ({endpoint})",
                    ms,
                )
            )
        elif callback_fresh and "could_not_connect" in body_l:
            checks.append(
                Check(
                    ch,
                    "Webhook test",
                    "PASS",
                    f"LINE official test false-negative; real callback age={callback_age}s path={callback_obj.get('path') or ''}",
                    ms,
                )
            )
        else:
            checks.append(Check(ch, "Webhook test", "FAIL", f"status={status} err={body[:180]}", ms))

    if ts > 0:
        age = callback_age if callback_age is not None else max(0, int(time.time()) - ts)
        if age <= 3600:
            checks.append(Check(ch, "Recent callback", "PASS", f"last callback age={age}s path={callback_obj.get('path') or ''}"))
        elif webhook_test_passed:
            checks.append(Check(ch, "Recent callback", "PASS", f"last callback age={age}s (stale; live webhook self-test passed)"))
        else:
            checks.append(Check(ch, "Recent callback", "WARN", f"last callback age={age}s (stale)"))
    else:
        status_name = "PASS" if webhook_test_passed else "WARN"
        detail = "no callback timestamp recorded; live webhook self-test passed" if webhook_test_passed else "no callback timestamp recorded"
        checks.append(Check(ch, "Recent callback", status_name, detail))


def _discord_checks(
    checks: list[Check],
    code_cfg: dict[str, Any],
    openclaw_cfg: dict[str, Any],
    timeout_sec: int,
) -> None:
    ch = "DISCORD"
    running, pid_text = _check_local_process("api/discord_bot.py")
    if running:
        checks.append(Check(ch, "Bot process", "PASS", f"running pid={pid_text}"))
    else:
        checks.append(Check(ch, "Bot process", "FAIL", "discord bot process not running"))

    token = _discord_bot_token(code_cfg, openclaw_cfg)
    if token:
        checks.append(Check(ch, "Bot token", "PASS", f"token={_mask(token)}"))
    else:
        checks.append(Check(ch, "Bot token", "FAIL", "DISCORD_BOT_TOKEN / config token missing"))
        return

    headers = {"Authorization": f"Bot {token}"}
    ok, status, j, body, ms = _http_json("GET", "https://discord.com/api/v10/users/@me", headers=headers, timeout_sec=timeout_sec)
    expected_bot_id = str(os.environ.get("MAGI_DISCORD_EXPECTED_BOT_ID") or "").strip()
    if ok and status == 200 and isinstance(j, dict):
        username = str(j.get("username") or "").strip()
        disc = str(j.get("discriminator") or "").strip()
        bot_id = str(j.get("id") or "").strip()
        tag = f"{username}#{disc}" if disc and disc != "0" else username
        if expected_bot_id and bot_id and bot_id != expected_bot_id:
            checks.append(
                Check(
                    ch,
                    "Official API auth",
                    "FAIL",
                    f"bot={tag} id={_mask(bot_id)} expected_id={_mask(expected_bot_id)}",
                    ms,
                )
            )
        else:
            checks.append(Check(ch, "Official API auth", "PASS", f"bot={tag} id={_mask(bot_id)}", ms))
    else:
        checks.append(Check(ch, "Official API auth", "FAIL", f"/users/@me status={status} err={body[:140]}", ms))

    channel_id = _discord_channel_id()
    if channel_id:
        checks.append(Check(ch, "Notify target channel", "PASS", f"channel_id={channel_id}"))
    else:
        checks.append(Check(ch, "Notify target channel", "WARN", "DISCORD_CHANNEL_ID and .agent/discord_last_channel.json are both empty"))

    webhook = _discord_webhook(code_cfg, openclaw_cfg)
    if not webhook:
        checks.append(Check(ch, "Webhook route", "WARN", "discord webhook URL not configured"))
    else:
        webhook_id, _ = _parse_webhook_id_token(webhook)
        ok, status, j, body, ms = _http_json("GET", webhook, timeout_sec=timeout_sec)
        if ok and status == 200:
            wh_name = ""
            if isinstance(j, dict):
                wh_name = str(j.get("name") or "").strip()
            checks.append(Check(ch, "Webhook route", "PASS", f"webhook_id={_mask(webhook_id)} name={wh_name or '(unnamed)'}", ms))
        else:
            checks.append(Check(ch, "Webhook route", "FAIL", f"status={status} err={body[:140]}", ms))

    oc_discord = ((openclaw_cfg.get("channels") or {}).get("discord") or {})
    if oc_discord:
        oc_enabled = bool(oc_discord.get("enabled"))
        status_text = "PASS" if not oc_enabled else "WARN"
        detail = "disabled" if not oc_enabled else "enabled (may conflict with MAGI Discord bot)"
        checks.append(Check(ch, "OpenClaw Discord", status_text, detail))


def _telegram_checks(
    checks: list[Check],
    openclaw_cfg: dict[str, Any],
    timeout_sec: int,
) -> None:
    ch = "TELEGRAM"
    token = _telegram_token(openclaw_cfg)
    if not token:
        checks.append(Check(ch, "Bot token", "FAIL", "telegram bot token missing"))
        return
    checks.append(Check(ch, "Bot token", "PASS", f"token={_mask(token)}"))

    base = f"https://api.telegram.org/bot{token}"
    ok, status, j, body, ms = _http_json("GET", f"{base}/getMe", timeout_sec=timeout_sec)
    if ok and status == 200 and isinstance(j, dict) and bool(j.get("ok")):
        res = j.get("result") or {}
        uname = str((res or {}).get("username") or "").strip()
        bid = str((res or {}).get("id") or "").strip()
        checks.append(Check(ch, "Official API auth", "PASS", f"bot=@{uname} id={_mask(bid)}", ms))
    else:
        checks.append(Check(ch, "Official API auth", "FAIL", f"getMe status={status} err={body[:160]}", ms))
        return

    admin_ids = _telegram_admin_ids(openclaw_cfg)
    if admin_ids:
        checks.append(Check(ch, "Admin allowlist", "PASS", f"allowFrom count={len(admin_ids)}"))
    else:
        checks.append(Check(ch, "Admin allowlist", "WARN", "allowFrom is empty"))
        return

    # Probe first two admins only to avoid excessive API calls.
    probed = admin_ids[:2]
    ok_count = 0
    warn_msgs: list[str] = []
    total_ms = 0
    for chat_id in probed:
        ok, status, j, body, ms = _http_json("GET", f"{base}/getChat?chat_id={chat_id}", timeout_sec=timeout_sec)
        total_ms += ms
        if ok and status == 200 and isinstance(j, dict) and bool(j.get("ok")):
            ok_count += 1
        else:
            short = body[:80].replace("\n", " ")
            warn_msgs.append(f"{chat_id}:{status}:{short}")

    if ok_count == len(probed):
        checks.append(Check(ch, "Admin reachability", "PASS", f"{ok_count}/{len(probed)} getChat success", total_ms))
    elif ok_count >= 1:
        checks.append(Check(ch, "Admin reachability", "WARN", f"{ok_count}/{len(probed)} getChat success; {', '.join(warn_msgs)}", total_ms))
    else:
        checks.append(Check(ch, "Admin reachability", "WARN", f"0/{len(probed)} getChat success; {', '.join(warn_msgs)}", total_ms))


def _tools_api_checks(checks: list[Check], tools_url: str, timeout_sec: int) -> None:
    ch = "INFRA"
    ok, status, _, body, ms = _http_json("GET", f"{tools_url.rstrip('/')}/health", timeout_sec=timeout_sec)
    if ok and status == 200:
        checks.append(Check(ch, "tools_api health", "PASS", "tools_api online", ms))
    else:
        checks.append(Check(ch, "tools_api health", "WARN", f"tools_api unreachable status={status} err={body[:120]}", ms))


def _print_report(checks: list[Check]) -> tuple[int, int, int]:
    pass_n = sum(1 for c in checks if c.status == "PASS")
    warn_n = sum(1 for c in checks if c.status == "WARN")
    fail_n = sum(1 for c in checks if c.status == "FAIL")

    print("\n=== MAGI Three-Channel Smoke Report ===")
    for c in checks:
        icon = {
            "PASS": "✅",
            "WARN": "⚠️",
            "FAIL": "❌",
            "SKIP": "⏭️",
        }.get(c.status, "•")
        latency = f" ({c.latency_ms}ms)" if c.latency_ms is not None else ""
        print(f"{icon} [{c.channel}] {c.name}: {c.status}{latency} - {c.detail}")

    print("\n--- Summary ---")
    print(f"PASS: {pass_n}")
    print(f"WARN: {warn_n}")
    print(f"FAIL: {fail_n}")
    return pass_n, warn_n, fail_n


def main() -> int:
    # 2026-04-20: OpenClaw/LINE primary webhook path removed. LINE probing
    # here is legacy; LINE/Discord/Telegram routing now lives in
    # api/pipelines/message_pipeline.py and api/domains/*_flow.py.
    # This smoke remains available for manual credential-presence checks,
    # but may emit WARN for LINE sections that no longer have a runtime.
    print("⚠️  smoke_three_channels.py: LINE webhook probes are legacy (OpenClaw removed 2026-04-20).",
          file=sys.stderr)
    parser = argparse.ArgumentParser(description="MAGI 3-channel smoke checks (LINE/DC/TG)")
    parser.add_argument("--server-url", default="http://127.0.0.1:5002", help="MAGI server base URL")
    try:
        from api.routing.service_registry import get_service_url as _gsurl
        _tools_def = _gsurl("tools_api")
    except Exception:
        _tools_def = "http://127.0.0.1:5003"
    parser.add_argument("--tools-url", default=_tools_def, help="MAGI tools API base URL")
    parser.add_argument("--timeout-sec", type=int, default=8, help="HTTP timeout in seconds")
    parser.add_argument("--json-out", default="", help="Optional path to save JSON report")
    parser.add_argument("--strict-warn", action="store_true", help="Treat WARN as non-zero exit")
    args = parser.parse_args()

    code_cfg = _load_json(CONFIG_PATH)
    openclaw_cfg = _load_json(OPENCLAW_CONFIG_PATH)

    checks: list[Check] = []
    _tools_api_checks(checks, args.tools_url, args.timeout_sec)
    _line_checks(checks, code_cfg, openclaw_cfg, args.server_url, args.timeout_sec)
    _discord_checks(checks, code_cfg, openclaw_cfg, args.timeout_sec)
    _telegram_checks(checks, openclaw_cfg, args.timeout_sec)

    pass_n, warn_n, fail_n = _print_report(checks)

    if args.json_out:
        payload = {
            "generated_at": int(time.time()),
            "summary": {"pass": pass_n, "warn": warn_n, "fail": fail_n},
            "checks": [asdict(c) for c in checks],
        }
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON report: {out_path}")

    if fail_n > 0:
        return 2
    if args.strict_warn and warn_n > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
