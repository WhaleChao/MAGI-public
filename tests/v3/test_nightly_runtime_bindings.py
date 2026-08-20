from __future__ import annotations

import json
from pathlib import Path


def test_system_test_report_uses_mutable_static(
    tmp_path: Path, monkeypatch
) -> None:
    from skills.ops import system_test

    sealed_release = tmp_path / "sealed"
    mutable_static = tmp_path / "mutable-static"
    sealed_release.mkdir()
    mutable_static.mkdir()
    monkeypatch.setattr(system_test, "MAGI_DIR", str(sealed_release))
    monkeypatch.setenv("MAGI_MUTABLE_STATIC_DIR", str(mutable_static))
    monkeypatch.setattr(
        system_test,
        "ALL_TESTS",
        [("unit", "unit", lambda: {"pass": True, "detail": "ok"})],
    )
    sealed_release.chmod(0o555)
    try:
        report = system_test.run_all_tests()
    finally:
        sealed_release.chmod(0o755)

    saved = json.loads(
        (mutable_static / "system_test_report.json").read_text(encoding="utf-8")
    )
    assert report["score"] == "1/1"
    assert saved["score"] == "1/1"
    assert not (sealed_release / "static").exists()


def test_channel_smoke_source_loads_explicit_v3_env() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "ops"
        / "smoke_three_channels.py"
    ).read_text(encoding="utf-8")
    assert 'os.environ.get("MAGI_ENV_FILE", "")' in source
    assert "PROJECT_ROOT / \".env\"" in source
    assert "override=False" in source
