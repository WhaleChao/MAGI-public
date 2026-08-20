from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from api.command_registry import CommandContext, CommandRegistry
from api.commands import forensic_transcript_commands
from api.help_text import build_help_text


ROOT = Path(__file__).resolve().parents[1]
LIVE_RUNTIME = (
    ROOT
    / "skills"
    / "forensic-transcript-verifier"
    / "scripts"
    / "live_runtime.py"
)


def _load_live_runtime():
    scripts = str(LIVE_RUNTIME.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("forensic_live_runtime_test", LIVE_RUNTIME)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LIVE = _load_live_runtime()


def _context(message: str, role: str = "admin") -> CommandContext:
    return CommandContext(
        user_id="discord-test",
        message=message,
        msg_lower=message.lower(),
        role=role,
        platform="Discord",
        orchestrator=object(),
    )


def test_discord_single_word_command_is_exact_and_admin_guarded(monkeypatch) -> None:
    registry = CommandRegistry()
    forensic_transcript_commands.register_forensic_transcript_commands(registry)
    monkeypatch.setattr(
        forensic_transcript_commands,
        "_start_or_status",
        lambda: "正式勘驗已啟動",
    )

    assert registry.dispatch(_context("勘驗")) == "正式勘驗已啟動"
    assert registry.dispatch(_context("請勘驗")) is None
    assert "管理員限定" in registry.dispatch(_context("勘驗", role="user"))
    assert "`勘驗`" in build_help_text("admin")


def _fixture_manifest(tmp_path: Path, *, require_secondary: bool = True) -> Path:
    video = tmp_path / "hearing.mp4"
    video.write_bytes(b"video")
    transcript = tmp_path / "transcript.txt"
    transcript.write_text(
        """[00:00:10–00:00:12] 檢察官\n「問題」

[00:00:00.50] 檢察官\n「人工節文」""",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("[00:00:00.50] 檢察官\n「人工節文」", encoding="utf-8")
    asr = tmp_path / "asr.json"
    asr.write_text(json.dumps({"segments": []}), encoding="utf-8")
    manifest = tmp_path / "job.json"
    manifest.write_text(
        json.dumps(
            {
                "video": str(video),
                "transcript": str(transcript),
                "baseline": str(baseline),
                "asr_json": str(asr),
                "require_secondary_asr": require_secondary,
                "output_root": str(tmp_path / "output"),
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_live_start_preflights_full_timeline_and_forces_court_contract(tmp_path: Path) -> None:
    manifest = _fixture_manifest(tmp_path)
    state = tmp_path / "state.json"
    calls = []

    class FakePopen:
        pid = os.getpid()

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    identity = {
        "pid": os.getpid(),
        "pgid": os.getpid(),
        "process_started": "fixture-start",
    }
    original_identity = LIVE._process_identity
    LIVE._process_identity = lambda _pid: dict(identity)
    try:
        result = LIVE.start_live_job(manifest, state_path=state, popen_factory=FakePopen)
    finally:
        LIVE._process_identity = original_identity
    task = json.loads(Path(result["task_path"]).read_text(encoding="utf-8"))

    assert result["status"] == "running"
    assert result["court_mode"] is True
    assert result["preflight"]["timeline_complete"] is True
    assert task["require_secondary_asr"] is True
    assert task["max_visual_reviews"] >= 2000
    assert Path(task["output_docx"]) != Path(task["transcript"])
    assert calls and "--worker" in calls[0][0]
    assert result["process_identity"] == identity
    assert result["deadline_at"] > result["started_at"]


def test_live_start_rejects_relaxed_secondary_asr(tmp_path: Path) -> None:
    manifest = _fixture_manifest(tmp_path, require_secondary=False)

    try:
        LIVE.start_live_job(manifest, state_path=tmp_path / "state.json")
    except ValueError as exc:
        assert "禁止 require_secondary_asr=false" in str(exc)
    else:
        raise AssertionError("formal live mode accepted relaxed secondary ASR")


def test_status_immediately_reconciles_reused_pid_identity(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "running",
                "pid": 4242,
                "process_identity": {
                    "pid": 4242,
                    "pgid": 4242,
                    "process_started": "old-start",
                },
                "started_at": 1,
                "deadline_at": 99999999999,
                "output_dir": str(tmp_path / "output"),
            }
        ),
        encoding="utf-8",
    )
    original_identity = LIVE._process_identity
    LIVE._process_identity = lambda _pid: {
        "pid": 4242,
        "pgid": 4242,
        "process_started": "new-reused-start",
    }
    try:
        result = LIVE.get_live_status(state_path)
    finally:
        LIVE._process_identity = original_identity

    assert result["status"] == "failed"
    assert "PID/PGID/start identity" in result["error"]


def test_cancel_refuses_to_signal_when_identity_no_longer_matches(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "running",
                "pid": 4242,
                "process_identity": {
                    "pid": 4242,
                    "pgid": 4242,
                    "process_started": "old-start",
                },
            }
        ),
        encoding="utf-8",
    )
    original_identity = LIVE._process_identity
    original_killpg = LIVE.os.killpg
    LIVE._process_identity = lambda _pid: {
        "pid": 4242,
        "pgid": 4242,
        "process_started": "other-process",
    }
    LIVE.os.killpg = lambda *_args: (_ for _ in ()).throw(
        AssertionError("must not signal a reused PID")
    )
    try:
        result = LIVE.cancel_live_job(state_path)
    finally:
        LIVE._process_identity = original_identity
        LIVE.os.killpg = original_killpg

    assert result["status"] == "failed"
    assert "cancel refused" in result["error"]
