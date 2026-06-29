#!/usr/bin/env python3
"""Check and self-heal Tailscale Funnel from a public-DNS perspective.

Local MagicDNS can resolve a Funnel host to the node's 100.x Tailnet address.
That proves tailnet access, but not public Funnel reachability.  This check
queries public DNS, probes each public ingress IP with curl --resolve, and
rebuilds Funnel when every public probe fails.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / ".runtime" / "tailscale_funnel_health_latest.json"
MOBILE_ENTRY_PATH = "/mobile-app"
MOBILE_LOGIN_PATH = "/login?next=/mobile&mobile_app=1"
MOBILE_ENTRY_EXPECTED = "302 redirect to /login?next=/mobile&mobile_app=1"


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _run(args: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "args": args,
        }
    except Exception as exc:
        return {"ok": False, "returncode": 124, "stdout": "", "stderr": str(exc), "args": args}


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _parse_curl_http_code(stdout: str) -> int:
    code_text = (stdout or "").strip()[-3:]
    try:
        return int(code_text)
    except Exception:
        return 0


def _extract_location_header(stdout: str) -> str:
    for line in (stdout or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "location":
            return value.strip()
    return ""


def _mobile_redirect_ok(location: str) -> bool:
    if not location:
        return False
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    return parsed.path == "/login" and query.get("next", [None])[0] == "/mobile" and query.get("mobile_app", [None])[0] == "1"


def _tailscale_bin() -> str:
    for candidate in (
        os.environ.get("MAGI_TAILSCALE_BIN", ""),
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/opt/homebrew/bin/tailscale",
        "tailscale",
    ):
        if candidate and (candidate == "tailscale" or Path(candidate).exists()):
            return candidate
    return "tailscale"


def _load_funnel_status() -> dict[str, Any]:
    ts = _tailscale_bin()
    if ts == "tailscale" and shutil.which("tailscale") is None:
        return {"ok": True, "data": {}, "skipped_reason": "tailscale CLI unavailable"}
    res = _run([ts, "funnel", "status", "--json"], timeout=8)
    if not res["ok"]:
        return {"ok": False, "error": res["stderr"] or res["stdout"] or "tailscale funnel status failed"}
    try:
        return {"ok": True, "data": json.loads(res["stdout"] or "{}")}
    except Exception as exc:
        return {"ok": False, "error": f"invalid funnel status json: {exc}", "raw": res["stdout"]}


def _extract_targets(status: dict[str, Any]) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    web = status.get("Web") if isinstance(status, dict) else {}
    if not isinstance(web, dict):
        return targets
    for host_port, cfg in web.items():
        host = str(host_port).rsplit(":", 1)[0]
        handlers = cfg.get("Handlers") if isinstance(cfg, dict) else {}
        if not isinstance(handlers, dict):
            continue
        for path, handler in handlers.items():
            proxy = str((handler or {}).get("Proxy") or "")
            if not proxy:
                continue
            targets.append({"host": host, "path": str(path or "/"), "proxy": proxy})
    return targets


def _public_ips(host: str) -> list[str]:
    def is_public_ip(value: str) -> bool:
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            return False
        return addr.is_global

    ips: list[str] = []
    if shutil.which("dig"):
        for resolver in ("1.1.1.1", "8.8.8.8"):
            for record_type in ("A", "AAAA"):
                res = _run(["dig", f"@{resolver}", "+short", host, record_type], timeout=6)
                if not res["ok"]:
                    continue
                for line in res["stdout"].splitlines():
                    line = line.strip()
                    if is_public_ip(line):
                        ips.append(line)
    elif shutil.which("nslookup"):
        for resolver in ("1.1.1.1", "8.8.8.8"):
            res = _run(["nslookup", host, resolver], timeout=6)
            if not res["ok"]:
                continue
            for line in res["stdout"].splitlines():
                match = re.search(r"Address:\s*([0-9a-fA-F:.]+)", line)
                if match and is_public_ip(match.group(1)):
                    ips.append(match.group(1))
    return sorted(set(ips))


def _probe(host: str, ip: str, path: str) -> dict[str, Any]:
    url_path = path if path.startswith("/") else f"/{path}"
    url = f"https://{host}{url_path if url_path != '/' else '/'}"
    resolve_ip = f"[{ip}]" if ":" in ip else ip
    res = _run(
        [
            "curl",
            "-sS",
            "-L",
            "--max-time",
            "20",
            "--resolve",
            f"{host}:443:{resolve_ip}",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            url,
        ],
        timeout=25,
    )
    http_code = _parse_curl_http_code(res["stdout"])
    return {
        "host": host,
        "ip": ip,
        "path": path,
        "ok": bool(res["ok"] and 200 <= http_code < 500),
        "http_code": http_code,
        "stderr": res["stderr"][-240:],
    }


def _probe_public_url(url: str) -> dict[str, Any]:
    res = _run(
        [
            "curl",
            "-k",
            "-sS",
            "-L",
            "--max-time",
            "12",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            url,
        ],
        timeout=15,
    )
    http_code = _parse_curl_http_code(res["stdout"])
    return {
        "url": url,
        "ok": bool(res["ok"] and 200 <= http_code < 500),
        "http_code": http_code,
        "stderr": res["stderr"][-240:],
    }


def _curl_resolve_value(host: str, ip: str) -> str:
    resolve_ip = f"[{ip}]" if ":" in ip else ip
    return f"{host}:443:{resolve_ip}"


def _mobile_probe_args(url: str, *, host: str = "", ip: str = "") -> list[str]:
    args = [
        "curl",
        "-k",
        "-sS",
        "--max-time",
        "12",
        "-o",
        "/dev/null",
        "-D",
        "-",
        "-w",
        "\n%{http_code}",
    ]
    if host and ip:
        args.extend(["--resolve", _curl_resolve_value(host, ip)])
    args.append(url)
    return args


def _probe_mobile_login(url: str, *, host: str = "", ip: str = "") -> dict[str, Any]:
    res = _run(_mobile_probe_args(url, host=host, ip=ip), timeout=15)
    http_code = _parse_curl_http_code(res["stdout"])
    return {
        "kind": "mobile_login",
        "url": url,
        "host": host,
        "ip": ip,
        "ok": bool(res["ok"] and 200 <= http_code < 400),
        "http_code": http_code,
        "stderr": res["stderr"][-240:],
        "expected": "HTTP 2xx/3xx login response for /login?next=/mobile&mobile_app=1",
    }


def _probe_mobile_entry_url(url: str, *, host: str = "", ip: str = "") -> dict[str, Any]:
    res = _run(_mobile_probe_args(url, host=host, ip=ip), timeout=15)
    http_code = _parse_curl_http_code(res["stdout"])
    location = _extract_location_header(res["stdout"])
    ok = bool(res["ok"] and http_code in {301, 302, 303, 307, 308} and _mobile_redirect_ok(location))
    result: dict[str, Any] = {
        "kind": "mobile_entry",
        "url": url,
        "host": host,
        "ip": ip,
        "ok": ok,
        "http_code": http_code,
        "location": location,
        "expected": MOBILE_ENTRY_EXPECTED,
        "stderr": res["stderr"][-240:],
    }
    if not ok:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            login_url = f"{parsed.scheme}://{parsed.netloc}{MOBILE_LOGIN_PATH}"
            result["login_probe"] = _probe_mobile_login(login_url, host=host, ip=ip)
    return result


def _public_base_url() -> str:
    raw = (
        os.environ.get("MAGI_PUBLIC_BASE_URL")
        or os.environ.get("MAGI_MOBILE_BASE_URL")
        or os.environ.get("MAGI_TAILSCALE_URL")
        or os.environ.get("MAGI_TAILSCALE_FUNNEL_HEALTH_URL")
        or ""
    ).strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc and parsed.path.rstrip("/") in {"/health", "/mobile-app", "/mobile", "/login"}:
        return parsed._replace(path="", params="", query="", fragment="").geturl().rstrip("/")
    return raw


def _public_health_url() -> str:
    explicit = (os.environ.get("MAGI_TAILSCALE_FUNNEL_HEALTH_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit if explicit.endswith("/health") else f"{explicit}/health"
    base = _public_base_url()
    if not base:
        return ""
    return base if base.endswith("/health") else f"{base}/health"


def _public_mobile_entry_url() -> str:
    explicit = (os.environ.get("MAGI_TAILSCALE_FUNNEL_MOBILE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    base = _public_base_url()
    if not base:
        return ""
    return base if base.endswith(MOBILE_ENTRY_PATH) else f"{base}{MOBILE_ENTRY_PATH}"


def _probe_configured_mobile_entry() -> dict[str, Any]:
    url = _public_mobile_entry_url()
    if not url:
        return {"ok": None, "expected": MOBILE_ENTRY_EXPECTED, "probes": [], "skipped_reason": "no public mobile URL configured"}
    probe = _probe_mobile_entry_url(url)
    return {"ok": bool(probe.get("ok")), "expected": MOBILE_ENTRY_EXPECTED, "probes": [probe]}


def _probe_mobile_entry_targets(targets: list[dict[str, str]], ips_by_host: dict[str, list[str]]) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()
    for target in targets:
        host = target["host"]
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        ips = ips_by_host.get(host) or _public_ips(host)
        ips_by_host[host] = ips
        if not ips:
            probes.append(
                {
                    "kind": "mobile_entry",
                    "host": host,
                    "path": MOBILE_ENTRY_PATH,
                    "ok": False,
                    "error": "no public DNS A/AAAA record",
                    "expected": MOBILE_ENTRY_EXPECTED,
                }
            )
            continue
        for ip in ips:
            probes.append(_probe_mobile_entry_url(f"https://{host}{MOBILE_ENTRY_PATH}", host=host, ip=ip))
    return {"ok": any(p.get("ok") for p in probes), "expected": MOBILE_ENTRY_EXPECTED, "probes": probes}


def _add_repair_guidance(payload: dict[str, Any], targets: list[dict[str, str]]) -> None:
    status = str(payload.get("status") or "")
    mobile_entry = payload.get("mobile_entry_after_repair") or payload.get("mobile_entry")
    mobile_entry = mobile_entry if isinstance(mobile_entry, dict) else {}
    needs_guidance = status in {"error", "failed", "failed_after_repair"} or mobile_entry.get("ok") is False
    if not needs_guidance:
        return

    actions: list[str] = list(payload.get("next_actions") or [])
    if targets:
        _append_unique(actions, "Run `scripts/ops/tailscale_funnel_healthcheck.py --apply` to reset and restore configured Funnel targets.")
    else:
        _append_unique(actions, "Configure Tailscale Funnel for the MAGI web port or set MAGI_PUBLIC_BASE_URL to the public Funnel base URL.")

    all_probe_sets = []
    for key in ("probes", "reprobes"):
        value = payload.get(key)
        if isinstance(value, list):
            all_probe_sets.extend(value)
    if isinstance(mobile_entry, dict):
        all_probe_sets.extend(mobile_entry.get("probes") or [])
    if any(isinstance(p, dict) and "no public DNS" in str(p.get("error", "")) for p in all_probe_sets):
        _append_unique(actions, "Confirm public Funnel DNS with `dig @1.1.1.1 <host> A` and re-enable Funnel if no public record exists.")

    if mobile_entry.get("ok") is False:
        _append_unique(
            actions,
            "Verify `/mobile-app` is forwarded to MAGI web and returns `302 /login?next=/mobile&mobile_app=1`.",
        )
        login_failed = False
        for probe in mobile_entry.get("probes") or []:
            login_probe = probe.get("login_probe") if isinstance(probe, dict) else None
            if isinstance(login_probe, dict) and not login_probe.get("ok"):
                login_failed = True
        if login_failed:
            _append_unique(actions, "Check the MAGI `/login` route locally before repairing public Funnel.")

    _append_unique(actions, "Rerun `scripts/ops/tailscale_funnel_healthcheck.py --print-json` after repair.")
    payload["next_actions"] = actions
    payload["restart_hint"] = (
        "If public probes return 000/5xx while local MAGI works, restart Tailscale/Funnel first; restart MAGI web only when "
        "`/login` or `/mobile-app` fails locally too."
    )


def _reset_and_restore(targets: list[dict[str, str]]) -> list[dict[str, Any]]:
    ts = _tailscale_bin()
    actions: list[dict[str, Any]] = []
    actions.append({"action": "reset", "result": _run([ts, "funnel", "reset"], timeout=15)})
    for target in targets:
        cmd = [ts, "funnel", "--bg", "--yes"]
        if target["path"] and target["path"] != "/":
            cmd.extend(["--set-path", target["path"]])
        cmd.append(target["proxy"])
        actions.append({"action": "enable", "target": target, "result": _run(cmd, timeout=20)})
    return actions


def check(apply: bool = False) -> dict[str, Any]:
    _load_dotenv()
    status = _load_funnel_status()
    payload: dict[str, Any] = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "skipped",
        "reason": "",
        "targets": [],
        "probes": [],
        "mobile_entry": {},
        "actions": [],
        "next_actions": [],
        "restart_hint": "",
    }
    if not status["ok"]:
        payload["mobile_entry"] = _probe_configured_mobile_entry()
        health_url = _public_health_url()
        if health_url:
            probe = _probe_public_url(health_url)
            payload["probes"] = [probe]
            if probe.get("ok"):
                if payload["mobile_entry"].get("ok") is False:
                    payload.update({
                        "status": "failed",
                        "reason": "public health probe succeeded, but mobile entry/login probe failed",
                    })
                    _add_repair_guidance(payload, [])
                    return payload
                payload.update({
                    "status": "ok",
                    "reason": f"tailscale CLI status unavailable, but public health probe succeeded: {status.get('error', 'status failed')}",
                })
                return payload
        if payload["mobile_entry"].get("ok") is True:
            payload.update({"status": "ok", "reason": "tailscale CLI status unavailable, but mobile entry probe succeeded"})
            return payload
        payload.update({"status": "error", "reason": status.get("error", "status failed")})
        _add_repair_guidance(payload, [])
        return payload

    targets = _extract_targets(status["data"])
    payload["targets"] = targets
    if not targets:
        payload["mobile_entry"] = _probe_configured_mobile_entry()
        health_url = _public_health_url()
        if health_url:
            probe = _probe_public_url(health_url)
            payload["probes"] = [probe]
            if probe.get("ok"):
                if payload["mobile_entry"].get("ok") is False:
                    payload.update({"status": "failed", "reason": "public health probe succeeded, but mobile entry/login probe failed"})
                    _add_repair_guidance(payload, [])
                    return payload
                payload.update({"status": "ok", "reason": "no Funnel target in CLI output, but public health probe succeeded"})
                return payload
        if payload["mobile_entry"].get("ok") is True:
            payload.update({"status": "ok", "reason": "no Funnel target in CLI output, but mobile entry probe succeeded"})
            return payload
        if payload["mobile_entry"].get("ok") is False:
            payload.update({"status": "failed", "reason": "no Funnel target in CLI output and mobile entry probe failed"})
            _add_repair_guidance(payload, [])
            return payload
        payload.update({"status": "skipped", "reason": status.get("skipped_reason") or "no Funnel target configured"})
        return payload

    probes: list[dict[str, Any]] = []
    ips_by_host: dict[str, list[str]] = {}
    for target in targets:
        ips = _public_ips(target["host"])
        ips_by_host[target["host"]] = ips
        if not ips:
            probes.append({"host": target["host"], "path": target["path"], "ok": False, "error": "no public DNS A/AAAA record"})
            continue
        for ip in ips:
            probes.append(_probe(target["host"], ip, target["path"]))
    payload["probes"] = probes
    payload["mobile_entry"] = _probe_mobile_entry_targets(targets, ips_by_host)

    public_ok = any(p.get("ok") for p in probes)
    mobile_ok = payload["mobile_entry"].get("ok")
    if public_ok and mobile_ok is not False:
        reason = "public Funnel and mobile entry probes succeeded" if mobile_ok is True else "public Funnel probe succeeded"
        payload.update({"status": "ok", "reason": reason})
        return payload
    if public_ok and mobile_ok is False:
        payload.update({"status": "failed", "reason": "public Funnel probe succeeded, but mobile entry/login probe failed"})
        _add_repair_guidance(payload, targets)
        return payload
    if mobile_ok is True:
        payload.update({"status": "ok", "reason": "mobile entry public probe succeeded"})
        return payload

    payload.update({"status": "failed", "reason": "all public Funnel probes failed"})
    if apply:
        payload["actions"] = _reset_and_restore(targets)
        time.sleep(1.5)
        reprobes: list[dict[str, Any]] = []
        re_ips_by_host: dict[str, list[str]] = {}
        for target in targets:
            ips = _public_ips(target["host"])
            re_ips_by_host[target["host"]] = ips
            for ip in ips:
                reprobes.append(_probe(target["host"], ip, target["path"]))
        payload["reprobes"] = reprobes
        payload["mobile_entry_after_repair"] = _probe_mobile_entry_targets(targets, re_ips_by_host)
        repaired_public = any(p.get("ok") for p in reprobes)
        repaired_mobile = payload["mobile_entry_after_repair"].get("ok") is not False
        payload["status"] = "recovered" if repaired_public and repaired_mobile else "failed_after_repair"
        payload["reason"] = (
            "repaired and public/mobile probes succeeded"
            if payload["status"] == "recovered"
            else "repair did not restore public Funnel or mobile entry"
        )
    _add_repair_guidance(payload, targets)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="reset and restore Funnel when public probes fail")
    parser.add_argument("--print-json", action="store_true", help="print JSON only; useful for manual/live checks")
    parser.add_argument("--json-out", default=str(STATE_PATH))
    args = parser.parse_args(argv)

    payload = check(apply=args.apply)
    should_write = args.json_out != "-" and not (args.print_json and args.json_out == str(STATE_PATH))
    if should_write:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] in {"ok", "skipped", "recovered"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
