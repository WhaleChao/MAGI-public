from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest


MAGI_ROOT = Path(__file__).resolve().parents[1]


def _run_skill_script(
    relative_path: str,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    patch_case_roots: bool = False,
):
    monkeypatch.chdir(MAGI_ROOT)
    monkeypatch.setenv("MAGI_ROOT", str(MAGI_ROOT))
    monkeypatch.setenv("SYNOLOGY_CASE_ROOT", str(tmp_path))
    monkeypatch.setenv("SYNOLOGY_CASE_ROOTS", str(tmp_path))
    monkeypatch.setenv("TRANSCRIPT_INDEX_DB", str(tmp_path / "transcript_index.json"))

    if patch_case_roots:
        import api.case_path_mapper as case_path_mapper

        monkeypatch.setattr(
            case_path_mapper,
            "preferred_case_roots",
            lambda include_closed=True, cfg=None: [str(tmp_path)],
        )

    script_path = MAGI_ROOT / relative_path
    monkeypatch.setattr(sys, "argv", [str(script_path), *argv])
    try:
        runpy.run_path(str(script_path), run_name="__main__")
    except SystemExit as exc:
        return exc.code
    return 0


def test_market_briefing_cli_bootstraps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_skill_script(
        "skills/market-briefing/action.py",
        ["--task", "list"],
        monkeypatch,
        tmp_path,
    )
    assert exit_code == 0
    assert "追蹤" in capsys.readouterr().out


def test_iron_dome_cli_bootstraps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_skill_script(
        "skills/iron-dome/action.py",
        ["--task", "self_test"],
        monkeypatch,
        tmp_path,
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out.lower()


def test_transcript_indexer_help_bootstraps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_skill_script(
        "skills/transcript-indexer/action.py",
        ["--help"],
        monkeypatch,
        tmp_path,
        patch_case_roots=True,
    )
    assert exit_code == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_pdf_annotator_help_bootstraps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_skill_script(
        "skills/pdf-annotator/action.py",
        ["--help"],
        monkeypatch,
        tmp_path,
        patch_case_roots=True,
    )
    assert exit_code == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_pdf_namer_help_bootstraps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_skill_script(
        "skills/pdf-namer/action.py",
        ["--help"],
        monkeypatch,
        tmp_path,
        patch_case_roots=True,
    )
    assert exit_code == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_legal_attest_help_bootstraps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_skill_script(
        "skills/legal_attest/action.py",
        ["--task", "help"],
        monkeypatch,
        tmp_path,
    )
    assert exit_code == 0
    assert "legal_attest" in capsys.readouterr().out


def test_insight_refine_help_bootstraps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_skill_script(
        "skills/insight-refine/action.py",
        ["--help"],
        monkeypatch,
        tmp_path,
    )
    assert exit_code == 0
    assert "usage:" in capsys.readouterr().out.lower()
