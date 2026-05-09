#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Continuous long-file 3-channel stress runner (non-stop mode).

Design goals:
- Run real Orchestrator file translation flows (no fake stubs).
- Keep running until 24h observer window ends.
- Never block forever on a single task (per-task process timeout + terminate).
- Emit task-level JSONL and final summary JSON.
- Optionally merge with observer final report into one combined final report.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2])))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.runtime_paths import get_metrics_dir

METRICS_DIR = get_metrics_dir()
DEFAULT_JUDGMENT_DIR = Path("/Users/ai/Desktop/判決")
DEFAULT_OBS_STATUS = METRICS_DIR / "stability_observe_24h_current_status.json"
DEFAULT_STRESS_JSONL = METRICS_DIR / "stress_longfile_3ch.jsonl"
DEFAULT_STRESS_SUMMARY = METRICS_DIR / "stress_longfile_3ch_summary.json"
DEFAULT_COMBINED_FINAL = METRICS_DIR / "stability_final_with_stress.json"


CHANNELS: List[Tuple[str, str]] = [
    ("Discord", "discord_stress_24h"),
    ("Telegram", "telegram_stress_24h"),
    ("LINE", "U" + "8" * 24),
]

PROMPTS: List[Tuple[str, str]] = [
    ("translate", "請翻譯這份檔案並給我TXT"),
    ("translate_summary", "請翻譯這份檔案並摘要，給我TXT"),
]


@dataclass
class TaskSpec:
    cycle: int
    seq: int
    platform: str
    user_id: str
    prompt_kind: str
    prompt: str
    file_path: str
    filename: str
    file_size: int


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _extract_file_path(reply: str) -> str:
    s = str(reply or "")
    if "|||FILE_PATH|||" not in s:
        return ""
    _, p = s.split("|||FILE_PATH|||", 1)
    return p.strip()


def _read_head(path: Path, n: int = 1800) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:n]
    except Exception:
        return ""


def _load_observer_status(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _estimate_end_time(status: Dict[str, Any], hours: int, explicit_from_ts: str = "") -> datetime:
    if explicit_from_ts:
        try:
            return datetime.fromisoformat(explicit_from_ts) + timedelta(hours=max(1, int(hours)))
        except Exception:
            pass

    last = status.get("last_snapshot") if isinstance(status.get("last_snapshot"), dict) else {}
    ws = str(last.get("window_start") or "").strip()
    if ws:
        try:
            return datetime.fromisoformat(ws) + timedelta(hours=max(1, int(hours)))
        except Exception:
            pass

    return datetime.now() + timedelta(hours=max(1, int(hours)))


def _discover_long_pdfs(folder: Path) -> List[Path]:
    files = [p for p in folder.glob("*.pdf") if p.is_file()]
    files.sort(key=lambda x: x.stat().st_size, reverse=True)
    return files


def _worker_process(task: Dict[str, Any], out_path: str) -> None:
    """
    Subprocess worker for a single Orchestrator task.
    Writes result JSON to out_path so parent can read reliably.
    """
    result: Dict[str, Any] = {
        "success": False,
        "error": "",
        "reply": "",
    }
    try:
        os.chdir(str(MAGI_ROOT))
        import sys

        sys.path.insert(0, str(MAGI_ROOT))
        from api.orchestrator import Orchestrator  # pylint: disable=import-outside-toplevel

        orc = Orchestrator()
        reply = orc.process_message(
            task["user_id"],
            task["prompt"],
            platform=task["platform"],
            role="admin",
            attachment={
                "type": "file",
                "path": task["file_path"],
                "filename": task["filename"],
            },
        )
        result["success"] = True
        result["reply"] = str(reply or "")
    except Exception as e:
        result["success"] = False
        result["error"] = f"{type(e).__name__}: {e}"

    Path(out_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def _run_task_with_timeout(task: TaskSpec, timeout_sec: int) -> Dict[str, Any]:
    t0 = time.time()
    tmp = METRICS_DIR / f"stress_task_{os.getpid()}_{int(t0*1000)}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)

    proc = mp.Process(target=_worker_process, args=(asdict(task), str(tmp)), daemon=False)
    proc.start()
    proc.join(timeout=max(30, int(timeout_sec)))

    timed_out = False
    if proc.is_alive():
        timed_out = True
        try:
            proc.terminate()
        except Exception:
            pass
        proc.join(8)
        if proc.is_alive():
            try:
                os.kill(proc.pid, signal.SIGKILL)  # nosec B108
            except Exception:
                pass
            proc.join(2)

    elapsed_ms = int((time.time() - t0) * 1000)
    payload: Dict[str, Any] = {}
    if not timed_out and tmp.exists():
        try:
            payload = json.loads(tmp.read_text(encoding="utf-8"))
        except Exception:
            payload = {"success": False, "error": "worker_output_parse_failed", "reply": ""}
    else:
        payload = {"success": False, "error": "task_timeout", "reply": ""}

    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    reply = str(payload.get("reply") or "")
    export_path = _extract_file_path(reply)
    export_ok = False
    has_header = False
    has_summary = False
    exported_exists = False
    if export_path:
        p = Path(export_path)
        if p.exists() and p.is_file():
            exported_exists = True
            head = _read_head(p, n=1800)
            has_header = "MAGI Translation Output" in head and "[Translated Text]" in head
            has_summary = "摘要" in head
            export_ok = has_header and ("翻譯失敗" not in head)

    expected_summary = task.prompt_kind == "translate_summary"
    ok = bool(payload.get("success")) and export_ok and ((not expected_summary) or has_summary)

    return {
        "ts": _now_iso(),
        "cycle": task.cycle,
        "seq": task.seq,
        "platform": task.platform,
        "user_id": task.user_id,
        "prompt_kind": task.prompt_kind,
        "file": task.file_path,
        "filename": task.filename,
        "file_size": task.file_size,
        "elapsed_ms": elapsed_ms,
        "ok": bool(ok),
        "timed_out": bool(timed_out),
        "worker_success": bool(payload.get("success")),
        "error": str(payload.get("error") or ""),
        "reply_preview": reply[:360],
        "export_path": export_path,
        "exported_exists": exported_exists,
        "has_header": has_header,
        "has_summary": has_summary,
    }


def _build_tasks(files: List[Path], cycle: int, start_seq: int) -> Tuple[List[TaskSpec], int]:
    out: List[TaskSpec] = []
    seq = start_seq
    for f in files:
        size = int(f.stat().st_size)
        for platform, uid in CHANNELS:
            for kind, prompt in PROMPTS:
                seq += 1
                out.append(
                    TaskSpec(
                        cycle=cycle,
                        seq=seq,
                        platform=platform,
                        user_id=f"{uid}_{cycle:03d}_{seq:05d}",
                        prompt_kind=kind,
                        prompt=prompt,
                        file_path=str(f),
                        filename=f.name,
                        file_size=size,
                    )
                )
    return out, seq


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    passed = sum(1 for r in rows if r.get("ok"))
    failed = total - passed
    timeout = sum(1 for r in rows if r.get("timed_out"))
    by_channel: Dict[str, Dict[str, int]] = {}
    by_prompt: Dict[str, Dict[str, int]] = {}
    by_file: Dict[str, Dict[str, int]] = {}

    for r in rows:
        c = str(r.get("platform") or "unknown")
        p = str(r.get("prompt_kind") or "unknown")
        f = str(r.get("filename") or "unknown")
        by_channel.setdefault(c, {"pass": 0, "fail": 0})
        by_prompt.setdefault(p, {"pass": 0, "fail": 0})
        by_file.setdefault(f, {"pass": 0, "fail": 0})
        key = "pass" if r.get("ok") else "fail"
        by_channel[c][key] += 1
        by_prompt[p][key] += 1
        by_file[f][key] += 1

    elapsed_values = [int(r.get("elapsed_ms") or 0) for r in rows if int(r.get("elapsed_ms") or 0) > 0]
    elapsed_values.sort()

    def _pct(arr: List[int], p: float) -> int:
        if not arr:
            return 0
        idx = int(round((len(arr) - 1) * p))
        idx = max(0, min(len(arr) - 1, idx))
        return int(arr[idx])

    return {
        "total": total,
        "pass": passed,
        "fail": failed,
        "pass_rate": round((passed / total) * 100.0, 2) if total else 0.0,
        "timeout_count": timeout,
        "latency_ms": {
            "p50": _pct(elapsed_values, 0.50),
            "p95": _pct(elapsed_values, 0.95),
            "max": max(elapsed_values) if elapsed_values else 0,
        },
        "by_channel": by_channel,
        "by_prompt": by_prompt,
        "by_file": by_file,
    }


def _merge_with_observer(
    *,
    stress_summary: Dict[str, Any],
    observer_final_path: Path,
    combined_out: Path,
) -> Dict[str, Any]:
    observer_payload: Dict[str, Any] = {}
    if observer_final_path.exists():
        try:
            observer_payload = json.loads(observer_final_path.read_text(encoding="utf-8"))
        except Exception:
            observer_payload = {}

    merged = {
        "generated_at": _now_iso(),
        "observer_final_path": str(observer_final_path),
        "observer": observer_payload,
        "stress": stress_summary,
        "overall_ok": bool(stress_summary.get("aggregate", {}).get("fail", 1) == 0),
    }
    _write_json(combined_out, merged)

    # Optional TXT export for quick reading in channels/UI.
    try:
        import importlib.util

        export_mod_path = MAGI_ROOT / "skills" / "ops" / "export_text.py"
        spec = importlib.util.spec_from_file_location("export_text", str(export_mod_path))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            report_lines = [
                "MAGI 最終穩定化報告（24h + 全量長檔三通道壓測）",
                f"生成時間: {merged['generated_at']}",
                "",
                "[壓測總覽]",
                f"- pass={stress_summary.get('aggregate', {}).get('pass', 0)}",
                f"- fail={stress_summary.get('aggregate', {}).get('fail', 0)}",
                f"- pass_rate={stress_summary.get('aggregate', {}).get('pass_rate', 0)}%",
                f"- timeout_count={stress_summary.get('aggregate', {}).get('timeout_count', 0)}",
                "",
                "[壓測延遲]",
                f"- p50={stress_summary.get('aggregate', {}).get('latency_ms', {}).get('p50', 0)} ms",
                f"- p95={stress_summary.get('aggregate', {}).get('latency_ms', {}).get('p95', 0)} ms",
                f"- max={stress_summary.get('aggregate', {}).get('latency_ms', {}).get('max', 0)} ms",
            ]
            txt = "\n".join(report_lines).strip()
            export = mod.export_txt(txt, prefix="qa_final_stabilization_report")
            merged["txt_export"] = export
            _write_json(combined_out, merged)
    except Exception:
        pass

    return merged


def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous long-file 3-channel stress runner")
    ap.add_argument("--judgment-dir", default=str(DEFAULT_JUDGMENT_DIR))
    ap.add_argument("--observer-status-path", default=str(DEFAULT_OBS_STATUS))
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--from-ts", default="")
    ap.add_argument("--task-timeout-sec", type=int, default=1800)
    ap.add_argument("--pause-sec", type=float, default=1.5)
    ap.add_argument("--jsonl-path", default=str(DEFAULT_STRESS_JSONL))
    ap.add_argument("--summary-path", default=str(DEFAULT_STRESS_SUMMARY))
    ap.add_argument("--combined-final-path", default=str(DEFAULT_COMBINED_FINAL))
    ap.add_argument("--wait-observer-final-sec", type=int, default=1200)
    ap.add_argument("--max-tasks", type=int, default=0, help="for debugging; 0 means unlimited")
    args = ap.parse_args()

    judgment_dir = Path(args.judgment_dir).expanduser()
    status_path = Path(args.observer_status_path).expanduser()
    jsonl_path = Path(args.jsonl_path).expanduser()
    summary_path = Path(args.summary_path).expanduser()
    combined_out = Path(args.combined_final_path).expanduser()

    files = _discover_long_pdfs(judgment_dir)
    if not files:
        print(json.dumps({"success": False, "error": f"no pdf files under {judgment_dir}"}, ensure_ascii=False))
        return 2

    obs_status = _load_observer_status(status_path)
    end_at = _estimate_end_time(obs_status, int(args.hours), explicit_from_ts=str(args.from_ts or ""))
    observer_final_path = Path(
        str(obs_status.get("final_json_path") or (METRICS_DIR / "stability_observe_24h_current_final.json"))
    )

    print(f"[stress] start={_now_iso()} files={len(files)} end_at={end_at.isoformat(timespec='seconds')}")
    print(f"[stress] jsonl={jsonl_path}")
    print(f"[stress] summary={summary_path}")

    rows: List[Dict[str, Any]] = []
    cycle = 0
    seq = 0
    tasks_done = 0
    stop_reason = "time_window_reached"

    _write_json(
        summary_path,
        {
            "running": True,
            "generated_at": _now_iso(),
            "stop_reason": "",
            "tasks_done": 0,
            "cycle": 0,
            "aggregate": _aggregate(rows),
            "jsonl_path": str(jsonl_path),
            "observer_status_path": str(status_path),
            "observer_final_path": str(observer_final_path),
        },
    )

    while True:
        now = datetime.now()
        if now >= end_at:
            stop_reason = "time_window_reached"
            break

        status = _load_observer_status(status_path)
        if isinstance(status, dict) and (status.get("running") is False):
            stop_reason = "observer_finished"
            break

        cycle += 1
        tasks, seq = _build_tasks(files, cycle=cycle, start_seq=seq)
        for task in tasks:
            if args.max_tasks > 0 and tasks_done >= int(args.max_tasks):
                stop_reason = "max_tasks_reached"
                break

            row = _run_task_with_timeout(task, timeout_sec=int(args.task_timeout_sec))
            _append_jsonl(jsonl_path, row)
            rows.append(row)
            tasks_done += 1

            partial = {
                "running": True,
                "generated_at": _now_iso(),
                "stop_reason": "",
                "tasks_done": tasks_done,
                "cycle": cycle,
                "aggregate": _aggregate(rows),
                "jsonl_path": str(jsonl_path),
                "observer_status_path": str(status_path),
                "observer_final_path": str(observer_final_path),
            }
            _write_json(summary_path, partial)

            time.sleep(max(0.0, float(args.pause_sec)))

        if stop_reason == "max_tasks_reached":
            break

    summary = {
        "running": False,
        "generated_at": _now_iso(),
        "stop_reason": stop_reason,
        "tasks_done": tasks_done,
        "cycles_done": cycle,
        "config": {
            "judgment_dir": str(judgment_dir),
            "files": [str(x) for x in files],
            "task_timeout_sec": int(args.task_timeout_sec),
            "pause_sec": float(args.pause_sec),
            "hours": int(args.hours),
            "from_ts": str(args.from_ts or ""),
            "observer_status_path": str(status_path),
            "observer_final_path": str(observer_final_path),
        },
        "aggregate": _aggregate(rows),
        "jsonl_path": str(jsonl_path),
    }
    _write_json(summary_path, summary)

    # Wait for observer final output, then merge.
    wait_sec = max(0, int(args.wait_observer_final_sec))
    t0 = time.time()
    while wait_sec > 0 and (time.time() - t0) < wait_sec:
        if observer_final_path.exists():
            break
        time.sleep(10)

    merged = _merge_with_observer(
        stress_summary=summary,
        observer_final_path=observer_final_path,
        combined_out=combined_out,
    )

    print(json.dumps({"success": True, "summary_path": str(summary_path), "combined_final_path": str(combined_out), "overall_ok": merged.get("overall_ok")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
