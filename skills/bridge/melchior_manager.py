import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from typing import Dict, List, Tuple

import requests

from skills.bridge.http_pool import get_session as _get_session
from skills.bridge import melchior_bridge, melchior_client
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

logger = logging.getLogger("MelchiorManager")

STATE_PATH = os.environ.get(
    "MAGI_MELCHIOR_SYNC_STATE_PATH",
    f"{_MAGI_ROOT}/.agent/melchior_sync_state.json",
)
SYNC_MIN_INTERVAL_SEC = int(os.environ.get("MELCHIOR_SYNC_MIN_INTERVAL_SEC", "900"))
SYNC_MAX_DELTA_FILES = int(os.environ.get("MELCHIOR_SYNC_MAX_DELTA_FILES", "800"))
SYNC_MAX_DELTA_BYTES = int(os.environ.get("MELCHIOR_SYNC_MAX_DELTA_BYTES", str(25 * 1024 * 1024)))

# "auto" will pick delta unless deletions detected or delta too large.
DEFAULT_SYNC_MODE = os.environ.get("MELCHIOR_SYNC_MODE", "auto").strip().lower() or "auto"

MELCHIOR_HOST = os.environ.get("MELCHIOR_HOST", "100.116.54.16")
MELCHIOR_PORT = int(os.environ.get("MELCHIOR_PORT", "5002"))
MELCHIOR_OLLAMA_PORT = int(os.environ.get("MELCHIOR_OLLAMA_PORT", "11434"))
MELCHIOR_BASE = f"http://{MELCHIOR_HOST}:{MELCHIOR_PORT}"
MELCHIOR_OLLAMA_BASE = f"http://{MELCHIOR_HOST}:{MELCHIOR_OLLAMA_PORT}"

QWEN_MAIN_MODEL = os.environ.get("MAGI_MAIN_MODEL", "taide-12b").strip() or "taide-12b"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _load_state() -> dict:
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("files", {})
                return data
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 53, exc_info=True)
    return {"files": {}, "last_sync_at": "", "last_sync_epoch": 0, "last_mode": "", "last_smoke": {}}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 63, exc_info=True)


def _should_exclude(rel_path: str) -> bool:
    p = (rel_path or "").replace("\\", "/")
    if not p or p.startswith("../"):
        return True
    parts = [x for x in p.split("/") if x]
    if not parts:
        return True

    blocked_dirs = {".versions", "__pycache__", ".git", ".agent", "logs", "cache"}
    if any(seg in blocked_dirs for seg in parts):
        return True

    name = parts[-1].lower()
    if name in {".ds_store"}:
        return True
    if name.endswith(".pyc") or name.endswith(".pyo"):
        return True
    return False


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_skills_tree(skills_dir: str) -> Dict[str, dict]:
    """
    Returns mapping: rel_path -> {sha256,size,mtime}
    """
    base = os.path.abspath(skills_dir)
    out: Dict[str, dict] = {}
    for root, _dirs, files in os.walk(base):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base).replace("\\", "/")
            if _should_exclude(rel):
                continue
            try:
                st = os.stat(full)
                out[rel] = {
                    "sha256": _sha256_file(full),
                    "size": int(st.st_size),
                    "mtime": int(st.st_mtime),
                }
            except Exception:
                continue
    return out


def _compute_delta(prev: Dict[str, dict], cur: Dict[str, dict]) -> Tuple[List[str], List[str], int]:
    changed = []
    deleted = []
    total_bytes = 0

    for path, meta in cur.items():
        before = prev.get(path)
        if not before or before.get("sha256") != meta.get("sha256"):
            changed.append(path)
            total_bytes += int(meta.get("size", 0) or 0)

    for path in prev.keys():
        if path not in cur:
            deleted.append(path)

    changed.sort()
    deleted.sort()
    return changed, deleted, total_bytes


def _build_zip(skills_dir: str, include_files: List[str], mode: str, deleted: List[str]) -> dict:
    """
    Build a zip via a staging directory + shutil.make_archive.
    This avoids edge cases where some remote unzip implementations reject certain ZIP features.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = tempfile.mkdtemp(prefix=f"magi_melchior_{mode}_")
    base = os.path.abspath(skills_dir)

    manifest = {
        "generated_at": _now_iso(),
        "mode": mode,
        "skills_dir": base,
        "include_files": include_files,
        "deleted_detected": deleted[:200],
        "note": "Delta zip overlays files. Deletions require full sync.",
    }

    written = 0
    bytes_written = 0
    try:
        with open(os.path.join(stage, "MAGI_SYNC_MANIFEST.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(manifest, ensure_ascii=False, indent=2))

        for rel in include_files:
            full = os.path.join(base, rel)
            if not os.path.exists(full):
                continue
            dst = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
            try:
                bytes_written += int(os.path.getsize(full))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 172, exc_info=True)
            written += 1

        tmp = tempfile.gettempdir()
        out_base = os.path.join(tmp, f"magi_melchior_sync_{mode}_{stamp}")
        zip_path = out_base + ".zip"
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 182, exc_info=True)
        shutil.make_archive(out_base, "zip", stage)
        return {"zip": zip_path, "files": written, "bytes": bytes_written, "stage": stage}
    finally:
        # Keep stage around only if debugging; otherwise remove it.
        if os.environ.get("MELCHIOR_SYNC_KEEP_STAGE", "0").strip().lower() not in {"1", "true", "yes", "on"}:
            try:
                shutil.rmtree(stage, ignore_errors=True)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 191, exc_info=True)


def melchior_health() -> dict:
    try:
        return melchior_client.check_health()
    except Exception as e:
        return {"online": False, "error": str(e)}


def _smoke_test_melchior(require_qwen: bool = True) -> dict:
    """
    Remote smoke tests (no internet). Validates:
    - agent health
    - ollama tags include main model (optional)
    - chat round-trip latency using main model
    """
    report = {"ts": _now_iso(), "ok": True, "checks": []}

    def _check(name: str, fn):
        start = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)
        ms = int((time.time() - start) * 1000)
        report["checks"].append({"name": name, "ok": bool(ok), "ms": ms, "detail": detail[:800] if isinstance(detail, str) else detail})
        if not ok:
            report["ok"] = False

    def _agent_health():
        r = _get_session().get(f"{MELCHIOR_BASE}/health", timeout=3)
        return (r.status_code == 200, r.text)

    def _ollama_tags():
        r = _get_session().get(f"{MELCHIOR_OLLAMA_BASE}/api/tags", timeout=4)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        models = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
        has_qwen = any(QWEN_MAIN_MODEL.lower() == (x or "").lower() for x in models)
        if require_qwen and (not has_qwen):
            return False, f"missing {QWEN_MAIN_MODEL}; available={models[:12]}"
        return True, f"has_qwen={has_qwen}; models={models[:8]}"

    def _chat_ping():
        payload = {"prompt": "Reply with exactly: pong", "model": QWEN_MAIN_MODEL, "timeout": 30}
        r = _get_session().post(f"{MELCHIOR_BASE}/api/chat", json=payload, timeout=35)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        text = (r.json() or {}).get("response", "")
        ok = "pong" in (text or "").lower()
        return ok, text

    def _vision_ping():
        # 1x1 transparent PNG
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X9pS8AAAAASUVORK5CYII="
        )
        img = base64.b64encode(tiny_png).decode("utf-8")
        payload = {"prompt": "Describe the image in one short sentence.", "image": img}
        r = _get_session().post(f"{MELCHIOR_BASE}/api/vision", json=payload, timeout=25)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        text = (r.json() or {}).get("response", "")
        return bool(text.strip()), text

    # Endpoint presence checks
    def _endpoint_matrix():
        paths = ["/api/skills/sync", "/api/chat", "/api/vision", "/api/code", "/api/update_agent"]
        statuses = {}
        for p in paths:
            try:
                rr = requests.options(f"{MELCHIOR_BASE}{p}", timeout=3)
                statuses[p] = rr.status_code
            except Exception as e:
                statuses[p] = f"error:{e}"
        # update_agent is optional; lack is acceptable
        ok = all(str(statuses.get(p)).startswith("2") for p in ["/api/chat", "/api/vision", "/api/code", "/api/skills/sync"])
        return ok, statuses

    _check("melchior_agent_health", _agent_health)
    _check("melchior_endpoints", _endpoint_matrix)
    _check("melchior_ollama_tags", _ollama_tags)
    _check("melchior_chat_ping_qwen", _chat_ping)
    _check("melchior_vision_ping", _vision_ping)

    return report


def sync_skills_to_melchior(
    skills_dir: str = f"{_MAGI_ROOT}/skills",
    mode: str = "",
    force: bool = False,
    smoke_test: bool = True,
) -> dict:
    """
    Rate-limited incremental sync to Melchior.
    - mode: auto|delta|full
    - delta: only changed/new files (overlay). Deletions trigger full.
    - full: all files.
    """
    skills_dir = (skills_dir or "").strip()
    if not skills_dir or not os.path.isdir(skills_dir):
        return {"success": False, "error": f"skills_dir not found: {skills_dir}"}

    mode = (mode or DEFAULT_SYNC_MODE).strip().lower()
    if mode not in {"auto", "delta", "full"}:
        mode = "auto"

    state = _load_state()
    last_epoch = int(state.get("last_sync_epoch", 0) or 0)
    now_epoch = int(time.time())
    if (not force) and last_epoch and (now_epoch - last_epoch) < int(max(30, SYNC_MIN_INTERVAL_SEC)):
        return {
            "success": True,
            "action": "skipped_rate_limited",
            "min_interval_sec": SYNC_MIN_INTERVAL_SEC,
            "since_last_sec": now_epoch - last_epoch,
            "state": {"last_sync_at": state.get("last_sync_at"), "last_mode": state.get("last_mode")},
        }

    prev_files = state.get("files", {}) if isinstance(state.get("files"), dict) else {}
    cur_files = _scan_skills_tree(skills_dir)
    changed, deleted, delta_bytes = _compute_delta(prev_files, cur_files)

    if not changed and not deleted:
        return {"success": True, "action": "skipped_no_changes", "changed": 0, "deleted": 0}

    # Decide full vs delta
    selected_mode = mode
    if selected_mode == "auto":
        selected_mode = "delta"
        if deleted:
            selected_mode = "full"
        if len(changed) > SYNC_MAX_DELTA_FILES or delta_bytes > SYNC_MAX_DELTA_BYTES:
            selected_mode = "full"
    if selected_mode == "delta" and deleted:
        selected_mode = "full"

    include = list(cur_files.keys()) if selected_mode == "full" else changed
    pkg = _build_zip(skills_dir, include_files=include, mode=selected_mode, deleted=deleted)

    # Push to Melchior
    push = melchior_bridge.sync_skills(pkg["zip"])
    ok = isinstance(push, dict) and bool(push.get("success"))
    result = {
        "success": bool(ok),
        "action": "synced" if ok else "failed",
        "mode": selected_mode,
        "zip": pkg["zip"],
        "zip_files": pkg["files"],
        "zip_bytes": pkg["bytes"],
        "changed": len(changed),
        "deleted": len(deleted),
        "delta_bytes": delta_bytes,
        "melchior_result": push,
    }

    smoke = {}
    if ok and smoke_test:
        smoke = _smoke_test_melchior(require_qwen=True)
        result["smoke"] = smoke

    # Persist state only if push succeeded.
    if ok:
        state["files"] = cur_files
        state["last_sync_epoch"] = now_epoch
        state["last_sync_at"] = _now_iso()
        state["last_mode"] = selected_mode
        state["last_smoke"] = smoke or {}
        _save_state(state)

    return result
