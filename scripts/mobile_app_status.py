#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE_CONFIG = ROOT / "mobile_app" / "capacitor.config.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(args: list[str]) -> dict:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=4)
        return {"ok": result.returncode == 0, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except Exception as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc)}


def _load_mobile_config() -> dict:
    try:
        return json.loads(MOBILE_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}


def _probe_url(url: str) -> dict:
    if not url:
        return {"ok": False, "error": "empty url"}
    result = _run([
        "curl",
        "-sS",
        "-I",
        "--max-time",
        "8",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        url,
    ])
    try:
        http_code = int((result["stdout"] or "0")[-3:])
    except Exception:
        http_code = 0
    return {
        "ok": bool(result["ok"] and 200 <= http_code < 500),
        "http_code": http_code,
        "stderr": result["stderr"][-240:],
    }


def main() -> int:
    serve = _run(["tailscale", "serve", "status", "--json"])
    status = _run(["tailscale", "status", "--json"])
    payload = {"tailscale_status": status["ok"], "tailscale_serve": serve["ok"]}
    if serve["stdout"]:
        try:
            payload["serve"] = json.loads(serve["stdout"])
        except Exception:
            payload["serve_raw"] = serve["stdout"]
    if status["stdout"]:
        try:
            data = json.loads(status["stdout"])
            self_node = data.get("Self") or {}
            payload["dns_name"] = str(self_node.get("DNSName") or "").rstrip(".")
            ips = self_node.get("TailscaleIPs") or []
            payload["ip"] = ips[0] if ips else ""
            payload["online"] = bool(self_node.get("Online"))
        except Exception:
            payload["status_raw"] = status["stdout"]
    mobile_config = _load_mobile_config()
    server = mobile_config.get("server") if isinstance(mobile_config, dict) else {}
    active_url = str((server or {}).get("url") or "")
    payload["mobile_app_config"] = {
        "url": active_url,
        "cleartext": bool((server or {}).get("cleartext")),
    }
    if payload.get("ip"):
        payload["tailnet_fallback_url"] = f"http://{payload['ip']}:5002/mobile-app"
        payload["tailnet_fallback_probe"] = _probe_url(payload["tailnet_fallback_url"])
    payload["active_mobile_url_probe"] = _probe_url(active_url)

    try:
        from scripts.ops.tailscale_funnel_healthcheck import check as _funnel_check

        funnel = _funnel_check(apply=False)
        payload["public_funnel_status"] = funnel.get("status")
        payload["public_funnel_reason"] = funnel.get("reason")
        payload["public_funnel_probes"] = funnel.get("probes", [])
    except Exception as exc:
        payload["public_funnel_status"] = "error"
        payload["public_funnel_reason"] = str(exc)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
