#!/usr/bin/env python3
"""Durable court-grade live runner used by the Discord command ``勘驗``."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
MAGI_ROOT = SKILL_DIR.parents[1]
ACTION = SKILL_DIR / "action.py"
WORKER_PYTHON = Path(
    os.environ.get("MAGI_FORENSIC_TRANSCRIPT_PYTHON")
    or ((MAGI_ROOT / "venv" / "bin" / "python3") if (MAGI_ROOT / "venv" / "bin" / "python3").is_file() else sys.executable)
).expanduser()
DEFAULT_AGENT_DIR = Path(
    os.environ.get("MAGI_AGENT_DIR") or (MAGI_ROOT / ".agent")
).expanduser().resolve()
DEFAULT_MANIFEST = Path(
    os.environ.get("MAGI_FORENSIC_TRANSCRIPT_LIVE_JOB")
    or (DEFAULT_AGENT_DIR / "forensic_transcript_live_job.json")
).expanduser().resolve()
DEFAULT_STATE = DEFAULT_AGENT_DIR / "forensic-transcript-live" / "state.json"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_json_list(path: Path) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _process_identity(pid: Any) -> dict[str, Any]:
    """Return fields that distinguish a live worker from PID reuse."""

    try:
        normalized = int(pid)
        os.kill(normalized, 0)
        pgid = os.getpgid(normalized)
    except (OSError, TypeError, ValueError):
        return {}
    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(normalized)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        started = completed.stdout.strip() if completed.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        started = ""
    if not started:
        return {}
    return {"pid": normalized, "pgid": int(pgid), "process_started": started}


def _identity_matches(state: Mapping[str, Any]) -> bool:
    expected = state.get("process_identity")
    if not isinstance(expected, Mapping):
        return False
    actual = _process_identity(state.get("pid"))
    return bool(actual) and all(actual.get(key) == expected.get(key) for key in actual)


def _terminate_owned_worker(
    state: Mapping[str, Any],
    *,
    grace_sec: float = 3.0,
) -> bool:
    """Terminate only the exact process group recorded at launch."""

    if not _identity_matches(state):
        return False
    identity = state["process_identity"]
    pid = int(identity["pid"])
    pgid = int(identity["pgid"])
    if pid != pgid:
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + max(0.0, grace_sec)
    while _process_identity(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _process_identity(pid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return not bool(_process_identity(pid))


def _manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    evidence: list[dict[str, Any]] = []
    for field in ("video", "transcript", "baseline", "asr_json", "secondary_asr_json"):
        raw = str(manifest.get(field) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        try:
            stat = path.stat()
            evidence.append(
                {"field": field, "path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            )
        except OSError:
            evidence.append({"field": field, "path": str(path), "missing": True})
    body = json.dumps(
        {"manifest": dict(manifest), "evidence": evidence},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _resolve_manifest(path: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    target = Path(path or DEFAULT_MANIFEST).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"勘驗工作設定不存在：{target}")
    manifest = _read_json(target)
    if not manifest:
        raise ValueError(f"勘驗工作設定不是有效 JSON object：{target}")
    return target, manifest


def _prepare_task(manifest: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    task = dict(manifest)
    for field in ("video", "transcript", "baseline", "asr_json"):
        raw = str(task.get(field) or "").strip()
        if not raw:
            raise ValueError(f"正式勘驗設定缺少 {field}")
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"正式勘驗輸入不存在：{path}")
        task[field] = str(path)

    secondary = str(task.get("secondary_asr_json") or "").strip()
    if secondary:
        secondary_path = Path(secondary).expanduser().resolve()
        if not secondary_path.is_file():
            raise FileNotFoundError(f"第二路 ASR 不存在：{secondary_path}")
        if secondary_path == Path(task["asr_json"]):
            raise ValueError("第二路 ASR 不得與第一路使用同一檔案")
        if not bool(task.get("secondary_asr_independent_verified")):
            raise ValueError("外部提供第二路 ASR 時，必須明示 secondary_asr_independent_verified=true")
        task["secondary_asr_json"] = str(secondary_path)

    if task.get("require_secondary_asr") is False:
        raise ValueError("正式勘驗禁止 require_secondary_asr=false")
    task["require_secondary_asr"] = True
    task["operation"] = "autonomous"
    task["max_visual_reviews"] = max(2000, int(task.get("max_visual_reviews", 2000) or 2000))
    timeout_sec = int(task.get("lifecycle_timeout_sec", 21600) or 21600)
    task["lifecycle_timeout_sec"] = max(300, min(timeout_sec, 86400))

    output_root = Path(
        str(task.pop("output_root", "") or (MAGI_ROOT / ".forensic-transcript-live"))
    ).expanduser().resolve()
    output_dir = output_root / run_id
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"正式勘驗輸出目錄不是空目錄：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    task["output_dir"] = str(output_dir)
    output_name = str(task.get("output_docx") or "完整譯文_MAGI正式自主雙重勘驗版.docx")
    output_docx = Path(output_name)
    if not output_docx.is_absolute():
        output_docx = output_dir / output_docx.name
    output_docx = output_docx.expanduser().resolve()
    if output_docx == Path(task["transcript"]):
        raise ValueError("正式勘驗不得覆寫來源譯文")
    task["output_docx"] = str(output_docx)
    return task


def _preflight(task: Mapping[str, Any]) -> dict[str, Any]:
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from video_agent import prepare_autonomous_video_review

    plan = prepare_autonomous_video_review(task)
    if not plan.get("timeline_complete"):
        raise RuntimeError(
            f"正式勘驗時間軸遭截斷：{plan.get('review_points_selected')}/"
            f"{plan.get('review_points_total')}"
        )
    if int(plan.get("review_points_selected", 0) or 0) <= 0:
        raise RuntimeError("正式勘驗沒有產生任何影音審查點")
    return {
        "turns": plan.get("turns"),
        "baseline_locked_turns": plan.get("baseline_locked_turns"),
        "speaker_review_findings": plan.get("speaker_review_findings"),
        "review_points_total": plan.get("review_points_total"),
        "review_points_selected": plan.get("review_points_selected"),
        "timeline_complete": plan.get("timeline_complete"),
    }


def _progress(state: Mapping[str, Any]) -> dict[str, Any]:
    output_dir = Path(str(state.get("output_dir") or ""))
    planned = int((state.get("preflight") or {}).get("review_points_selected", 0) or 0)
    first_rows = _read_json_list(output_dir / "visual-pass-1.json") if output_dir else []
    second_rows = _read_json_list(output_dir / "visual-pass-2.json") if output_dir else []
    first = len(first_rows)
    second = len(second_rows)
    if not first and output_dir:
        first = len(list((output_dir / "visual-pass-1").glob("*.jpg")))
    if not second and output_dir:
        second = len(list((output_dir / "visual-pass-2").glob("*.jpg")))
    return {
        "planned_points": planned,
        "visual_pass_1_frames": first,
        "visual_pass_2_frames": second,
        "secondary_asr_ready": (output_dir / "asr-secondary.json").is_file() if output_dir else False,
        "text_review_ready": (output_dir / "text-review-proposals.json").is_file() if output_dir else False,
        "autonomous_report_ready": (output_dir / "autonomous.json").is_file() if output_dir else False,
    }


def get_live_status(state_path: str | Path | None = None) -> dict[str, Any]:
    target = Path(state_path or DEFAULT_STATE).expanduser().resolve()
    state = _read_json(target)
    if not state:
        return {"status": "idle", "state_path": str(target)}
    if state.get("status") in {"starting", "running"}:
        deadline = float(state.get("deadline_at", 0) or 0)
        if deadline and time.time() >= deadline and _identity_matches(state):
            terminated = _terminate_owned_worker(state)
            state["status"] = "timed_out"
            state["error"] = (
                "worker exceeded lifecycle deadline"
                if terminated
                else "worker deadline expired but owned process group could not be confirmed closed"
            )
            state["finished_at"] = time.time()
            _atomic_json(target, state)
        elif state.get("pid") and not _identity_matches(state):
            state["status"] = "failed"
            state["error"] = "stale running state: worker PID/PGID/start identity no longer matches"
            state["finished_at"] = time.time()
            _atomic_json(target, state)
    if state.get("status") in {"starting", "running"} and not _pid_alive(state.get("pid")):
        report = _read_json(Path(str(state.get("output_dir") or "")) / "autonomous.json")
        if report:
            state["status"] = "completed" if report.get("court_grade_contract_satisfied") else "failed"
            state["result"] = {
                "passed": report.get("passed"),
                "court_grade_contract_satisfied": report.get("court_grade_contract_satisfied"),
                "output_docx": report.get("output_docx"),
                "unresolved_count": report.get("unresolved_count"),
            }
        else:
            state["status"] = "failed"
            state["error"] = "worker exited without autonomous.json"
        state["finished_at"] = state.get("finished_at") or time.time()
        _atomic_json(target, state)
    state["progress"] = _progress(state)
    state["state_path"] = str(target)
    return state


def cancel_live_job(state_path: str | Path | None = None) -> dict[str, Any]:
    target = Path(state_path or DEFAULT_STATE).expanduser().resolve()
    state = _read_json(target)
    if not state:
        return {"status": "idle", "state_path": str(target)}
    if state.get("status") not in {"starting", "running"}:
        state["state_path"] = str(target)
        return state
    if not _terminate_owned_worker(state):
        state.update(
            {
                "status": "failed",
                "error": "cancel refused: worker identity did not match or process group remained alive",
                "finished_at": time.time(),
            }
        )
    else:
        state.update(
            {
                "status": "cancelled",
                "error": "cancelled by operator",
                "finished_at": time.time(),
            }
        )
    _atomic_json(target, state)
    state["state_path"] = str(target)
    return state


def start_live_job(
    manifest_path: str | Path | None = None,
    *,
    state_path: str | Path | None = None,
    popen_factory: Any = subprocess.Popen,
) -> dict[str, Any]:
    target_state = Path(state_path or DEFAULT_STATE).expanduser().resolve()
    manifest_file, manifest = _resolve_manifest(manifest_path)
    fingerprint = _manifest_fingerprint(manifest)
    current = get_live_status(target_state)
    if current.get("status") in {"starting", "running"}:
        return current
    if current.get("manifest_fingerprint") == fingerprint and current.get("status") in {
        "completed",
        "failed",
    }:
        return current

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"live-{stamp}-{fingerprint[:8]}"
    task = _prepare_task(manifest, run_id)
    preflight = _preflight(task)
    state_dir = target_state.parent
    state_dir.mkdir(parents=True, exist_ok=True)
    task_path = state_dir / f"{run_id}.task.json"
    log_path = state_dir / f"{run_id}.log"
    _atomic_json(task_path, task)
    state: dict[str, Any] = {
        "status": "starting",
        "run_id": run_id,
        "manifest": str(manifest_file),
        "manifest_fingerprint": fingerprint,
        "task_path": str(task_path),
        "output_dir": task["output_dir"],
        "output_docx": task["output_docx"],
        "log_path": str(log_path),
        "preflight": preflight,
        "started_at": time.time(),
        "deadline_at": time.time() + float(task["lifecycle_timeout_sec"]),
        "court_mode": True,
        "task_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
    }
    _atomic_json(target_state, state)
    with log_path.open("ab", buffering=0) as log_stream:
        proc = popen_factory(
            [str(WORKER_PYTHON), str(Path(__file__).resolve()), "--worker", str(task_path), str(target_state)],
            cwd=str(MAGI_ROOT),
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    state["pid"] = int(proc.pid)
    identity = _process_identity(proc.pid)
    if not identity or identity.get("pgid") != int(proc.pid):
        state.update(
            {
                "status": "failed",
                "error": "spawned worker did not establish a verifiable owned process group",
                "finished_at": time.time(),
            }
        )
        _atomic_json(target_state, state)
        return get_live_status(target_state)
    state["process_identity"] = identity
    state["status"] = "running"
    _atomic_json(target_state, state)
    return get_live_status(target_state)


def format_live_status(state: Mapping[str, Any]) -> str:
    status = str(state.get("status") or "idle")
    if status == "idle":
        return f"⚠️ 尚未設定正式勘驗工作。請建立：`{DEFAULT_MANIFEST}`"
    progress = state.get("progress") or {}
    planned = int(progress.get("planned_points", 0) or 0)
    first = int(progress.get("visual_pass_1_frames", 0) or 0)
    second = int(progress.get("visual_pass_2_frames", 0) or 0)
    if status in {"starting", "running"}:
        return (
            "🎬 **正式勘驗執行中**\n"
            f"- 工作：`{state.get('run_id', '')}`\n"
            f"- PID：`{state.get('pid', '')}`\n"
            f"- 完整時間軸：`{planned}` 點\n"
            f"- 畫面 Pass 1：`{first}/{planned}`\n"
            f"- 畫面 Pass 2：`{second}/{planned}`\n"
            f"- 第二路 ASR：`{'完成' if progress.get('secondary_asr_ready') else '處理中'}`\n"
            "再次輸入 `勘驗` 可查詢進度。"
        )
    result = state.get("result") or {}
    if status == "completed":
        return (
            "✅ **正式勘驗完成**\n"
            f"- 法院級契約：`{bool(result.get('court_grade_contract_satisfied'))}`\n"
            f"- 未決項目：`{result.get('unresolved_count', 0)}`\n"
            f"- Word：`{result.get('output_docx') or state.get('output_docx', '')}`\n"
            "送件前仍須由承辦人作成最終法律確認。"
        )
    return (
        "❌ **正式勘驗未通過**\n"
        f"- 工作：`{state.get('run_id', '')}`\n"
        f"- 原因：`{state.get('error') or '法院級完成門檻未通過'}`\n"
        f"- 稽核目錄：`{state.get('output_dir', '')}`"
    )


def start_or_status(
    manifest_path: str | Path | None = None,
    *,
    state_path: str | Path | None = None,
) -> str:
    state = get_live_status(state_path)
    if state.get("status") in {"starting", "running"}:
        return format_live_status(state)
    try:
        state = start_live_job(manifest_path, state_path=state_path)
    except Exception as exc:
        return f"❌ 正式勘驗無法啟動：{type(exc).__name__}: {str(exc)[:500]}"
    return format_live_status(state)


def _worker(task_path: Path, state_path: Path) -> int:
    task = _read_json(task_path)
    state = _read_json(state_path)
    task_sha256 = hashlib.sha256(task_path.read_bytes()).hexdigest() if task_path.is_file() else ""
    if task_sha256 != state.get("task_sha256"):
        state.update(
            {
                "status": "failed",
                "finished_at": time.time(),
                "error": "worker task hash does not match launch state",
            }
        )
        _atomic_json(state_path, state)
        return 2
    identity = _process_identity(os.getpid())
    if not identity or identity != state.get("process_identity"):
        state.update(
            {
                "status": "failed",
                "finished_at": time.time(),
                "error": "worker process identity does not match launch state",
            }
        )
        _atomic_json(state_path, state)
        return 2
    state.update({"status": "running", "worker_started_at": time.time()})
    _atomic_json(state_path, state)
    try:
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        if str(MAGI_ROOT) not in sys.path:
            sys.path.insert(0, str(MAGI_ROOT))
        from video_agent import run_autonomous_video_review

        result = run_autonomous_video_review(task)
        court_passed = bool(result.get("court_grade_contract_satisfied"))
        state.update(
            {
                "status": "completed" if court_passed else "failed",
                "finished_at": time.time(),
                "result": {
                    "passed": result.get("passed"),
                    "court_grade_contract_satisfied": result.get("court_grade_contract_satisfied"),
                    "timeline_complete": result.get("timeline_complete"),
                    "secondary_asr_independent": result.get("secondary_asr_independent"),
                    "output_docx": result.get("output_docx"),
                    "unresolved_count": result.get("unresolved_count"),
                },
                "error": "" if court_passed else "法院級完成門檻未通過",
            }
        )
        _atomic_json(state_path, state)
        return 0 if court_passed else 2
    except Exception as exc:
        state.update(
            {
                "status": "failed",
                "finished_at": time.time(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        _atomic_json(state_path, state)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", nargs=2, metavar=("TASK_JSON", "STATE_JSON"))
    parser.add_argument("--manifest", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--cancel", action="store_true")
    args = parser.parse_args()
    if args.worker:
        return _worker(Path(args.worker[0]).resolve(), Path(args.worker[1]).resolve())
    if args.cancel:
        state = cancel_live_job(args.state or None)
    elif args.status:
        state = get_live_status(args.state or None)
    else:
        state = start_live_job(args.manifest or None, state_path=args.state or None)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
