"""
BRAIN MANAGER ACTION MODULE (CASPER SIDE)
=========================================
Manages the cognitive backend of CASPER.
Handles switching between Local and Distributed modes with safety checks.
"""

import argparse
import json
import logging
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Tuple

import requests

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from skills.bridge import melchior_client
from skills.bridge.melchior_client import generate_code as melchior_code

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("BrainManager")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LLAMA_SERVER_PATH = os.environ.get(
    "LLAMA_SERVER_PATH",
    "/Users/ai/.docker/bin/inference/llama-server",
)
RPC_START_SCRIPT = os.environ.get(
    "RPC_START_SCRIPT",
    f"{_MAGI_ROOT}/start_rpc.sh",
)

try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    MELCHIOR_IP = os.environ.get("MELCHIOR_HOST") or _get_node_ip("melchior") or "100.116.54.16"
except Exception:
    MELCHIOR_IP = os.environ.get("MELCHIOR_HOST", "100.116.54.16")
MELCHIOR_API_PORT = int(os.environ.get("MELCHIOR_API_PORT", "8080"))
MELCHIOR_AGENT_PORT = int(os.environ.get("MELCHIOR_AGENT_PORT") or os.environ.get("MELCHIOR_PORT", "5002"))
MELCHIOR_OLLAMA_PORT = int(os.environ.get("MELCHIOR_OLLAMA_PORT", "11434"))

MELCHIOR_API_ENDPOINT = f"http://{MELCHIOR_IP}:{MELCHIOR_API_PORT}/v1"
MELCHIOR_AGENT_ENDPOINT = f"http://{MELCHIOR_IP}:{MELCHIOR_AGENT_PORT}"
try:
    from api.routing.service_registry import get_service_url as _get_svc_url2
    LOCAL_API_ENDPOINT = _get_svc_url2("omlx_inference") + "/v1"
except Exception:
    LOCAL_API_ENDPOINT = "http://localhost:8080/v1"

STATE_FILE = os.environ.get("MAGI_BRAIN_STATE_FILE", f"{_MAGI_ROOT}/.brain_state.json")
NGL_HINT_FILE = os.environ.get("MAGI_BRAIN_NGL_HINT_FILE", f"{_MAGI_ROOT}/.brain_ngl_hint.json")
BRAIN_SWITCH_LOCK = threading.RLock()
BRAIN_AUTO_FALLBACK_LOCAL = os.environ.get("BRAIN_AUTO_FALLBACK_LOCAL", "1") != "0"


def _remote_agent_reachable(timeout_sec: float | None = None) -> bool:
    """Fast preflight for Melchior agent reachability to avoid long switch retries."""
    try:
        timeout_val = float(
            timeout_sec
            if timeout_sec is not None
            else (os.environ.get("MELCHIOR_AGENT_REACHABILITY_TIMEOUT_SEC", "1.2") or "1.2")
        )
    except Exception:
        timeout_val = 1.2
    try:
        with socket.create_connection((MELCHIOR_IP, int(MELCHIOR_AGENT_PORT)), timeout=max(0.2, timeout_val)):
            return True
    except Exception:
        return False


def _normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m in {"engineer", "independent", "fallback"}:
        return "local"
    if m in {"distributed", "big", "big-brain"}:
        return "distributed"
    return m


def _is_process_running(pattern: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-f", pattern],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _wait_until(predicate, timeout_sec=20, interval_sec=0.5) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 102, exc_info=True)
        time.sleep(interval_sec)
    return False


def _write_state(mode: str, ok: bool, api_url: str, note: str = "") -> None:
    payload = {
        "timestamp": time.time(),
        "mode": mode,
        "success": ok,
        "api": api_url,
        "note": note,
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write brain state file: {e}")


def _load_ngl_hint() -> Dict[str, Any]:
    try:
        if os.path.exists(NGL_HINT_FILE):
            with open(NGL_HINT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if isinstance(data, dict):
                return data
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 130, exc_info=True)
    return {}


def _save_ngl_hint(data: Dict[str, Any]) -> None:
    try:
        tmp = NGL_HINT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data if isinstance(data, dict) else {}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, NGL_HINT_FILE)
    except Exception as e:
        logger.warning(f"Failed to write NGL hint file: {e}")


def get_recommended_ngl(default: int = 60) -> int:
    hint = _load_ngl_hint()
    try:
        v = int(hint.get("recommended_ngl"))
        return max(1, min(v, 200))
    except Exception:
        return int(default)


def _stop_local_server() -> None:
    subprocess.run(["pkill", "-f", "llama-server"], stderr=subprocess.DEVNULL)


def _stop_rpc_server() -> None:
    subprocess.run(["pkill", "-f", "rpc-server"], stderr=subprocess.DEVNULL)


def _start_rpc_server_if_needed() -> bool:
    if _is_process_running("rpc-server"):
        logger.info("RPC server already running")
        return True

    if not os.path.exists(RPC_START_SCRIPT):
        logger.error(f"RPC start script not found: {RPC_START_SCRIPT}")
        return False

    logger.info("Starting RPC server...")
    subprocess.Popen([RPC_START_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return _wait_until(lambda: _is_process_running("rpc-server"), timeout_sec=15, interval_sec=0.7)


def _start_local_server_if_needed() -> bool:
    if _is_process_running("llama-server"):
        logger.info("Local llama-server already running")
        return True

    if not os.path.exists(LLAMA_SERVER_PATH):
        logger.error(f"llama-server binary not found: {LLAMA_SERVER_PATH}")
        return False

    logger.info("Starting local llama-server...")
    gguf_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "models", "Gemma-3-TAIDE-12b-Chat-2602-Q4_K_M.gguf")
    cmd = [
        LLAMA_SERVER_PATH,
        "-m",
        gguf_path,
        "--port",
        "8080",
        "-c",
        "4096",
        "-ngl",
        "99",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    if proc.poll() is not None:
        return False

    return _wait_until(lambda: _is_process_running("llama-server"), timeout_sec=20, interval_sec=0.7)


def _check_http(url: str, timeout_sec=3, allow_loading: bool = False) -> Tuple[bool, str]:
    try:
        resp = requests.get(url, timeout=timeout_sec)
        if resp.status_code == 200:
            return True, "OK"
        if allow_loading and resp.status_code == 503:
            # llama.cpp /v1 returns 503 while the model is still loading.
            try:
                data = resp.json() if resp.headers.get("content-type", "").lower().startswith("application/json") else {}
                msg = (((data or {}).get("error") or {}).get("message") or "").strip().lower()
                if "loading model" in msg:
                    return True, "Loading model"
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 218, exc_info=True)
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


def check_system_health(required_mem_gb=4) -> Tuple[bool, str]:
    """
    Checks if the local system has enough free memory.
    Returns: (bool, message)
    """
    try:
        import psutil

        mem = psutil.virtual_memory()
        free_gb = mem.available / (1024 ** 3)
        if free_gb < required_mem_gb:
            return False, f"Low Memory: {free_gb:.1f}GB < required {required_mem_gb}GB"
        return True, f"Memory OK: {free_gb:.1f}GB"
    except Exception:
        try:
            out = subprocess.check_output(["vm_stat"], text=True)
            pages_free = 0
            pages_inactive = 0
            for line in out.splitlines():
                if "Pages free" in line:
                    pages_free = int(line.split(":", 1)[1].strip().strip("."))
                elif "Pages inactive" in line:
                    pages_inactive = int(line.split(":", 1)[1].strip().strip("."))
            free_gb = ((pages_free + pages_inactive) * 4096) / (1024 ** 3)
            if free_gb < required_mem_gb:
                return False, f"Low Memory: {free_gb:.1f}GB < required {required_mem_gb}GB"
            return True, f"Memory OK: {free_gb:.1f}GB"
        except Exception as e:
            logger.warning(f"Could not check memory: {e}")
            return True, "Memory check skipped"


def check_remote_health() -> Tuple[bool, str]:
    """Checks if Melchior inference API is online."""
    ok, msg = _check_http(
        f"http://{MELCHIOR_IP}:{MELCHIOR_API_PORT}/v1/models",
        timeout_sec=4,
        allow_loading=True,
    )
    if ok:
        return True, "Melchior API online" if msg == "OK" else f"Melchior API {msg}"

    # Fallback: Ollama also exposes OpenAI-compatible /v1 on port 11434 (very common).
    ok_ollama, msg_ollama = _check_http(
        f"http://{MELCHIOR_IP}:{MELCHIOR_OLLAMA_PORT}/v1/models",
        timeout_sec=4,
        allow_loading=True,
    )
    if ok_ollama:
        return True, "Melchior Ollama /v1 online" if msg_ollama == "OK" else f"Melchior Ollama /v1 {msg_ollama}"

    ok2, msg2 = _check_http(f"http://{MELCHIOR_IP}:{MELCHIOR_API_PORT}/health", timeout_sec=4)
    if ok2:
        return True, "Melchior health endpoint online"

    return False, f"Melchior offline ({msg}; {msg2})"


def check_local_health() -> Tuple[bool, str]:
    """Checks if local inference API is online."""
    try:
        from api.routing.service_registry import get_service_url as _gsurl3
        _local_models = _gsurl3("omlx_inference") + "/v1/models"
    except Exception:
        _local_models = "http://localhost:8080/v1/models"
    ok, msg = _check_http(_local_models, timeout_sec=2, allow_loading=True)
    if ok:
        return True, "Local API online" if msg == "OK" else f"Local API {msg}"
    if _is_process_running("llama-server"):
        return True, "Local process running"
    return False, f"Local offline ({msg})"


def _parse_mb_value(v: Any) -> float | None:
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            val = float(v)
            # heuristic: large number is likely MB; small maybe GB
            return val if val > 256 else val * 1024.0
        s = str(v).strip().lower()
        if not s:
            return None
        if s.endswith("mb") or s.endswith("mib"):
            return float(s[:-2].strip())
        if s.endswith("gb") or s.endswith("gib"):
            return float(s[:-2].strip()) * 1024.0
        val = float(s)
        return val if val > 256 else val * 1024.0
    except Exception:
        return None


def _extract_used_total_mb(payload: Any) -> Tuple[float | None, float | None]:
    """
    Best-effort parser for Melchior /api/status payload variants.
    Supports forms like:
      - {"gpu":{"memory":"10780/12288 MB"}}
      - {"gpu_memory":"10.5/12.0 GB"}
      - {"used_mb":10780,"total_mb":12288}
    """
    if isinstance(payload, dict):
        # Direct numeric keys
        used_keys = ["used_mb", "memory_used_mb", "vram_used_mb", "gpu_used_mb", "used"]
        total_keys = ["total_mb", "memory_total_mb", "vram_total_mb", "gpu_total_mb", "total"]
        used = None
        total = None
        for k in used_keys:
            if k in payload:
                used = _parse_mb_value(payload.get(k))
                if used is not None:
                    break
        for k in total_keys:
            if k in payload:
                total = _parse_mb_value(payload.get(k))
                if total is not None:
                    break
        if used is not None and total is not None:
            return used, total

        # Common nested containers
        for k in ["gpu", "cuda", "stats", "status"]:
            if k in payload and isinstance(payload.get(k), dict):
                u, t = _extract_used_total_mb(payload.get(k))
                if u is not None and t is not None:
                    return u, t

        # String memory fields
        for k in ["memory", "gpu_memory", "vram", "memory_usage"]:
            v = payload.get(k)
            if v is None:
                continue
            s = str(v)
            import re
            m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(mb|mib|gb|gib)?", s, re.IGNORECASE)
            if m:
                u = float(m.group(1))
                t = float(m.group(2))
                unit = (m.group(3) or "mb").lower()
                mul = 1024.0 if unit in {"gb", "gib"} else 1.0
                return u * mul, t * mul

        # Deep search
        for _k, v in payload.items():
            if isinstance(v, (dict, list)):
                u, t = _extract_used_total_mb(v)
                if u is not None and t is not None:
                    return u, t

    if isinstance(payload, list):
        for item in payload:
            u, t = _extract_used_total_mb(item)
            if u is not None and t is not None:
                return u, t

    return None, None


def get_melchior_runtime_status() -> Dict[str, Any]:
    """
    Unified runtime status for brain operations and chat status output.
    """
    out: Dict[str, Any] = {
        "agent_endpoint": MELCHIOR_AGENT_ENDPOINT,
        "v1_endpoint": MELCHIOR_API_ENDPOINT,
        "health_ok": False,
        "v1_ok": False,
        "v1_reachable": False,
        "v1_loading": False,
        "mode": "",
        "gpu_used_mb": None,
        "gpu_total_mb": None,
        "models": [],
        "errors": [],
    }

    # Agent health
    try:
        r = requests.get(f"{MELCHIOR_AGENT_ENDPOINT}/health", timeout=4)
        if r.status_code == 200:
            d = r.json() if "application/json" in (r.headers.get("content-type") or "") else {}
            out["health_ok"] = True
            if isinstance(d, dict):
                out["mode"] = str(d.get("mode") or out["mode"] or "")
                out["health"] = d
    except Exception as e:
        out["errors"].append(f"health:{e}")

    # Agent status (optional; richer metrics on newer server)
    try:
        r = requests.get(f"{MELCHIOR_AGENT_ENDPOINT}/api/status", timeout=5)
        if r.status_code == 200:
            d = r.json() if "application/json" in (r.headers.get("content-type") or "") else {}
            if isinstance(d, dict):
                out["agent_status"] = d
                mode = str(d.get("mode") or "").strip()
                if mode:
                    out["mode"] = mode
                used_mb, total_mb = _extract_used_total_mb(d)
                if used_mb is not None and total_mb is not None:
                    out["gpu_used_mb"] = round(float(used_mb), 2)
                    out["gpu_total_mb"] = round(float(total_mb), 2)
    except Exception as e:
        out["errors"].append(f"status:{e}")

    # Newer endpoint
    try:
        r = requests.get(f"{MELCHIOR_AGENT_ENDPOINT}/api/brain/status", timeout=4)
        if r.status_code == 200:
            d = r.json() if "application/json" in (r.headers.get("content-type") or "") else {}
            if isinstance(d, dict):
                out["brain_status"] = d
                mode = str(d.get("mode") or "").strip()
                if mode:
                    out["mode"] = mode
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 436, exc_info=True)

    # OpenAI /v1 models
    try:
        r = requests.get(f"{MELCHIOR_API_ENDPOINT}/models", timeout=4)
        out["v1_reachable"] = True
        if r.status_code == 200:
            d = r.json() if "application/json" in (r.headers.get("content-type") or "") else {}
            models = []
            for it in (d.get("data") or []) if isinstance(d, dict) else []:
                if isinstance(it, dict) and it.get("id"):
                    models.append(str(it.get("id")).strip())
            out["models"] = models
            out["v1_ok"] = True
        elif r.status_code in {502, 503}:
            # Big-brain startup often returns 503 "Loading model".
            try:
                txt = (r.text or "").lower()
            except Exception:
                txt = ""
            if ("loading model" in txt) or ("unavailable_error" in txt):
                out["v1_loading"] = True
    except Exception as e:
        out["errors"].append(f"v1:{e}")

    out["ok"] = bool(out["health_ok"] or out["v1_ok"] or out.get("v1_loading"))
    return out


def set_melchior_mode(mode: str, extra_payload: Dict[str, Any] | None = None) -> bool:
    """
    Remote Control: tell Melchior to switch modes via Agent API.
    Accepted local aliases are normalized to engineer.
    """
    normalized = _normalize_mode(mode)
    remote_mode = "engineer" if normalized == "local" else normalized

    logger.info(f"Sending mode switch to Melchior: {remote_mode}")
    if not _remote_agent_reachable():
        logger.warning("Melchior agent unreachable; skipping mode switch request")
        return False
    retries = 3
    for i in range(retries):
        try:
            payload: Dict[str, Any] = {"mode": remote_mode}
            if isinstance(extra_payload, dict):
                payload.update(extra_payload)
            resp = requests.post(
                f"{MELCHIOR_AGENT_ENDPOINT}/api/brain/switch",
                json=payload,
                timeout=12,
            )
            if resp.status_code == 200:
                data = resp.json() if "application/json" in (resp.headers.get("content-type") or "") else {}
                ok = bool((data or {}).get("success", True))
                if ok:
                    logger.info(f"Melchior switched: {data.get('status', 'ok') if isinstance(data, dict) else 'ok'}")
                    return True
                logger.warning(f"Melchior switch returned success=false: {data}")
                return False
            if resp.status_code == 404:
                logger.warning("Melchior agent endpoint not found; continuing with API health checks")
                return True
            logger.warning(f"Melchior mode switch returned HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Melchior switch attempt {i + 1}/{retries} failed: {e}")
            time.sleep(1.8)

    return False


def calibrate_distributed_ngl(
    target_gb: float = 8.0,
    tolerance_gb: float = 0.5,
    max_rounds: int = 4,
    min_ngl: int = 8,
    max_ngl: int = 80,
    initial_ngl: int | None = None,
    hard_cycle: bool = True,
) -> Dict[str, Any]:
    """
    Auto-calibrate Melchior distributed ngl toward target VRAM usage.
    This function is best-effort: on older Melchior agents that ignore ngl payload,
    it reports 'endpoint_may_ignore_ngl' instead of faking success.
    """
    target_mb = float(target_gb) * 1024.0
    tol_mb = max(64.0, float(tolerance_gb) * 1024.0)
    lo = max(1, int(min_ngl))
    hi = max(lo, int(max_ngl))
    seed_hint = get_recommended_ngl(default=int(os.environ.get("MELCHIOR_LLAMA_NGL", "60") or "60"))
    cur = int(initial_ngl if initial_ngl is not None else seed_hint)
    cur = max(lo, min(hi, cur))
    rounds = max(1, min(int(max_rounds), 8))

    result: Dict[str, Any] = {
        "success": False,
        "target_gb": float(target_gb),
        "tolerance_gb": float(tolerance_gb),
        "range": {"min_ngl": lo, "max_ngl": hi},
        "steps": [],
        "recommended_ngl": cur,
        "initial_ngl": cur,
        "seed_hint_ngl": seed_hint,
        "note": "",
    }

    best_step = None
    stable_same_usage_count = 0
    last_used_mb = None
    last_ngl = None

    for i in range(rounds):
        step: Dict[str, Any] = {"round": i + 1, "ngl": int(cur)}
        if hard_cycle:
            step["to_engineer"] = bool(set_melchior_mode("engineer"))
            time.sleep(1.2)

        payload = {
            # newer/older server variants
            "ngl": int(cur),
            "n_gpu_layers": int(cur),
            "llama_ngl": int(cur),
            "gpu_layers": int(cur),
        }
        step["switch_distributed"] = bool(set_melchior_mode("distributed", extra_payload=payload))
        if not step["switch_distributed"]:
            step["error"] = "switch_distributed_failed"
            result["steps"].append(step)
            break

        # warmup + wait
        try:
            step["warmup"] = melchior_client.warmup(
                model=os.environ.get("MAGI_MAIN_MODEL", ""),
                timeout=int(os.environ.get("MAGI_NGL_CAL_WARMUP_TIMEOUT_SEC", "90") or "90"),
            )
        except Exception as e:
            step["warmup"] = {"success": False, "error": str(e)}

        time.sleep(2.0)
        status = get_melchior_runtime_status()
        step["status"] = {
            "ok": status.get("ok"),
            "mode": status.get("mode"),
            "gpu_used_mb": status.get("gpu_used_mb"),
            "gpu_total_mb": status.get("gpu_total_mb"),
            "models": status.get("models", [])[:3],
        }

        used_mb = status.get("gpu_used_mb")
        total_mb = status.get("gpu_total_mb")
        if used_mb is None or total_mb is None:
            step["error"] = "gpu_usage_unavailable"
            result["steps"].append(step)
            continue

        used_mb = float(used_mb)
        diff_mb = used_mb - target_mb
        step["delta_mb"] = round(diff_mb, 2)
        step["delta_gb"] = round(diff_mb / 1024.0, 3)

        result["steps"].append(step)
        if best_step is None or abs(diff_mb) < abs(float(best_step.get("delta_mb", 1e9))):
            best_step = step
            result["recommended_ngl"] = int(cur)

        if last_used_mb is not None and last_ngl is not None and int(last_ngl) != int(cur):
            if abs(float(last_used_mb) - used_mb) < 96.0:
                stable_same_usage_count += 1
            else:
                stable_same_usage_count = 0
        last_used_mb = used_mb
        last_ngl = cur

        if abs(diff_mb) <= tol_mb:
            result["success"] = True
            result["note"] = "target_reached"
            break

        # If endpoint seems to ignore ngl, stop early and return best effort.
        if stable_same_usage_count >= 2:
            result["note"] = "endpoint_may_ignore_ngl"
            break

        if diff_mb > 0:
            hi = min(hi, cur - 1)
        else:
            lo = max(lo, cur + 1)
        if lo > hi:
            break
        nxt = (lo + hi) // 2
        if nxt == cur:
            if diff_mb > 0:
                nxt = max(lo, cur - 1)
            else:
                nxt = min(hi, cur + 1)
        cur = max(lo, min(hi, nxt))

    if not result["success"]:
        if not result["note"]:
            result["note"] = "best_effort"
        if best_step is not None:
            result["best_delta_gb"] = round(float(best_step.get("delta_mb", 0.0)) / 1024.0, 3)
        else:
            result["best_delta_gb"] = None

    # Persist for next run.
    hint_payload: Dict[str, Any] = {
        "timestamp": time.time(),
        "target_gb": float(target_gb),
        "tolerance_gb": float(tolerance_gb),
        "recommended_ngl": int(result.get("recommended_ngl") or cur),
        "success": bool(result.get("success")),
        "note": str(result.get("note") or ""),
        "best_delta_gb": result.get("best_delta_gb"),
        "rounds": int(rounds),
    }
    try:
        rt = get_melchior_runtime_status()
        if isinstance(rt, dict):
            hint_payload["gpu_used_mb"] = rt.get("gpu_used_mb")
            hint_payload["gpu_total_mb"] = rt.get("gpu_total_mb")
            models = rt.get("models") if isinstance(rt.get("models"), list) else []
            if models:
                hint_payload["model"] = models[0]
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 662, exc_info=True)
    _save_ngl_hint(hint_payload)
    result["hint_saved"] = True
    result["hint_path"] = NGL_HINT_FILE

    return result


def repair_big_brain(
    model: str = "",
    timeout_sec: int = 240,
    force_cycle: bool = True,
) -> Dict[str, Any]:
    """
    One-call Big Brain repair for chat/webhook invocation.
    """
    out: Dict[str, Any] = {
        "success": False,
        "mode_before": get_brain_mode(),
        "remote_repair": {},
        "remote_health": {},
        "mode_after": "",
    }
    ok_rr, rr_payload = remote_repair_distributed(
        model=model or os.environ.get("MAGI_MAIN_MODEL", ""),
        timeout_sec=timeout_sec,
        force_cycle=force_cycle,
    )
    out["remote_repair"] = rr_payload
    out["remote_repair"]["ok"] = bool(ok_rr)

    remote_ok, remote_msg = check_remote_health()
    out["remote_health"] = {"ok": bool(remote_ok), "message": str(remote_msg)}

    if not remote_ok:
        # keep CASPER available via local fallback if remote still broken
        _stop_rpc_server()
        set_melchior_mode("engineer")
        if _start_local_server_if_needed():
            local_ok, local_msg = check_local_health()
            out["local_fallback"] = {"ok": bool(local_ok), "message": str(local_msg)}
        else:
            out["local_fallback"] = {"ok": False, "message": "start_local_failed"}
    else:
        # ensure CASPER is in distributed stack
        restart_inference_engine("distributed", force=True)

    out["mode_after"] = get_brain_mode()
    out["success"] = bool(out["remote_health"].get("ok"))
    return out


def remote_repair_distributed(
    model: str = "",
    timeout_sec: int = 240,
    force_cycle: bool = True,
) -> Tuple[bool, dict]:
    """
    Ask Melchior to perform remote distributed self-repair/restart.
    Prefer dedicated endpoint /api/brain/recover.
    If endpoint is missing, fallback to existing engineer->distributed sequence.
    """
    use_model = (model or os.environ.get("MAGI_MAIN_MODEL", "") or "").strip()
    wait_sec = max(30, min(int(timeout_sec or 240), 900))

    payload = {
        "target": "distributed",
        "model": use_model,
        "wait_sec": wait_sec,
        "force_cycle": bool(force_cycle),
    }
    headers = {"Content-Type": "application/json", "User-Agent": "casper-brain-manager/1.0"}
    ops_token = (os.environ.get("MAGI_REMOTE_OPS_TOKEN") or "").strip()
    if ops_token:
        headers["X-MAGI-OPS-TOKEN"] = ops_token

    # 1) Dedicated recover endpoint
    try:
        recover_http_timeout = int(os.environ.get("MAGI_BIG_BRAIN_RECOVER_HTTP_TIMEOUT_SEC", "18") or "18")
    except Exception:
        recover_http_timeout = 18
    recover_http_timeout = max(8, min(recover_http_timeout, 45))
    try:
        resp = requests.post(
            f"{MELCHIOR_AGENT_ENDPOINT}/api/brain/recover",
            json=payload,
            headers=headers,
            timeout=(3, recover_http_timeout),
        )
        if resp.status_code != 404:
            data = resp.json() if "application/json" in (resp.headers.get("content-type") or "") else {"raw": resp.text}
            ok = bool((data or {}).get("success")) and int(resp.status_code) == 200
            return ok, {
                "transport": "recover_endpoint",
                "http_status": int(resp.status_code),
                "response": data,
            }
    except Exception as e:
        logger.warning(f"Melchior /api/brain/recover call failed: {e}")

    # 2) Fallback sequence on older agents
    out = {"transport": "fallback_sequence", "steps": {}}
    try:
        if force_cycle:
            out["steps"]["to_engineer"] = bool(set_melchior_mode("engineer"))
            time.sleep(2.0)
        out["steps"]["to_distributed"] = bool(set_melchior_mode("distributed"))
        if not out["steps"]["to_distributed"]:
            return False, out

        deadline = time.time() + wait_sec
        ready = False
        last_msg = ""
        hard_fail_count = 0
        while time.time() < deadline:
            ok, msg = check_remote_health()
            last_msg = msg
            if ok:
                # require 200-ready not merely "Loading model"
                if "loading model" not in str(msg).lower():
                    ready = True
                    break
            else:
                low = str(msg or "").lower()
                # Repeated transport failures usually mean node/network issue.
                # Break early to avoid blocking the entire tick/nightly loop too long.
                if any(k in low for k in ("timed out", "connecttimeout", "connection refused", "name or service not known", "offline")):
                    hard_fail_count += 1
                    if hard_fail_count >= 3:
                        break
            time.sleep(2.0)
        out["steps"]["wait_ready"] = {"ok": ready, "last": last_msg}

        if not ready:
            return False, out

        # Best-effort warmup
        try:
            from skills.bridge import melchior_client

            out["steps"]["warmup"] = melchior_client.warmup(model=use_model, timeout=min(120, wait_sec))
        except Exception as e:
            out["steps"]["warmup"] = {"success": False, "error": str(e)}

        return True, out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return False, out


def get_brain_mode() -> str:
    """Returns: distributed | local | mixed | offline"""
    rpc_running = _is_process_running("rpc-server")
    local_running = _is_process_running("llama-server")

    # oMLX 偵測（取代舊的 Ollama llama-server）
    omlx_running = _is_process_running("omlx")
    if not local_running and omlx_running:
        local_running = True

    if rpc_running and not local_running:
        return "distributed"
    if local_running and not rpc_running:
        return "local"
    if rpc_running and local_running:
        return "mixed"
    return "offline"


def restart_inference_engine(mode: str, force: bool = False):
    """
    Reconfigure CASPER inference stack.
    mode: local | distributed
    Returns: (success: bool, api_url: str)
    """
    target = _normalize_mode(mode)
    if target not in {"local", "distributed"}:
        return False, ""

    with BRAIN_SWITCH_LOCK:
        current_mode = get_brain_mode()

        if not force and current_mode == target:
            if target == "distributed":
                ok, _ = check_remote_health()
                if ok:
                    return True, MELCHIOR_API_ENDPOINT
            if target == "local":
                ok, _ = check_local_health()
                if ok:
                    return True, LOCAL_API_ENDPOINT

        if not force:
            required = 6 if target == "local" else 1.5
            mem_ok, mem_msg = check_system_health(required_mem_gb=required)
            if not mem_ok:
                logger.error(mem_msg)
                _write_state(target, False, "", mem_msg)
                return False, ""

        logger.info(f"Switching CASPER brain: {current_mode} -> {target}")

        if target == "distributed":
            _stop_local_server()

            if not _start_rpc_server_if_needed():
                note = "Failed to start RPC server"
                _write_state(target, False, "", note)
                logger.error(note)
                return False, ""

            set_melchior_mode("distributed")
            remote_ok, remote_msg = check_remote_health()
            if not remote_ok:
                _write_state(target, False, "", remote_msg)
                logger.error(remote_msg)
                if BRAIN_AUTO_FALLBACK_LOCAL:
                    logger.warning("Distributed unavailable, auto-falling back to local mode")
                    _stop_rpc_server()
                    set_melchior_mode("engineer")
                    if _start_local_server_if_needed():
                        local_ok, local_msg = check_local_health()
                        if local_ok:
                            note = f"auto fallback from distributed: {remote_msg}"
                            _write_state("local", True, LOCAL_API_ENDPOINT, note)
                            return True, LOCAL_API_ENDPOINT
                        _write_state("local", False, "", local_msg)
                return False, ""

            _write_state(target, True, MELCHIOR_API_ENDPOINT, "distributed ready")
            # Best-effort warmup: reduce first-request latency when Melchior is back.
            try:
                from skills.bridge import melchior_client

                melchior_client.list_openai_v1_models(force_refresh=True)
                melchior_client.warmup(model=os.environ.get("MAGI_MAIN_MODEL", ""), timeout=45)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 894, exc_info=True)
            return True, MELCHIOR_API_ENDPOINT

        # target == local
        _stop_rpc_server()
        set_melchior_mode("engineer")

        if not _start_local_server_if_needed():
            note = "Failed to start local llama-server"
            _write_state(target, False, "", note)
            logger.error(note)
            return False, ""

        local_ok, local_msg = check_local_health()
        if not local_ok:
            _write_state(target, False, "", local_msg)
            logger.error(local_msg)
            return False, ""

        _write_state(target, True, LOCAL_API_ENDPOINT, "local ready")
        return True, LOCAL_API_ENDPOINT


def switch_brain_mode(mode: str, force: bool = False):
    """
    Public API for switching modes.
    Returns a user-friendly string.
    """
    normalized = _normalize_mode(mode)
    if normalized not in {"local", "distributed"}:
        return f"Error: Invalid mode '{mode}'. Use 'local' or 'distributed'."

    success, url = restart_inference_engine(normalized, force=force)
    if success:
        return f"Successfully switched to {normalized} mode. Active API: {url}"
    return f"Failed to switch to {normalized} mode. Check logs for safety warnings."


def delegate_task(instruction: str, context: str = ""):
    """
    Delegates a task to Melchior (Engineer mode).
    """
    logger.info(f"Delegating task to Melchior: {instruction[:60]}")

    previous_mode = get_brain_mode()
    success, _ = restart_inference_engine("local")
    if not success:
        return "Error: Could not switch to local mode for delegation."

    full_prompt = f"Context: {context}\n\nTask: {instruction}"
    result = ""
    try:
        response = melchior_code(full_prompt)
        result = response.get("code", "") or response.get("error", "Unknown Error")
    except Exception as e:
        logger.error(f"Delegation failed: {e}")
        result = f"Error during delegation: {e}"
    finally:
        if previous_mode == "distributed":
            logger.info("Restoring distributed mode after delegation")
            restart_inference_engine("distributed", force=True)

    return f"Engineer Report:\n\n{result}"


def get_brain_status():
    """
    Human-readable brain status for UI/command output.
    """
    mode = get_brain_mode()

    if mode == "distributed":
        remote_ok, remote_msg = check_remote_health()
        rt = get_melchior_runtime_status()
        api_status = "Online" if remote_ok else f"Degraded ({remote_msg})"
        remote_mode = _normalize_mode(str(rt.get("mode") or ""))
        v1_ok = bool(rt.get("v1_ok"))
        v1_loading = bool(rt.get("v1_loading"))
        if remote_mode == "distributed":
            if v1_ok:
                api_status = "Online"
            elif v1_loading:
                api_status = "Warming up (v1 loading)"
            elif remote_ok:
                # Agent route is alive; /v1 may still be recycling.
                api_status = "Online (agent route)"
            else:
                api_status = f"Degraded ({remote_msg})"
        elif remote_mode != "distributed" and not (v1_ok or v1_loading):
            api_status = f"Degraded (Melchior mode={rt.get('mode') or 'unknown'})"
        model_hint = ""
        models = rt.get("models") if isinstance(rt.get("models"), list) else []
        if models:
            model_hint = f"\n- **Active Model:** {models[0]}"
        gpu_hint = ""
        if rt.get("gpu_used_mb") is not None and rt.get("gpu_total_mb") is not None:
            used_gb = float(rt["gpu_used_mb"]) / 1024.0
            total_gb = float(rt["gpu_total_mb"]) / 1024.0
            gpu_hint = f"\n- **Melchior GPU:** {used_gb:.2f} / {total_gb:.2f} GB"
        return (
            "🧠 **Current Brain:** Distributed (Big Brain)\n"
            "- **Model:** GLM/Qwen remote stack via Melchior\n"
            f"- **Status:** {api_status}\n"
            f"{model_hint}{gpu_hint}\n"
            "- **Role:** Commander"
        )

    if mode == "local":
        local_ok, local_msg = check_local_health()
        api_status = "Online" if local_ok else f"Degraded ({local_msg})"
        return (
            "🔌 **目前大腦模式:** Local (oMLX Direct)\n"
            "- **模型:** TAIDE-12b-Chat (MLX 4-bit)\n"
            f"- **狀態:** {api_status}\n"
            "- **角色:** Primary Inference Engine"
        )

    if mode == "mixed":
        return (
            "⚠️ **Current Brain:** Mixed/Transition\n"
            "- Both rpc-server and llama-server appear active.\n"
            "- Recommend switching explicitly to `local` or `distributed`."
        )

    return "❌ **Current Brain:** Offline\n- No inference engine running."


def _cli() -> int:
    ap = argparse.ArgumentParser(description="CASPER brain manager")
    ap.add_argument("--task", default="status", help="status|status-text|runtime|mode|switch|repair|calibrate")
    ap.add_argument("--mode", default="", help="local|distributed")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--model", default="")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--target-gb", type=float, default=8.0)
    ap.add_argument("--tol-gb", type=float, default=0.5)
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--min-ngl", type=int, default=8)
    ap.add_argument("--max-ngl", type=int, default=80)
    ap.add_argument("--initial-ngl", type=int, default=-1)
    args = ap.parse_args()

    task = (args.task or "status").strip().lower()
    if task == "help":
        print(json.dumps({"skill": "brain_manager", "tasks": ["status", "status-text", "runtime", "mode", "switch", "repair", "calibrate"], "description": "CASPER brain manager — 管理本地/分散式推論引擎"}, ensure_ascii=False, indent=2))
        return 0
    if task == "status":
        print(json.dumps({"ok": True, "mode": get_brain_mode(), "status": get_brain_status(), "runtime": get_melchior_runtime_status()}, ensure_ascii=False))
        return 0
    if task == "status-text":
        print(get_brain_status())
        return 0
    if task == "runtime":
        print(json.dumps({"ok": True, "runtime": get_melchior_runtime_status()}, ensure_ascii=False))
        return 0
    if task == "mode":
        print(get_brain_mode())
        return 0
    if task == "switch":
        mode = _normalize_mode(args.mode or "")
        if mode not in {"local", "distributed"}:
            print(json.dumps({"ok": False, "error": "invalid_mode"}, ensure_ascii=False))
            return 2
        msg = switch_brain_mode(mode, force=bool(args.force))
        ok = msg.lower().startswith("successfully")
        print(json.dumps({"ok": ok, "message": msg, "mode": mode}, ensure_ascii=False))
        return 0 if ok else 1
    if task == "repair":
        out = repair_big_brain(model=args.model, timeout_sec=max(30, int(args.timeout)), force_cycle=True)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("success") else 1
    if task == "calibrate":
        out = calibrate_distributed_ngl(
            target_gb=float(args.target_gb),
            tolerance_gb=float(args.tol_gb),
            max_rounds=int(args.max_rounds),
            min_ngl=int(args.min_ngl),
            max_ngl=int(args.max_ngl),
            initial_ngl=(None if int(args.initial_ngl) < 0 else int(args.initial_ngl)),
            hard_cycle=True,
        )
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("success") else 1

    print(json.dumps({"ok": False, "error": f"unknown_task:{task}"}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
