from scripts.ops.git_stage_guard import is_blocked_path, validate_staged_paths


def test_blocks_codex_and_archive_runtime_paths():
    blocked = [
        ".codex_tmp_iccpr/bilingual_rows.tsv",
        ".openclaw_archived_20260412/dot_openclaw/session.jsonl",
        "Paperclip_rebuild/dist/Paperclip.app/Contents/Info.plist",
        ".runtime/issue_agenda.jsonl",
        "static/exports/report.pdf",
        "foo/client_credentials.json",
    ]

    for path in blocked:
        assert is_blocked_path(path), path


def test_allows_normal_source_paths():
    allowed = [
        "api/startup.py",
        "scripts/ops/git_stage_guard.py",
        "tests/test_git_stage_guard.py",
        "static/translator_ape_latest.json",
    ]

    for path in allowed:
        assert not is_blocked_path(path), path


def test_large_stage_requires_explicit_bypass(monkeypatch):
    monkeypatch.delenv("MAGI_ALLOW_LARGE_COMMIT", raising=False)

    ok, problems = validate_staged_paths([f"api/file_{i}.py" for i in range(251)])

    assert ok is False
    assert any("too many staged paths" in p for p in problems)


def test_large_stage_can_be_bypassed_after_review(monkeypatch):
    monkeypatch.setenv("MAGI_ALLOW_LARGE_COMMIT", "1")

    ok, problems = validate_staged_paths([f"api/file_{i}.py" for i in range(251)])

    assert ok is True
    assert problems == []
