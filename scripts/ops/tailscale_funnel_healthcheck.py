#!/usr/bin/env python3
"""Check and self-heal Tailscale Funnel from a public-DNS perspective.

Local MagicDNS can resolve a Funnel host to the node's 100.x Tailnet address.
That proves tailnet access, but not public Funnel reachability.  This check
queries public DNS, probes each public ingress IP with curl --resolve, and
rebuilds Funnel when every public probe fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / ".runtime" / "tailscale_funnel_health_latest.json"


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
    ips: list[str] = []
    if shutil.which("dig"):
        for resolver in ("1.1.1.1", "8.8.8.8"):
            res = _run(["dig", f"@{resolver}", "+short", host], timeout=6)
            if not res["ok"]:
                continue
            for line in res["stdout"].splitlines():
                line = line.strip()
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", line) and not line.startswith("100."):
                    ips.append(line)
    elif shutil.which("nslookup"):
        for resolver in ("1.1.1.1", "8.8.8.8"):
            res = _run(["nslookup", host, resolver], timeout=6)
            if not res["ok"]:
                continue
            for line in res["stdout"].splitlines():
                match = re.search(r"Address:\s*(\d+\.\d+\.\d+\.\d+)", line)
                if match and not match.group(1).startswith("100."):
                    ips.append(match.group(1))
    return sorted(set(ips))


def _probe(host: str, ip: str, path: str) -> dict[str, Any]:
    url_path = path if path.startswith("/") else f"/{path}"
    url = f"https://{host}{url_path if url_path != '/' else '/'}"
    res = _run(
        [
            "curl",
            "-sS",
            "-L",
            "--max-time",
            "20",
            "--resolve",
            f"{host}:443:{ip}",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            url,
        ],
        timeout=25,
    )
    code_text = (res["stdout"] or "").strip()[-3:]
    try:
        http_code = int(code_text)
    except Exception:
        http_code = 0
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
    code_text = (res["stdout"] or "").strip()[-3:]
    try:
        http_code = int(code_text)
    except Exception:
        http_code = 0
    return {
        "url": url,
        "ok": bool(res["ok"] and 200 <= http_code < 500),
        "http_code": http_code,
        "stderr": res["stderr"][-240:],
    }


def _public_health_url() -> str:
    base = (
        os.environ.get("MAGI_TAILSCALE_FUNNEL_HEALTH_URL")
        or os.environ.get("MAGI_PUBLIC_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/health") else f"{base}/health"


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
        "actions": [],
    }
    if not status["ok"]:
        health_url = _public_health_url()
        if health_url:
            probe = _probe_public_url(health_url)
            payload["probes"] = [probe]
            if probe.get("ok"):
                payload.update({
                    "status": "ok",
                    "reason": f"tailscale CLI status unavailable, but public health probe succeeded: {status.get('error', 'status failed')}",
                })
                return payload
        payload.update({"status": "error", "reason": status.get("error", "status failed")})
        return payload

    targets = _extract_targets(status["data"])
    payload["targets"] = targets
    if not targets:
        health_url = _public_health_url()
        if health_url:
            probe = _probe_public_url(health_url)
            payload["probes"] = [probe]
            if probe.get("ok"):
                payload.update({"status": "ok", "reason": "no Funnel target in CLI output, but public health probe succeeded"})
                return payload
        payload.update({"status": "skipped", "reason": status.get("skipped_reason") or "no Funnel target configured"})
        return payload

    probes: list[dict[str, Any]] = []
    for target in targets:
        ips = _public_ips(target["host"])
        if not ips:
            probes.append({"host": target["host"], "path": target["path"], "ok": False, "error": "no public DNS A record"})
            continue
        for ip in ips:
            probes.append(_probe(target["host"], ip, target["path"]))
    payload["probes"] = probes

    if any(p.get("ok") for p in probes):
        payload.update({"status": "ok", "reason": "public Funnel probe succeeded"})
        return payload

    payload.update({"status": "failed", "reason": "all public Funnel probes failed"})
    if apply:
        payload["actions"] = _reset_and_restore(targets)
        time.sleep(1.5)
        reprobes: list[dict[str, Any]] = []
        for target in targets:
            for ip in _public_ips(target["host"]):
                reprobes.append(_probe(target["host"], ip, target["path"]))
        payload["reprobes"] = reprobes
        payload["status"] = "recovered" if any(p.get("ok") for p in reprobes) else "failed_after_repair"
        payload["reason"] = "repaired and public probe succeeded" if payload["status"] == "recovered" else "repair did not restore public Funnel"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="reset and restore Funnel when public probes fail")
    parser.add_argument("--json-out", default=str(STATE_PATH))
    args = parser.parse_args(argv)

    payload = check(apply=args.apply)
    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] in {"ok", "skipped", "recovered"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
