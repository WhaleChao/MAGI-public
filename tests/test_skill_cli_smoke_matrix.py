from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from skills.catalog import iter_top_level_skill_dirs


MAGI_ROOT = Path(__file__).resolve().parents[1]


def _runnable_skill_scripts() -> list[str]:
    scripts: list[str] = []
    for entry in iter_top_level_skill_dirs(MAGI_ROOT / "skills", runnable_only=True):
        scripts.append(str(Path("skills") / entry.name / "action.py"))
    return scripts


def _run_skill_script(
    relative_path: str,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> int:
    monkeypatch.chdir(MAGI_ROOT)
    monkeypatch.setenv("MAGI_ROOT", str(MAGI_ROOT))
    monkeypatch.setenv("MAGI_ROOT_DIR", str(MAGI_ROOT))
    monkeypatch.setenv("MAGI_DISABLE_SERVER_STARTUP_HOOKS", "1")
    monkeypatch.setenv("SYNOLOGY_CASE_ROOT", str(tmp_path))
    monkeypatch.setenv("SYNOLOGY_CASE_ROOTS", str(tmp_path))
    monkeypatch.setenv("TRANSCRIPT_INDEX_DB", str(tmp_path / "transcript_index.json"))

    import api.case_path_mapper as case_path_mapper

    monkeypatch.setattr(
        case_path_mapper,
        "preferred_case_roots",
        lambda include_closed=True, cfg=None: [str(tmp_path)],
    )
    monkeypatch.setattr(
        case_path_mapper,
        "default_case_roots",
        lambda include_closed=True, cfg=None: [str(tmp_path)],
    )

    script_path = MAGI_ROOT / relative_path
    monkeypatch.setattr(sys, "argv", [str(script_path), *argv])
    try:
        runpy.run_path(str(script_path), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.mark.parametrize("relative_path", _runnable_skill_scripts(), ids=lambda p: p.split("/")[-2])
def test_runnable_skill_help_entrypoints_bootstrap(
    relative_path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    exit_code = _run_skill_script(relative_path, ["--help"], monkeypatch, tmp_path)
    captured = capsys.readouterr()
    combined = (captured.out or "") + "\n" + (captured.err or "")
    assert exit_code == 0
    assert "traceback" not in combined.lower()
