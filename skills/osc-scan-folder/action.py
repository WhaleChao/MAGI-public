#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
osc-scan-folder/action.py

Wrapper around `osc-orchestrator scan_folder` / `掃描資料夾待辦`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import config_candidates, get_magi_root_dir
from api.case_path_mapper import default_scan_roots, translate_synology_path_to_local

ORCH_ACTION = str(get_magi_root_dir() / "skills" / "osc-orchestrator" / "action.py")
CONFIG_CANDIDATES = [str(p) for p in config_candidates("config.json")]


def _ok(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _load_jsonish(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if not t:
        return {}
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"value": t}


def _run_orch(task: str, timeout_sec: int) -> Dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("MAGI_NO_DELETE", "1")
    try:
        r = subprocess.run(
            [sys.executable, ORCH_ACTION, "--task", task],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=max(5, int(timeout_sec)),
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"timeout ({timeout_sec}s)", "detail": task}
    except Exception as e:
        return {"success": False, "error": f"spawn failed: {e}", "detail": task}

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    try:
        parsed = json.loads(out) if out else {}
    except Exception:
        parsed = {"ok": False, "raw": out[:800]}
    ok = (r.returncode == 0) and bool(parsed.get("ok"))
    return {"success": bool(ok), "returncode": r.returncode, "result": parsed, "stderr_tail": err[-800:]}


def _normalize_candidate_path(p: str) -> str:
    s = str(p or "").strip()
    if not s:
        return ""
    local = translate_synology_path_to_local(s)
    if local.startswith("/Users/") or local.startswith("/Volumes/"):
        return os.path.abspath(local)
    return os.path.abspath(s)


def _load_config_root() -> str:
    for path in CONFIG_CANDIDATES:
        try:
            if not os.path.exists(path):
                continue
            obj = json.loads(open(path, "r", encoding="utf-8").read() or "{}")
            if not isinstance(obj, dict):
                continue
            raw = str(obj.get("pigeonhole_staging") or "").strip()
            if raw:
                return _normalize_candidate_path(raw)
        except Exception:
            continue
    return ""


def _resolve_default_root() -> str:
    candidates = []
    for k in ("MAGI_SCAN_FOLDER_ROOT", "MAGI_PIGEONHOLE_STAGING", "PIGEONHOLE_STAGING"):
        v = str(os.environ.get(k) or "").strip()
        if v:
            candidates.append(v)
    cfg_root = _load_config_root()
    if cfg_root:
        candidates.append(cfg_root)
    candidates.extend(default_scan_roots())

    seen = set()
    for c in candidates:
        p = _normalize_candidate_path(c)
        if not p or p in seen:
            continue
        seen.add(p)
        if os.path.isdir(p):
            return p
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="osc-scan-folder skill")
    ap.add_argument("--task", default="help", help="task text")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({"success": True, "commands": ["help", "self_test", "run {..json..}", "掃描資料夾待辦 {..json..}"]})

    if task == "self_test":
        # No safe universal folder to scan here; just ensure orchestrator task wiring works.
        root = _resolve_default_root() or "/tmp"
        res = _run_orch("scan_folder " + json.dumps({"root": root, "max_files": 1, "dry_run": True}, ensure_ascii=False), 30)
        return _ok({"success": True, "checks": {"scan_folder_dry_run": res}})

    if task.startswith("run") or task.startswith("掃描資料夾待辦"):
        key = "run" if task.startswith("run") else "掃描資料夾待辦"
        payload = _load_jsonish(task[len(key) :].strip())
        if not isinstance(payload, dict):
            payload = {}
        if not str(payload.get("root") or payload.get("path") or "").strip():
            auto_root = _resolve_default_root()
            if auto_root:
                payload["root"] = auto_root
        payload.setdefault("max_files", 200)
        payload.setdefault("dry_run", False)
        timeout_sec = int(payload.pop("timeout_sec", 240) or 240)
        if not str(payload.get("root") or "").strip():
            return _ok(
                {
                    "success": False,
                    "error": "找不到可用掃描根目錄（請設定 MAGI_SCAN_FOLDER_ROOT 或 config.json 的 pigeonhole_staging）",
                }
            )
        res = _run_orch("scan_folder " + json.dumps(payload, ensure_ascii=False), timeout_sec)
        return _ok(res)

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())
