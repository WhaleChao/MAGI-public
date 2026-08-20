from __future__ import annotations

import json
from pathlib import Path

from skills.obsidian import action


def test_ingest_state_match_supports_legacy_and_strong_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    stat = source.stat()

    assert action._ingest_state_matches_stat(
        {"hash": "abc", "mtime": int(stat.st_mtime)}, stat
    )
    assert action._ingest_state_matches_stat(
        {
            "hash": "abc",
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        },
        stat,
    )
    assert not action._ingest_state_matches_stat(
        {
            "hash": "abc",
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size + 1,
        },
        stat,
    )


def test_save_ingest_state_merges_and_atomically_replaces(
    tmp_path: Path, monkeypatch
) -> None:
    state_path = tmp_path / "obsidian_ingest_state.json"
    state_path.write_text(
        json.dumps({"files": {"old": {"hash": "a"}}}), encoding="utf-8"
    )
    monkeypatch.setattr(action, "INGEST_STATE_PATH", state_path)

    incoming = {"files": {"new": {"hash": "b"}}}
    action._save_ingest_state(incoming)

    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(stored["files"]) == {"old", "new"}
    assert incoming == stored
    assert list(tmp_path.glob("*.tmp")) == []


def test_hydrate_ingest_state_recovers_durable_note_cursor() -> None:
    files_state = {"案件/already.pdf": {"hash": "old"}}
    notes = {
        "aaa": {
            "source_relpath": "new.pdf",
            "note_path": "20_Notes/案件/summary__new.md",
            "mtime": "123",
        },
        "bbb": {
            "source_relpath": "already.pdf",
            "note_path": "20_Notes/案件/summary__already.md",
            "mtime": "456",
        },
        "ccc": {"source_relpath": "missing-mtime.pdf", "mtime": ""},
    }

    recovered = action._hydrate_ingest_state_from_existing_notes(
        "案件", files_state, notes
    )

    assert recovered == 1
    assert files_state["案件/new.pdf"]["hash"] == "aaa"
    assert files_state["案件/new.pdf"]["mtime"] == 123
    assert files_state["案件/already.pdf"] == {"hash": "old"}
