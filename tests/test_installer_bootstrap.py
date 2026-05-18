from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scripts.packaging import build_installers
from scripts.packaging import magi_install_launcher as launcher
from scripts.packaging import runtime_bootstrap as bootstrap
from scripts import public_release_audit


def test_select_runtime_prefers_omlx_on_apple_silicon():
    profile = bootstrap.HardwareProfile(
        os_name="Darwin",
        machine="arm64",
        cpu_brand="Apple M4",
        memory_gb=24,
        free_disk_gb=120,
        is_apple_silicon=True,
    )

    plan = bootstrap.select_runtime_plan(profile)

    assert plan.provider == "omlx"
    assert plan.primary_model == "gemma-4-e4b-it-4bit"
    assert plan.heavy_model == ""
    assert any(item.role == "primary" and item.source.startswith("mlx-community/") for item in plan.downloads)


def test_select_runtime_uses_ollama_on_windows_and_scales_model():
    profile = bootstrap.HardwareProfile(
        os_name="Windows",
        machine="AMD64",
        cpu_brand="x64",
        memory_gb=32,
        free_disk_gb=100,
        is_apple_silicon=False,
    )

    plan = bootstrap.select_runtime_plan(profile)

    assert plan.provider == "ollama"
    assert plan.primary_model == "gemma3:12b"
    assert plan.embedding_model == "nomic-embed-text"


def test_runtime_bootstrap_dry_run_writes_actionable_steps(tmp_path, monkeypatch):
    profile = bootstrap.HardwareProfile(
        os_name="Darwin",
        machine="arm64",
        cpu_brand="Apple M4",
        memory_gb=24,
        free_disk_gb=100,
        is_apple_silicon=True,
    )
    monkeypatch.setattr(bootstrap, "detect_hardware", lambda: profile)
    monkeypatch.setattr(bootstrap, "_which", lambda name: "" if name == "omlx" else f"/usr/bin/{name}")
    out = tmp_path / "runtime.json"

    rc = bootstrap.main(["--repo-dir", str(tmp_path), "--dry-run", "--download-models", "--json", "--output", str(out)])

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["plan"]["provider"] == "omlx"
    assert payload["mode"] == "dry-run"
    assert any(step["key"] == "install_omlx" and step["status"] == "warn" for step in payload["steps"])
    assert any(step["key"].startswith("hf_download:") for step in payload["steps"])


def test_launcher_extract_release_archive_strips_top_level_and_blocks_traversal(tmp_path):
    archive = tmp_path / "MAGI-release.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("MAGI-test/scripts/customer_install_wizard.py", "print('ok')\n")
        zf.writestr("MAGI-test/README.md", "MAGI\n")

    repo = launcher.extract_release_archive(archive, tmp_path / "install", force=True)

    assert repo == (tmp_path / "install").resolve()
    assert (repo / "scripts" / "customer_install_wizard.py").exists()

    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", "no\n")

    try:
        launcher.extract_release_archive(bad, tmp_path / "bad-install", force=True)
    except ValueError as exc:
        assert "unsafe zip member" in str(exc)
    else:
        raise AssertionError("unsafe zip member was accepted")


def test_build_runtime_command_includes_customer_runtime_flags(tmp_path):
    command = launcher.build_runtime_command(
        tmp_path,
        python="python3",
        yes=True,
        dry_run=False,
        allow_system_install=True,
        download_models=True,
        install_services=True,
        provider="ollama",
        include_heavy=False,
        report_path=tmp_path / "runtime.json",
    )

    assert "scripts/packaging/runtime_bootstrap.py" in command[1]
    assert "--provider" in command
    assert "ollama" in command
    assert "--allow-system-install" in command
    assert "--download-models" in command


def test_build_installers_writes_macos_app_and_windows_payload(tmp_path):
    release = tmp_path / "MAGI-release.zip"
    with zipfile.ZipFile(release, "w") as zf:
        zf.writestr("MAGI-test/README.md", "MAGI\n")
        zf.writestr("MAGI-test/scripts/customer_install_wizard.py", "print('ok')\n")
        zf.writestr("MAGI-test/scripts/packaging/runtime_bootstrap.py", "print('runtime')\n")

    args = build_installers.parse_args([
        "--archive",
        str(release),
        "--output-root",
        str(tmp_path / "out"),
        "--force",
        "--no-dmg",
        "--json",
    ])
    payload = build_installers.build_installers(args)

    assert Path(payload["macos"]["app"]).exists()
    assert (Path(payload["macos"]["app"]) / "Contents" / "Resources" / "MAGI-release.zip").exists()
    assert Path(payload["windows"]["zip"]).exists()
    assert (Path(payload["windows"]["folder"]) / "build_windows_exe.ps1").exists()


def test_public_release_audit_scans_unpacked_release_without_git(tmp_path):
    (tmp_path / "README.md").write_text("MAGI\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("MAGI_API_KEY=<<replace_with_key>>\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "customer_install_wizard.py").write_text("print('ok')\n", encoding="utf-8")

    findings = public_release_audit.scan_tracked_files(repo_root=tmp_path)

    assert findings == []
