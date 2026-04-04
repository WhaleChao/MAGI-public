from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
import sys

from bin import check as check_py
from bin.release_bundle import build_release_bundle, sanitize_json_payload


def test_sanitize_json_payload_redacts_secrets_and_paths():
    payload = {
        "laf": {
            "username": "lawyer@example.com",
            "password": "super-secret",
            "download_folder": "/Users/example/Downloads/laf",
            "base_url": "https://lawyer.laf.org.tw",
        },
        "discord_bot_token": "abc123",
        "case_folder_base_path": "/Users/example/Cases",
    }

    sanitized = sanitize_json_payload(payload)

    assert sanitized["laf"]["username"] == "<USER>"
    assert sanitized["laf"]["password"] == "<REDACTED>"
    assert sanitized["laf"]["download_folder"] == "<PATH>"
    assert sanitized["discord_bot_token"] == "<REDACTED>"
    assert sanitized["case_folder_base_path"] == "<PATH>"
    assert sanitized["laf"]["base_url"] == "https://lawyer.laf.org.tw"


def test_build_release_bundle_excludes_sensitive_and_runtime_files(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "dist"

    (source / "api").mkdir(parents=True)
    (source / "bin").mkdir()
    (source / "json").mkdir()
    (source / "skills" / "pdf-namer").mkdir(parents=True)
    (source / "static" / "star-office").mkdir(parents=True)
    (source / "static" / "exports").mkdir(parents=True)
    (source / "casper_ecosystem" / "law_firm_orchestrators").mkdir(parents=True)

    (source / "README.md").write_text("MAGI\n", encoding="utf-8")
    (source / "LICENSE").write_text("license\n", encoding="utf-8")
    (source / ".env.example").write_text("MAGI_ROLE=CASPER\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        "[project]\nname='magi'\nversion='1.2.3'\n",
        encoding="utf-8",
    )
    (source / "api" / "server.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "bin" / "check").write_text("#!/bin/bash\n", encoding="utf-8")
    (source / "json" / "holidays_config.json").write_text('{"2026": {}}\n', encoding="utf-8")
    (source / "json" / "config.json").write_text(
        json.dumps(
            {
                "laf": {"password": "secret", "download_folder": "/Users/example/Downloads"},
                "discord_bot_token": "abc123",
            }
        ),
        encoding="utf-8",
    )
    (source / "json" / "token.pickle").write_bytes(b"secret")
    (source / "skills" / "pdf-namer" / "training_data.json").write_text("[]\n", encoding="utf-8")
    (source / "static" / "star-office" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (source / "static" / "exports" / "report.txt").write_text("secret\n", encoding="utf-8")
    (source / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py").write_text(
        "print('laf')\n",
        encoding="utf-8",
    )
    (source / "casper_ecosystem" / "law_firm_orchestrators" / "debug_click_before_1.png").write_bytes(b"x")

    result = build_release_bundle(source, output, bundle_name="MAGI-test", force=True)

    bundle = result.bundle_dir
    assert (bundle / "README.md").exists()
    assert (bundle / "api" / "server.py").exists()
    assert (bundle / "json" / "holidays_config.json").exists()
    assert (bundle / "json" / "config.example.json").exists()
    assert not (bundle / "json" / "token.pickle").exists()
    assert not (bundle / "skills" / "pdf-namer" / "training_data.json").exists()
    assert not (bundle / "static" / "star-office").exists()
    assert (bundle / "static" / "exports").is_dir()
    assert not any((bundle / "static" / "exports").iterdir())
    assert (bundle / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py").exists()
    assert not (bundle / "casper_ecosystem" / "law_firm_orchestrators" / "debug_click_before_1.png").exists()
    assert (bundle / "RELEASE_MANIFEST.json").exists()
    assert result.archive_path.exists()

    sanitized = json.loads((bundle / "json" / "config.example.json").read_text(encoding="utf-8"))
    assert sanitized["laf"]["password"] == "<REDACTED>"
    assert sanitized["laf"]["download_folder"] == "<PATH>"
    assert sanitized["discord_bot_token"] == "<REDACTED>"


def test_python_fallback_treats_uninitialized_bundle_as_warning(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env.example").write_text("MAGI_ROLE=CASPER\n", encoding="utf-8")

    monkeypatch.setattr(check_py, "resolve_python", lambda root: Path(sys.executable))
    monkeypatch.setattr(check_py, "_check_service", lambda port: "warn")

    def fake_run_python(root: Path, code: str) -> CompletedProcess[str]:
        if "validate_config" in code:
            return CompletedProcess(["python"], 0, "OK\n", "")
        if "mysql.connector" in code:
            return CompletedProcess(["python"], 0, "7\n", "")
        return CompletedProcess(
            ["python"],
            0,
            "api.runtime_paths\napi.product_runtime\nskills.ops.config\n",
            "",
        )

    monkeypatch.setattr(check_py, "_run_python", fake_run_python)

    rc = check_py._python_fallback(tmp_path)
    out = capsys.readouterr().out

    assert rc == 0
    assert "WARN No venv found (using system python:" in out
    assert "WARN .env missing (copy from .env.example during setup)" in out
