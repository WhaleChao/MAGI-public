from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import public_release_audit as release_audit
from scripts import v3_release_bundle as bundle


ROOT = Path(__file__).resolve().parents[2]
OSC_REQUIRED_PHOTO_ASSETS = (
    "resources/osc/photo/lawyer_stamp.png",
    "resources/osc/photo/logo.png",
    "resources/osc/photo/namecard.png",
)


@pytest.fixture(autouse=True)
def _unlock_sealed_release_directories_after_test(tmp_path: Path):
    yield
    for directory, _directory_names, _file_names in os.walk(
        tmp_path, topdown=False, followlinks=False
    ):
        path = Path(directory)
        if not path.is_symlink():
            path.chmod(0o755)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(source: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _head(source: Path) -> str:
    return _git(source, "rev-parse", "HEAD")


def _privacy_entry(tmp_path: Path, relative: str, content: str) -> tuple[Path, bundle.SourceEntry]:
    source = tmp_path / "privacy-source"
    path = source / relative
    _write(path, content)
    return source, bundle._entry_from_file(source.resolve(), path, Path(relative))


def test_public_audit_distinguishes_mysql_flag_check_from_inline_secret() -> None:
    flag_check = 'if "-' + 'p" not in command or "no:cacheprovider" not in command: pass'
    assert release_audit.scan_text("scripts/guard.py", flag_check) == []

    finding = release_audit.scan_text(
        "scripts/unsafe.sh", "mysql -p'" + "not-a-real-password' database"
    )
    assert [item.kind for item in finding] == ["mysql_cli_password"]


def test_public_audit_allows_only_official_document_number_context() -> None:
    official = (
        '經濟部 97 年經商字第 ' + '097020' + '6942 號函；'
        '財政部台財稅字第\\n ' + '092045' + '5616 號令'
    )
    assert release_audit.scan_text(
        "static/exam_tutor/essay_bank.json", official
    ) == []

    finding = release_audit.scan_text(
        "static/exam_tutor/essay_bank.json",
        official + "；聯絡電話 " + "091234" + "5678",
    )
    assert [item.kind for item in finding] == ["taiwan_mobile"]


def test_office_validation_cli_survives_v3_python_safe_path(tmp_path: Path) -> None:
    writer = ROOT / "skills" / "forensic-transcript-verifier" / "scripts" / "write_transcript_docx.py"
    validator = ROOT / "skills" / "docx" / "scripts" / "office" / "validate.py"
    output = tmp_path / "safe-path.docx"
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "output_path": str(output),
                "title": "候選封裝驗證",
                "case_info": "去識別測試",
                "turns": [{"display": "00:00:01", "speaker": "測試者", "text": "測試內容"}],
                "unresolved": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PYTHONSAFEPATH": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    office = validator.parent
    for cli_name in ("pack.py", "unpack.py"):
        imported = subprocess.run(
            [os.sys.executable, str(office / cli_name), "--help"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert imported.returncode == 0, imported.stderr or imported.stdout
    generated = subprocess.run(
        [os.sys.executable, str(writer), str(task)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    validated = subprocess.run(
        [os.sys.executable, str(validator), str(output)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert validated.returncode == 0, validated.stderr or validated.stdout


def _source_tree(tmp_path: Path, *, initialize_git: bool = True) -> Path:
    source = tmp_path / "source"
    _write(source / "magi_v3" / "__init__.py", "VERSION = '3.0'\n")
    _write(
        source / "magi_v3" / "worker.py",
        "import os\n"
        "from pathlib import Path\n"
        "def run():\n    return 'ok'\n"
        "def first_write():\n"
        "    target = Path(os.environ['MAGI_V3_SHARED_STATE_DIR']) / 'candidate-probe.json'\n"
        "    target.write_text('{\\\"candidate\\\":true}\\n', encoding='utf-8')\n"
        "    return target\n",
    )
    (source / "magi_v3" / "worker.py").chmod(0o600)
    _write(source / "api" / "server.py", "app = object()\n")
    _write(source / "api" / "tools_api.py", "app = object()\n")
    for name in ("01_A.py", "02_B.py", "03_C.py", "04_D.py", "05_E.py", "06_F.py"):
        _write(source / "integrations" / "debt_robot" / name, "def main(): return 0\n")
    _write(source / "src" / "supplement_core" / "__init__.py", "VERSION = 1\n")
    _write(source / "skills" / "ops" / "heartbeat.py", "def main(): return 0\n")
    _write(source / "skills" / "browser" / "browser_control.py", "def main(): return 0\n")
    _write(source / "skills" / "pdf-namer" / "action.py", "def main(): return 0\n")
    _write(source / "skills" / "pdf-namer" / "naming_rules.py", "RULES = {}\n")
    _write(source / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py", "\n")
    _write(source / "gui" / "magi_menubar.py", "def main(): return 0\n")
    _write(
        source / "mobile_app" / "capacitor.config.json",
        json.dumps({"server": {"url": "https://mobile.invalid/mobile-app"}}),
    )
    _write(source / "mobile_app" / "package.json", json.dumps({"name": "magi-mobile"}))
    _write(source / "mobile_app" / "package-lock.json", json.dumps({"lockfileVersion": 3}))
    _write(source / "mobile_app" / "www" / "index.html", "<!doctype html>\n")
    _write(
        source / "mobile_app" / "android" / "app" / "src" / "main" / "assets" / "capacitor.config.json",
        json.dumps({"server": {"url": "https://mobile.invalid/mobile-app"}}),
    )
    _write(
        source / "mobile_app" / "android" / "app" / "src" / "main" / "AndroidManifest.xml",
        "<manifest/>\n",
    )
    _write(source / "scripts" / "runtime_probe.py", "print('ok')\n")
    _write(
        source / "migrations" / "versions" / "003_add_tenant_scope.sql",
        "CREATE TABLE IF NOT EXISTS tenants (id VARCHAR(64) PRIMARY KEY);\n",
    )
    _write(source / "templates" / "index.html", "<!doctype html>\n")
    _write(source / "static" / "app.js", "console.log('ok');\n")
    _write(source / "config" / "launchagents" / "com.magi.test.plist", "<plist/>\n")
    _write(source / "tests" / "v3" / "test_release_smoke.py", "def test_release_smoke(): pass\n")
    _write(source / "tests" / "conftest.py", "# release test bootstrap\n")
    _write(source / "tests" / "support" / "__init__.py", "\n")
    _write(source / "tests" / "support" / "side_effect_guard.py", "def install(monkeypatch): pass\n")
    _write(source / "tests" / "test_input_method_watchdog.py", "def test_watchdog(): pass\n")
    for relative in bundle.CONFIG_FILES:
        _write(source / relative, json.dumps({"schema_version": 1, "name": relative}))
    _write(
        source / "docs" / "architecture" / "v3" / "contracts" / "job-envelope.schema.json",
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema"}),
    )
    _write(
        source / "docs" / "architecture" / "v3" / "generated" / "v2_runtime_routes.json",
        json.dumps({"schema_version": 1}),
    )
    _write(
        source / "docs" / "architecture" / "v3" / "OWNSCRIBE_EVALUATION.json",
        json.dumps({"schema_version": 1, "status": "blocked_candidate"}),
    )
    _write(source / "daemon.py", "# import compatibility sentinel; never executed by V3\n")
    _write(source / "requirements.txt", "flask\n")
    _write(source / "requirements-optional.txt", "\n")
    _write(
        source / "requirements-melchior-launch-intent.txt",
        "cryptography==50.0.0 --hash=sha256:" + "0" * 64 + "\n",
    )
    _write(source / "pyproject.toml", "[project]\nname='magi-v3-test'\n")
    for name in ("datastores", "holidays_config", "models", "nodes", "services"):
        _write(source / "json" / f"{name}.json", "{}\n")
    _write(source / "bin" / "magi-v3-python", "#!/bin/sh\nexec \"$@\"\n")
    (source / "bin" / "magi-v3-python").chmod(0o755)
    _write(source / "magi_v3" / ".env", "SECRET=never-copy\n")
    _write(source / "magi_v3" / "cache" / "cached.bin", "never-copy\n")
    _write(source / "magi_v3" / "browser" / "profile.json", "never-copy\n")
    _write(source / "magi_v3" / "queue" / "pending.json", "never-copy\n")
    _write(source / "magi_v3" / "logs" / "runtime.log", "never-copy\n")
    _write(source / "magi_v3" / "__pycache__" / "worker.pyc", "never-copy\n")
    _write(source / "api" / ".DS_Store", "never-copy\n")
    _write(source / "skills" / "review_cache.json", "{\"mutable\": true}\n")
    for name in bundle.PDF_NAMER_MUTABLE_FILES:
        _write(source / "skills" / "pdf-namer" / name, "{\"private\": true}\n")
    for relative in bundle.DEBT_ADDRESS_MUTABLE_FILES:
        content = "name,address\nSynthetic Bank,Synthetic Address\n" if relative.endswith(".csv") else "{\"items\": []}\n"
        _write(source / relative, content)
    _write(source / "static" / "exports" / "result.txt", "never-copy\n")
    _write(source / "static" / "integration_smoke_latest.md", "never-copy\n")
    # Keep the synthetic repository aligned with the production bundle's
    # required compatibility tests without duplicating that list here.
    for relative in bundle.REQUIRED_FILES:
        required = source / relative
        if not required.exists():
            _write(required, "def test_required_release_compatibility(): pass\n")
    if initialize_git:
        _git(source, "init", "--quiet")
        _git(source, "config", "user.name", "Release Test")
        _git(source, "config", "user.email", "release-test@example.invalid")
        # Production intentionally tracks the safe template even though the
        # generic ``.env*`` ignore rule protects real credential files.  Mirror
        # that exact contract in the synthetic repository.
        _git(source, "add", "--all")
        _git(source, "add", "-f", ".env.example")
        _git(source, "commit", "--quiet", "-m", "initial release source")
    return source


def test_bundle_contains_only_allowlisted_verified_files_and_atomic_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path)
    staging = tmp_path / "staging" / "release-1"
    staging.parent.mkdir()
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def tracked_replace(source_path: str | os.PathLike[str], target_path: str | os.PathLike[str]) -> None:
        replace_calls.append((Path(source_path), Path(target_path)))
        real_replace(source_path, target_path)

    monkeypatch.setattr(bundle.os, "replace", tracked_replace)
    marker = bundle.build_release_bundle(
        source,
        staging,
        release_id="v3-test-1",
    )

    manifest_path = staging / bundle.MANIFEST_NAME
    marker_path = staging / bundle.COMPLETION_MARKER
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    persisted_marker = json.loads(marker_path.read_text(encoding="utf-8"))
    paths = {item["path"] for item in manifest["files"]}

    assert marker == persisted_marker
    assert manifest["release_id"] == "v3-test-1"
    assert manifest["commit"] == _head(source)
    assert manifest["immutable"] is True
    assert set(bundle.CONFIG_FILES) <= paths
    assert "magi_v3/__init__.py" in paths
    assert "magi_v3/worker.py" in paths
    assert (staging / "magi_v3" / "worker.py").stat().st_mode & 0o777 == 0o444
    assert "api/server.py" in paths
    assert "integrations/debt_robot/01_A.py" in paths
    assert "integrations/debt_robot/06_F.py" in paths
    assert "src/supplement_core/__init__.py" in paths
    assert "data/templates/D_supplement.docx" in paths
    for name in ("A.docx", "B.docx", "C.docx", "D.docx"):
        assert f"integrations/debt_robot/document/{name}" in paths
    assert not (bundle.DEBT_ADDRESS_MUTABLE_FILES & paths)
    assert "skills/ops/heartbeat.py" in paths
    assert "casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py" in paths
    assert "gui/magi_menubar.py" in paths
    assert "scripts/runtime_probe.py" in paths
    assert "migrations/versions/003_add_tenant_scope.sql" in paths
    assert "templates/index.html" in paths
    assert "static/app.js" in paths
    assert "tests/v3/test_release_smoke.py" in paths
    assert "tests/__init__.py" in paths
    assert "tests/conftest.py" in paths
    assert "tests/support/side_effect_guard.py" in paths
    assert "tests/test_input_method_watchdog.py" in paths
    assert "tests/test_saas_readiness_migration.py" in paths
    assert "tests/test_selfhost_release_smoke.py" in paths
    assert "daemon.py" in paths
    assert "osc.py" in paths
    assert "bin/magi-v3-python" in paths
    assert ".env.example" in paths
    assert "requirements-selfhost.txt" in paths
    assert "requirements-melchior-launch-intent.txt" in paths
    assert hashlib.sha256(
        (staging / "requirements-melchior-launch-intent.txt").read_bytes()
    ).hexdigest() == hashlib.sha256(
        (source / "requirements-melchior-launch-intent.txt").read_bytes()
    ).hexdigest()
    assert "install-magi.cmd" in paths
    assert "install-magi.command" in paths
    assert "install-magi.ps1" in paths
    assert "config/selfhost.example.json" in paths
    assert "config/selfhost.schema.json" in paths
    assert "docs/SELFHOST_DEPLOYMENT.md" in paths
    assert "json/models.json" in paths
    manifest_files = {item["path"]: item for item in manifest["files"]}
    for relative in OSC_REQUIRED_PHOTO_ASSETS:
        assert relative in paths
        assert manifest_files[relative]["sha256"] == hashlib.sha256(
            (source / relative).read_bytes()
        ).hexdigest()
        assert hashlib.sha256((staging / relative).read_bytes()).hexdigest() == (
            manifest_files[relative]["sha256"]
        )
    assert "docs/architecture/v3/contracts/job-envelope.schema.json" in paths
    assert "docs/architecture/v3/generated/v2_runtime_routes.json" in paths
    assert "docs/architecture/v3/OWNSCRIBE_EVALUATION.json" in paths
    assert not any(
        excluded in path.split("/")
        for path in paths
        for excluded in ("cache", "queue", "logs", "__pycache__")
    )
    assert not any(path.endswith((".env", ".log", ".pyc")) for path in paths)
    assert not any(path.endswith(".DS_Store") for path in paths)
    assert "skills/review_cache.json" not in paths
    assert "skills/pdf-namer/action.py" in paths
    assert "skills/pdf-namer/naming_rules.py" in paths
    assert "skills/browser/browser_control.py" in paths
    assert "config/launchagents/com.magi.test.plist" in paths
    assert not ({f"skills/pdf-namer/{name}" for name in bundle.PDF_NAMER_MUTABLE_FILES} & paths)
    assert "static/exports/result.txt" not in paths
    assert "static/integration_smoke_latest.md" not in paths
    assert (staging / "bin" / "magi-v3-python").stat().st_mode & 0o777 == 0o555
    assert manifest_path.stat().st_mode & 0o777 == 0o444
    assert marker_path.stat().st_mode & 0o777 == 0o444
    for directory, directory_names, _file_names in os.walk(staging):
        assert Path(directory).stat().st_mode & 0o777 == 0o555
        assert all(not (Path(directory) / name).is_symlink() for name in directory_names)
    assert manifest["source_file_count"] == len(paths) == len(manifest["files"])
    assert manifest["release_sha256"] == manifest["source_snapshot_sha256"]
    assert manifest["git_provenance"]["head"] == _head(source)
    assert manifest["git_provenance"]["dirty"] is False
    for item in manifest["files"]:
        copied = staging / item["path"]
        assert copied.is_file() and not copied.is_symlink()
        assert copied.stat().st_size == item["size"]
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == item["sha256"]
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert persisted_marker["manifest_sha256"] == manifest_digest
    assert persisted_marker["source_snapshot_sha256"] == manifest["source_snapshot_sha256"]
    assert replace_calls == [
        (
            staging / f".{bundle.COMPLETION_MARKER}.tmp-{os.getpid()}",
            marker_path,
        )
    ]
    assert not list(staging.glob(f".{bundle.COMPLETION_MARKER}.tmp-*"))

    runtime = tmp_path / "candidate-runtime"
    runtime.mkdir()
    code = (
        "import json,magi_v3.worker as worker;"
        "path=worker.first_write();"
        "print(json.dumps({'origin':worker.__file__,'write':str(path)}))"
    )
    candidate_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(staging),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "MAGI_V3_SHARED_STATE_DIR": str(runtime),
    }
    candidate = subprocess.run(
        ["/usr/bin/python3", "-S", "-c", code],
        cwd=tmp_path,
        env=candidate_env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    observed = json.loads(candidate.stdout)
    assert Path(observed["origin"]).resolve().is_relative_to(staging.resolve())
    assert Path(observed["write"]) == runtime / "candidate-probe.json"
    assert json.loads((runtime / "candidate-probe.json").read_text()) == {"candidate": True}
    assert not list(staging.rglob("__pycache__"))


@pytest.mark.parametrize(
    "hidden_path",
    [
        "skills/browser/browser_control.py",
        "config/launchagents/com.magi.test.plist",
    ],
)
def test_tracked_functional_source_cannot_be_silently_excluded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hidden_path: str,
) -> None:
    source = _source_tree(tmp_path)
    monkeypatch.setattr(
        bundle,
        "LOCAL_ONLY_RELEASE_FILES",
        bundle.LOCAL_ONLY_RELEASE_FILES | {hidden_path},
    )

    with pytest.raises(bundle.ReleaseBundleError, match="excludes tracked functional source"):
        bundle.build_release_bundle(source, tmp_path / "release", release_id="v3-code-loss")


def test_exam_tutor_official_bank_and_source_pdfs_are_immutable_release_assets() -> None:
    assert bundle._excluded(Path("static/exam_tutor/choice_bank.json")) is False
    assert bundle._excluded(Path("static/exam_tutor/essay_bank.json")) is False
    assert bundle._excluded(Path("static/exam_tutor/curated_practice_weights.json")) is False
    assert bundle._excluded(Path("static/exam_tutor/extended_source_catalog.json")) is False
    assert bundle._excluded(Path("static/exam_tutor/trend_analysis.json")) is False
    assert bundle._excluded(Path("static/exam_tutor/source-pdfs/114-question.pdf")) is False
    assert bundle._excluded(Path("static/exam_tutor/source-pdfs/114-answer.pdf")) is False
    assert bundle._excluded(Path("static/exam_tutor/essay-source-pdfs/114-essay-question.pdf")) is False
    assert bundle._excluded(Path("static/exam_tutor/essay-source-pdfs/114-official-rubric.pdf")) is False
    assert bundle._excluded(Path("static/exam_tutor/notes.txt")) is True
    assert bundle._excluded(Path("static/unrelated-data.json")) is True


def test_video_autopilot_minimal_runtime_is_a_sealed_release_surface() -> None:
    assert "tests/test_video_studio_blueprint.py" in bundle.REQUIRED_TEST_TARGETS
    assert "third_party/video_autopilot_kit/LICENSE" in bundle.REQUIRED_PACKAGE_FILES
    assert (
        "third_party/video_autopilot_kit/MAGI_INTEGRATION.json"
        in bundle.REQUIRED_PACKAGE_FILES
    )
    assert (
        "third_party/video_autopilot_kit/runtime/portrait_normalizer.py"
        in bundle.REQUIRED_PACKAGE_FILES
    )
    assert bundle._excluded(Path("third_party/video_autopilot_kit/LICENSE")) is False
    assert bundle._excluded(
        Path("third_party/video_autopilot_kit/MAGI_INTEGRATION.json")
    ) is False
    assert bundle._excluded(
        Path("third_party/video_autopilot_kit/runtime/portrait_normalizer.py")
    ) is False


def test_release_quality_targets_are_part_of_the_bundle_allowlist() -> None:
    suites = json.loads(
        (ROOT / "config/v3_release_quality_suites.json").read_text(encoding="utf-8")
    )
    targets = {
        target
        for rows in suites["v3_suites"].values()
        for target in rows
    }

    assert all(
        target.startswith("tests/v3/") or target in bundle.REQUIRED_TEST_TARGETS
        for target in targets
    )


def test_active_test_matrix_targets_are_part_of_the_bundle_allowlist() -> None:
    matrix = json.loads(
        (ROOT / "config/test_matrix.json").read_text(encoding="utf-8")
    )

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)

    targets = {value for value in strings(matrix) if value.startswith("tests/")}
    assert targets
    assert all(
        target.startswith("tests/v3/")
        or target.startswith("tests/support/")
        or target in bundle.REQUIRED_TEST_TARGETS
        for target in targets
    )


def test_agent_gateway_stdio_launcher_is_a_sealed_release_surface() -> None:
    assert "bin/agent_mcp.py" in bundle.REQUIRED_PACKAGE_FILES
    assert bundle._excluded(Path("bin/agent_mcp.py")) is False


@pytest.mark.parametrize(
    "literal",
    [
        '"/Users/private-user/case"',
        '"/Volumes/private-share/case"',
        'r"C:\\Users\\private-user\\case"',
        'r"\\\\private-server\\private-share\\case"',
    ],
)
def test_privacy_audit_rejects_private_posix_windows_and_unc_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    relative = "api/privacy_probe.py"
    source, entry = _privacy_entry(tmp_path, relative, f"VALUE = {literal}\n")
    monkeypatch.setattr(
        bundle,
        "AUDITED_V2_ABSOLUTE_PATH_FILES",
        bundle.AUDITED_V2_ABSOLUTE_PATH_FILES | {relative},
    )

    with pytest.raises(bundle.ReleaseBundleError, match="private workstation path"):
        bundle._release_privacy_audit(source, (entry,))


def test_privacy_audit_does_not_exempt_private_paths_in_cutover_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = "config/v3_cutover_gates.json"
    source, entry = _privacy_entry(
        tmp_path,
        relative,
        '{"v2_root":"/Users/private-user/Desktop/MAGI_v2"}\n',
    )
    monkeypatch.setattr(
        bundle,
        "AUDITED_V2_ABSOLUTE_PATH_FILES",
        bundle.AUDITED_V2_ABSOLUTE_PATH_FILES | {relative},
    )

    with pytest.raises(bundle.ReleaseBundleError, match="private workstation path"):
        bundle._release_privacy_audit(source, (entry,))


def test_whale_tailnet_installer_has_no_complete_workstation_path_literal() -> None:
    relative = Path(
        "scripts/melchior_federation/windows_tailnet_serve_installer.py"
    )
    entry = bundle._entry_from_file(ROOT, ROOT / relative, relative)

    evidence = bundle._release_privacy_audit(ROOT, (entry,))

    assert evidence["raw_hits"] == 0
    assert evidence["reviewed_non_sensitive_compat_hits"] == 0


def test_current_release_source_passes_privacy_literal_inventory() -> None:
    evidence = bundle._release_privacy_audit(ROOT, bundle.snapshot_sources(ROOT))

    assert evidence["status"] == "passed"
    assert evidence["raw_hits"] >= evidence["synthetic_fixture_hits"]


def test_privacy_evidence_splits_synthetic_and_reviewed_hits_without_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "privacy-source"
    reviewed_path = "api/reviewed_probe.py"
    synthetic_path = "tests/v3/synthetic_probe.py"
    _write(source / reviewed_path, 'VALUE = r"C:\\Program Files\\Tool\\tool.exe"\n')
    _write(source / synthetic_path, 'VALUE = r"C:\\Users\\Public\\fixture.txt"\n')
    entries = tuple(
        bundle._entry_from_file(source.resolve(), source / relative, Path(relative))
        for relative in (reviewed_path, synthetic_path)
    )
    monkeypatch.setattr(
        bundle,
        "AUDITED_V2_ABSOLUTE_PATH_FILES",
        bundle.AUDITED_V2_ABSOLUTE_PATH_FILES | {reviewed_path},
    )
    monkeypatch.setattr(
        bundle,
        "SYNTHETIC_ABSOLUTE_PATH_FILES",
        bundle.SYNTHETIC_ABSOLUTE_PATH_FILES | {synthetic_path},
    )

    evidence = bundle._release_privacy_audit(source, entries)

    assert evidence["raw_hits"] == 2
    assert evidence["reviewed_non_sensitive_compat_hits"] == 1
    assert evidence["synthetic_fixture_hits"] == 1
    assert evidence["violations"] == 0
    assert evidence["content_in_evidence"] is False
    serialized = json.dumps(evidence, sort_keys=True)
    assert "private" not in serialized
    assert "Program Files" not in serialized


def test_symlink_escape_fails_closed_before_staging_is_created(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    outside = tmp_path / "outside-secret.py"
    outside.write_text("SECRET = True\n", encoding="utf-8")
    (source / "magi_v3" / "escape.py").symlink_to(outside)
    staging = tmp_path / "release"

    with pytest.raises(bundle.ReleaseBundleError, match="symlinks are forbidden"):
        bundle.build_release_bundle(
            source,
            staging,
            release_id="v3-symlink-test",
            commit=_head(source),
        )

    assert not staging.exists()


def test_missing_required_file_and_secret_key_fail_before_staging(tmp_path: Path) -> None:
    source = _source_tree(tmp_path / "missing")
    (source / "bin" / "magi-v3-python").unlink()
    staging = tmp_path / "missing-release"
    with pytest.raises(bundle.ReleaseBundleError, match="required source file is missing"):
        bundle.build_release_bundle(source, staging, release_id="v3-missing", commit=_head(source))
    assert not staging.exists()

    source = _source_tree(tmp_path / "secret")
    _write(source / "api" / "production.pem", "not-a-real-secret\n")
    staging = tmp_path / "secret-release"
    with pytest.raises(bundle.ReleaseBundleError, match="secret-bearing file type"):
        bundle.build_release_bundle(source, staging, release_id="v3-secret", commit=_head(source))
    assert not staging.exists()


@pytest.mark.parametrize("relative", OSC_REQUIRED_PHOTO_ASSETS)
def test_missing_required_osc_photo_asset_fails_before_staging(
    tmp_path: Path,
    relative: str,
) -> None:
    source = _source_tree(tmp_path / relative.replace("/", "-"))
    (source / relative).unlink()
    staging = tmp_path / f"missing-{Path(relative).stem}"

    with pytest.raises(bundle.ReleaseBundleError, match="required source file is missing"):
        bundle.build_release_bundle(
            source,
            staging,
            release_id=f"v3-missing-{Path(relative).stem}",
            commit=_head(source),
        )

    assert not staging.exists()


def test_source_change_during_copy_never_gets_completion_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path)
    staging = tmp_path / "release"
    real_copy = bundle._copy_entry
    changed = False

    def mutate_after_copy(source_root: Path, staging_root: Path, entry: bundle.SourceEntry) -> None:
        nonlocal changed
        real_copy(source_root, staging_root, entry)
        if not changed:
            changed = True
            target = source_root / "magi_v3" / "worker.py"
            target.write_text("def run():\n    return 'changed'\n", encoding="utf-8")

    monkeypatch.setattr(bundle, "_copy_entry", mutate_after_copy)
    with pytest.raises(bundle.ReleaseBundleError, match="source changed"):
        bundle.build_release_bundle(
            source,
            staging,
            release_id="v3-changing-source",
            commit=_head(source),
        )

    assert staging.is_dir()
    assert not (staging / bundle.COMPLETION_MARKER).exists()


def test_staging_rejects_live_runtime_application_support_and_existing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path)
    runtime = tmp_path / "live-runtime"
    runtime.mkdir()
    monkeypatch.setenv("MAGI_V3_STATE_DIR", str(runtime))
    with pytest.raises(bundle.ReleaseBundleError, match="live runtime"):
        bundle.build_release_bundle(
            source,
            runtime / "release",
            release_id="v3-live-runtime",
            commit=_head(source),
        )

    application_support = tmp_path / "Application Support"
    application_support.mkdir()
    monkeypatch.delenv("MAGI_V3_STATE_DIR")
    monkeypatch.setattr(bundle, "_application_support_root", lambda: application_support)
    with pytest.raises(bundle.ReleaseBundleError, match="Application Support"):
        bundle.build_release_bundle(
            source,
            application_support / "release",
            release_id="v3-app-support",
            commit=_head(source),
        )

    existing = tmp_path / "already-exists"
    existing.mkdir()
    with pytest.raises(bundle.ReleaseBundleError, match="must not already exist"):
        bundle.build_release_bundle(
            source,
            existing,
            release_id="v3-existing",
            commit=_head(source),
        )


def test_release_identity_is_strictly_validated(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    with pytest.raises(bundle.ReleaseBundleError, match="release_id"):
        bundle.build_release_bundle(
            source,
            tmp_path / "release",
            release_id="../escape",
            commit=_head(source),
        )
    with pytest.raises(bundle.ReleaseBundleError, match="commit"):
        bundle.build_release_bundle(
            source,
            tmp_path / "release",
            release_id="valid-release",
            commit="not-a-commit",
        )


def test_non_repository_and_old_head_fail_before_staging_creation(tmp_path: Path) -> None:
    non_repository = _source_tree(tmp_path / "non-repository", initialize_git=False)
    non_repo_staging = tmp_path / "non-repo-release"
    with pytest.raises(bundle.ReleaseBundleError, match="git"):
        bundle.build_release_bundle(
            non_repository,
            non_repo_staging,
            release_id="v3-no-git",
        )
    assert not non_repo_staging.exists()

    source = _source_tree(tmp_path / "old-head")
    old_head = _head(source)
    _write(source / "magi_v3" / "worker.py", "def run():\n    return 'new commit'\n")
    _git(source, "add", "magi_v3/worker.py")
    _git(source, "commit", "--quiet", "-m", "new head")
    staging = tmp_path / "old-head-release"
    with pytest.raises(bundle.ReleaseBundleError, match="exactly match git HEAD"):
        bundle.build_release_bundle(
            source,
            staging,
            release_id="v3-old-head",
            commit=old_head,
        )
    assert not staging.exists()


@pytest.mark.parametrize("change", ["modified", "staged", "untracked", "ignored"])
def test_dirty_staged_untracked_or_ignored_allowlist_is_rejected(
    tmp_path: Path,
    change: str,
) -> None:
    source = _source_tree(tmp_path)
    worker = source / "magi_v3" / "worker.py"
    if change == "modified":
        worker.write_text("def run():\n    return 'modified'\n", encoding="utf-8")
    elif change == "staged":
        worker.write_text("def run():\n    return 'staged'\n", encoding="utf-8")
        _git(source, "add", "magi_v3/worker.py")
    elif change == "untracked":
        _write(source / "magi_v3" / "untracked.py", "UNTRACKED = True\n")
    else:
        exclude = source / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "\nmagi_v3/ignored.py\n")
        _write(source / "magi_v3" / "ignored.py", "IGNORED = True\n")

    staging = tmp_path / "release"
    with pytest.raises(bundle.ReleaseBundleError, match="allowlist"):
        bundle.build_release_bundle(
            source,
            staging,
            release_id=f"v3-dirty-{change}",
            commit=_head(source),
        )
    assert not staging.exists()


def test_expected_snapshot_sha256_accepts_exact_content_and_rejects_mismatch(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    expected = bundle._snapshot_digest(bundle.snapshot_sources(source))
    staging = tmp_path / "matching-release"
    bundle.build_release_bundle(
        source,
        staging,
        release_id="v3-content-bound",
        expected_snapshot_sha256=expected,
    )
    assert json.loads((staging / bundle.MANIFEST_NAME).read_text())["release_sha256"] == expected

    rejected = tmp_path / "mismatched-release"
    with pytest.raises(bundle.ReleaseBundleError, match="does not match"):
        bundle.build_release_bundle(
            source,
            rejected,
            release_id="v3-content-mismatch",
            expected_snapshot_sha256="0" * 64,
        )
    assert not rejected.exists()
