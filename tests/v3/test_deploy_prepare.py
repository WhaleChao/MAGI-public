from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import docx
import pytest
from packaging.utils import canonicalize_name

from scripts import v3_deploy_prepare as deploy
from scripts import v3_static_external_staging as static_external


ROOT = Path(__file__).resolve().parents[2]
REAL_DOCX_RUNTIME_PROBE = deploy._probe_python_docx_runtime
SEATBELT_ALLOW_DEFAULT_PROFILE = "(version 1)\n(allow default)\n"


def _probe_seatbelt_capability(
    sandbox_exec: Path = Path("/usr/bin/sandbox-exec"),
    *,
    runner=None,
) -> tuple[bool, str]:
    """Require a working allow-default Seatbelt, not merely a Darwin host."""
    if not sandbox_exec.is_file() or not os.access(sandbox_exec, os.X_OK):
        return False, "Seatbelt capability unavailable: sandbox-exec is not executable"
    execute = runner or subprocess.run
    command = [
        str(sandbox_exec),
        "-p",
        SEATBELT_ALLOW_DEFAULT_PROFILE,
        "--",
        "/usr/bin/true",
    ]
    try:
        result = execute(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Seatbelt capability probe failed: {type(exc).__name__}"
    if result.returncode != 0:
        return (
            False,
            "Seatbelt capability probe failed: allow-default /usr/bin/true "
            f"returned rc={result.returncode}",
        )
    return True, "Seatbelt allow-default /usr/bin/true probe passed"


@pytest.fixture
def seatbelt_capable() -> None:
    available, reason = _probe_seatbelt_capability()
    if not available:
        pytest.skip(reason)


@pytest.mark.parametrize("returncode", [0, 71])
def test_seatbelt_capability_probe_requires_allow_default_true_success(
    tmp_path: Path,
    returncode: int,
) -> None:
    sandbox_exec = tmp_path / "sandbox-exec"
    sandbox_exec.write_text("#!/bin/sh\n", encoding="utf-8")
    sandbox_exec.chmod(0o700)
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return type("Result", (), {"returncode": returncode})()

    available, reason = _probe_seatbelt_capability(
        sandbox_exec,
        runner=fake_run,
    )

    assert available is (returncode == 0)
    assert observed["command"] == [
        str(sandbox_exec),
        "-p",
        SEATBELT_ALLOW_DEFAULT_PROFILE,
        "--",
        "/usr/bin/true",
    ]
    assert observed["kwargs"] == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "check": False,
        "timeout": 5,
    }
    if returncode == 0:
        assert reason == "Seatbelt allow-default /usr/bin/true probe passed"
    else:
        assert reason.endswith("returned rc=71")


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _remove_test_tree(path: Path) -> None:
    if not path.exists():
        return
    for directory, _directory_names, _file_names in os.walk(path):
        Path(directory).chmod(0o755)
    shutil.rmtree(path)


def _seal_test_release(path: Path) -> None:
    for directory, _directory_names, file_names in os.walk(
        path,
        topdown=False,
        followlinks=False,
    ):
        base = Path(directory)
        for name in file_names:
            member = base / name
            member.chmod(0o555 if member.stat().st_mode & 0o111 else 0o444)
        base.chmod(0o555)


def _fake_venv(root: Path) -> Path:
    python = root / "bin" / "python"
    _write(python, "#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    _write(
        root / "pyvenv.cfg",
        "home = " + str(python.parent) + "\n"
        "include-system-site-packages = false\n"
        "executable = " + str(python) + "\n",
    )
    _write(root / "lib" / "python3.14" / "site-packages" / "example.py", "VALUE = 1\n")
    return python


def _stub_docx_preflight() -> dict[str, object]:
    evidence: dict[str, object] = {
        "module": "docx",
        "distribution": "python-docx",
        "minimum_version": "1.0",
        "version": "1.2.0",
        "module_sha256": "d" * 64,
        "distribution_metadata_sha256": "e" * 64,
        "distribution_record_sha256": "f" * 64,
        "runtime_tree_sha256": "c" * 64,
        "distribution_unambiguous": True,
        "distribution_metadata_manifest_bound": True,
        "distribution_record_manifest_bound": True,
        "distribution_module_owned": True,
        "distribution_module_record_bound": True,
        "distribution_version_matches_module": True,
        "import_succeeded": True,
        "module_manifest_bound": True,
        "roundtrip_succeeded": True,
    }
    evidence["evidence_sha256"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return evidence


def _python_docx_distribution_for_origin(
    origin: Path,
) -> importlib.metadata.Distribution:
    canonical_origin = origin.resolve(strict=True)
    owners: list[importlib.metadata.Distribution] = []
    for distribution in importlib.metadata.distributions():
        if canonicalize_name(str(distribution.metadata.get("Name", ""))) != "python-docx":
            continue
        owned_entries = []
        for entry in tuple(distribution.files or ()):
            try:
                located = Path(distribution.locate_file(entry)).resolve(strict=True)
            except OSError:
                continue
            if located == canonical_origin:
                owned_entries.append(entry)
        if len(owned_entries) > 1:
            raise AssertionError(
                "python-docx distribution ambiguously owns the selected module origin"
            )
        if len(owned_entries) == 1:
            owners.append(distribution)
    if len(owners) != 1:
        raise AssertionError(
            "selected docx module origin must have exactly one python-docx distribution owner"
        )
    return owners[0]


def _runtime_manifest_for_docx(
    module_sha256: str,
    *,
    origin: Path | None = None,
    python_runtime: Path | None = None,
) -> dict[str, object]:
    origin = (origin or Path(docx.__file__)).resolve(strict=True)
    python_runtime = Path(
        os.path.abspath((python_runtime or Path(sys.executable)).expanduser())
    )
    runtime_root = origin.parent.parent
    distribution = _python_docx_distribution_for_origin(origin)
    distribution_files = tuple(distribution.files or ())
    metadata_entry = next(
        entry
        for entry in distribution_files
        if Path(str(entry)).name == "METADATA"
        and Path(str(entry)).parent.name.endswith(".dist-info")
    )
    record_entry = next(
        entry
        for entry in distribution_files
        if Path(str(entry)).name == "RECORD"
        and Path(str(entry)).parent == Path(str(metadata_entry)).parent
    )
    metadata_origin = Path(distribution.locate_file(metadata_entry)).resolve(strict=True)
    record_origin = Path(distribution.locate_file(record_entry)).resolve(strict=True)
    return {
        "runtime_root": str(runtime_root),
        "base_runtime_root": str(python_runtime.resolve(strict=True).parent),
        "python_runtime": str(python_runtime),
        "python_runtime_realpath": str(python_runtime.resolve(strict=True)),
        "tree_sha256": "c" * 64,
        "files": [
            {
                "path": origin.relative_to(runtime_root).as_posix(),
                "kind": "file",
                "sha256": module_sha256,
            },
            {
                "path": metadata_origin.relative_to(runtime_root).as_posix(),
                "kind": "file",
                "sha256": hashlib.sha256(metadata_origin.read_bytes()).hexdigest(),
            },
            {
                "path": record_origin.relative_to(runtime_root).as_posix(),
                "kind": "file",
                "sha256": hashlib.sha256(record_origin.read_bytes()).hexdigest(),
            },
        ],
        "base_files": [],
    }


def _python_docx_probe_payload(
    module_sha256: str,
    *,
    origin: Path | None = None,
    version: str = "1.2.0",
    version_at_least_minimum: bool = True,
) -> dict[str, object]:
    origin = (origin or Path(docx.__file__)).resolve(strict=True)
    distribution = _python_docx_distribution_for_origin(origin)
    distribution_files = tuple(distribution.files or ())
    metadata_entry = next(
        entry
        for entry in distribution_files
        if Path(str(entry)).name == "METADATA"
        and Path(str(entry)).parent.name.endswith(".dist-info")
    )
    record_entry = next(
        entry
        for entry in distribution_files
        if Path(str(entry)).name == "RECORD"
        and Path(str(entry)).parent == Path(str(metadata_entry)).parent
    )
    metadata_origin = Path(distribution.locate_file(metadata_entry)).resolve(strict=True)
    record_origin = Path(distribution.locate_file(record_entry)).resolve(strict=True)
    return {
        "ok": True,
        "module": "docx",
        "distribution": "python-docx",
        "distribution_count": 1,
        "distribution_metadata_origin": str(metadata_origin),
        "distribution_metadata_sha256": hashlib.sha256(
            metadata_origin.read_bytes()
        ).hexdigest(),
        "distribution_record_origin": str(record_origin),
        "distribution_record_sha256": hashlib.sha256(
            record_origin.read_bytes()
        ).hexdigest(),
        "distribution_module_entry": "docx/__init__.py",
        "distribution_module_origin": str(origin),
        "distribution_module_record_sha256": module_sha256,
        "distribution_module_record_size": origin.stat().st_size,
        "distribution_version_matches_module": True,
        "version": version,
        "version_at_least_minimum": version_at_least_minimum,
        "module_sha256": module_sha256,
        "origin": str(origin),
        "roundtrip_succeeded": True,
    }


def test_python_docx_runtime_preflight_uses_isolated_selected_runtime(
    seatbelt_capable: None,
) -> None:
    module_sha256 = hashlib.sha256(Path(docx.__file__).read_bytes()).hexdigest()

    evidence = REAL_DOCX_RUNTIME_PROBE(
        Path(sys.executable),
        _runtime_manifest_for_docx(module_sha256),
    )

    assert evidence["module"] == "docx"
    assert evidence["distribution"] == "python-docx"
    assert evidence["module_sha256"] == module_sha256
    assert evidence["import_succeeded"] is True
    assert evidence["module_manifest_bound"] is True
    assert evidence["roundtrip_succeeded"] is True
    assert str(Path(docx.__file__).resolve(strict=True)) not in json.dumps(evidence)
    assert "magi-v3-docx-preflight-" not in json.dumps(evidence)
    assert set(evidence) == {
        "module",
        "distribution",
        "minimum_version",
        "version",
        "module_sha256",
        "distribution_metadata_sha256",
        "distribution_record_sha256",
        "runtime_tree_sha256",
        "distribution_unambiguous",
        "distribution_metadata_manifest_bound",
        "distribution_record_manifest_bound",
        "distribution_module_owned",
        "distribution_module_record_bound",
        "distribution_version_matches_module",
        "import_succeeded",
        "module_manifest_bound",
        "roundtrip_succeeded",
        "evidence_sha256",
    }


def test_python_docx_runtime_preflight_blocks_missing_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deploy,
        "_run_bounded_runtime_probe",
        lambda _runtime: json.dumps(
            {"ok": False, "error_type": "PackageNotFoundError"}
        ).encode(),
    )

    with pytest.raises(
        deploy.DeployPrepareBlocked,
        match="missing the required python-docx module",
    ):
        REAL_DOCX_RUNTIME_PROBE(
            Path(sys.executable),
            _runtime_manifest_for_docx("d" * 64),
        )


def test_python_docx_runtime_preflight_blocks_too_old_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_sha256 = "e" * 64
    monkeypatch.setattr(
        deploy,
        "_run_bounded_runtime_probe",
        lambda _runtime: json.dumps(
            _python_docx_probe_payload(
                module_sha256,
                version="0.9.9",
                version_at_least_minimum=False,
            )
        ).encode(),
    )

    with pytest.raises(deploy.DeployPrepareBlocked, match="version is below 1.0"):
        REAL_DOCX_RUNTIME_PROBE(
            Path(sys.executable),
            _runtime_manifest_for_docx(module_sha256),
        )


def test_python_docx_runtime_preflight_blocks_unbound_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_sha256 = "f" * 64
    monkeypatch.setattr(
        deploy,
        "_run_bounded_runtime_probe",
        lambda _runtime: json.dumps(
            _python_docx_probe_payload(module_sha256)
        ).encode(),
    )

    with pytest.raises(deploy.DeployPrepareBlocked, match="not hash-bound"):
        REAL_DOCX_RUNTIME_PROBE(
            Path(sys.executable),
            _runtime_manifest_for_docx("0" * 64),
        )


def test_python_docx_runtime_preflight_blocks_origin_outside_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "outside" / "docx" / "__init__.py"
    _write(origin, "__version__ = '1.2.0'\n")
    module_sha256 = hashlib.sha256(origin.read_bytes()).hexdigest()
    forged_payload = _python_docx_probe_payload(
        hashlib.sha256(Path(docx.__file__).read_bytes()).hexdigest()
    )
    forged_payload.update(
        {
            "origin": str(origin),
            "distribution_module_origin": str(origin),
            "module_sha256": module_sha256,
            "distribution_module_record_sha256": module_sha256,
            "distribution_module_record_size": origin.stat().st_size,
        }
    )
    monkeypatch.setattr(
        deploy,
        "_run_bounded_runtime_probe",
        lambda _runtime: json.dumps(forged_payload).encode(),
    )

    with pytest.raises(deploy.DeployPrepareBlocked, match="orphaned from its module"):
        REAL_DOCX_RUNTIME_PROBE(
            Path(sys.executable),
            _runtime_manifest_for_docx("0" * 64),
        )


def test_docx_test_helpers_ignore_unowned_forged_distribution_on_sys_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_site = tmp_path / "polluted-site-packages"
    forged_dist_info = fake_site / "python_docx-99.0.0.dist-info"
    forged_dist_info.mkdir(parents=True)
    metadata = forged_dist_info / "METADATA"
    metadata.write_text(
        "Metadata-Version: 2.1\nName: python-docx\nVersion: 99.0.0\n",
        encoding="utf-8",
    )
    record = forged_dist_info / "RECORD"
    record.write_text(
        "python_docx-99.0.0.dist-info/METADATA,,\n"
        "python_docx-99.0.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(fake_site))

    assert importlib.metadata.distribution("python-docx").version == "99.0.0"
    origin = Path(docx.__file__).resolve(strict=True)
    owner = _python_docx_distribution_for_origin(origin)
    assert owner.version == docx.__version__

    module_sha256 = hashlib.sha256(origin.read_bytes()).hexdigest()
    manifest = _runtime_manifest_for_docx(module_sha256)
    payload = _python_docx_probe_payload(module_sha256)
    assert all(
        not str(row["path"]).startswith("python_docx-99.0.0.dist-info/")
        for row in manifest["files"]
    )
    assert not str(payload["distribution_metadata_origin"]).startswith(str(fake_site))


def test_python_docx_runtime_preflight_rejects_forged_newer_dist_info_for_old_module(
    tmp_path: Path,
) -> None:
    fake_site = tmp_path / "site-packages"
    fake_docx = fake_site / "docx"
    shutil.copytree(Path(docx.__file__).resolve(strict=True).parent, fake_docx)
    fake_init = fake_docx / "__init__.py"
    fake_init.chmod(0o600)
    fake_init.write_text(
        fake_init.read_text(encoding="utf-8").replace(
            '__version__ = "1.2.0"',
            '__version__ = "0.9.0"',
        ),
        encoding="utf-8",
    )
    forged_dist_info = fake_site / "python_docx-99.0.0.dist-info"
    forged_dist_info.mkdir(parents=True)
    metadata = forged_dist_info / "METADATA"
    metadata.write_text(
        "Metadata-Version: 2.1\nName: python-docx\nVersion: 99.0.0\n",
        encoding="utf-8",
    )

    def record_row(relative: str, path: Path) -> str:
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(path.read_bytes()).digest()
        ).decode("ascii").rstrip("=")
        return f"{relative},sha256={digest},{path.stat().st_size}"

    record = forged_dist_info / "RECORD"
    record.write_text(
        "\n".join(
            (
                record_row("docx/__init__.py", fake_init),
                record_row("python_docx-99.0.0.dist-info/METADATA", metadata),
                "python_docx-99.0.0.dist-info/RECORD,,",
                "",
            )
        ),
        encoding="utf-8",
    )

    import packaging.utils  # noqa: F401
    import packaging.version  # noqa: F401

    original_sys_path = list(sys.path)
    original_docx_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "docx" or name.startswith("docx.")
    }

    def has_python_docx_distribution(raw: str) -> bool:
        try:
            root = Path(raw or os.getcwd())
            return root.is_dir() and any(root.glob("*docx*.dist-info"))
        except OSError:
            return False

    try:
        for name in tuple(original_docx_modules):
            sys.modules.pop(name, None)
        sys.path[:] = [str(fake_site), *[
            entry
            for entry in original_sys_path
            if not has_python_docx_distribution(entry)
        ]]
        candidates = [
            candidate
            for candidate in importlib.metadata.distributions()
            if candidate.metadata.get("Name") == "python-docx"
        ]
        assert len(candidates) == 1
        assert candidates[0].version == "99.0.0"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(deploy._RUNTIME_MODULE_PROBE, {})
        payload = json.loads(output.getvalue())
        assert sys.modules["docx"].__version__ == "0.9.0"
        assert payload == {"ok": False, "error_type": "RuntimeError"}
    finally:
        for name in tuple(sys.modules):
            if name == "docx" or name.startswith("docx."):
                sys.modules.pop(name, None)
        sys.modules.update(original_docx_modules)
        sys.path[:] = original_sys_path


def test_python_docx_runtime_preflight_blocks_timeout(
    monkeypatch: pytest.MonkeyPatch,
    seatbelt_capable: None,
) -> None:
    monkeypatch.setattr(deploy, "_RUNTIME_MODULE_PROBE", "import time; time.sleep(60)")
    monkeypatch.setattr(deploy, "RUNTIME_MODULE_PROBE_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(deploy.DeployPrepareBlocked, match="before timeout"):
        deploy._run_bounded_runtime_probe(Path(sys.executable))


def test_python_docx_runtime_preflight_blocks_oversize_output(
    monkeypatch: pytest.MonkeyPatch,
    seatbelt_capable: None,
) -> None:
    monkeypatch.setattr(
        deploy,
        "_RUNTIME_MODULE_PROBE",
        "import os; os.write(1, b'x' * 8192)",
    )

    with pytest.raises(deploy.DeployPrepareBlocked, match="output limit"):
        deploy._run_bounded_runtime_probe(Path(sys.executable))


def _validation_inputs(tmp_path: Path) -> tuple[Path, ...]:
    root = tmp_path / "isolated-validation-inputs"
    env_file = root / "validation.env"
    _write(env_file, deploy.VALIDATION_ENV_BYTES.decode())
    env_file.chmod(0o600)
    cron = root / "cron.json"
    _write(cron, json.dumps([deploy.VALIDATION_CRON_JOB]) + "\n")
    website = root / "website"
    _write(website / "admin" / "admin_server.py", "class AdminHandler: pass\n")
    (website / "assets").mkdir()
    _write(website / "data" / "live-validation-document.txt", "isolated fixture\n")
    laf_config = root / "laf-config.json"
    laf_config.write_bytes(deploy.VALIDATION_LAF_CONFIG_BYTES)
    laf_config.chmod(0o600)
    credentials = root / "credentials.json"
    credentials.write_bytes(deploy.VALIDATION_GOOGLE_CREDENTIALS_BYTES)
    credentials.chmod(0o600)
    calendar_token = root / "google-calendar-token.json"
    calendar_token.write_bytes(deploy.VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES)
    laf_token = root / "laf-gmail-token.pickle"
    laf_token.write_bytes(deploy.VALIDATION_LAF_GMAIL_TOKEN_BYTES)
    file_review_token = root / "filereview-token.pickle"
    file_review_token.write_bytes(deploy.VALIDATION_LAF_GMAIL_TOKEN_BYTES)
    ocr_queue = root / "nas-ocr-queue.db"
    with sqlite3.connect(ocr_queue) as connection:
        connection.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY)")
    return (
        root,
        env_file,
        cron,
        website,
        laf_config,
        credentials,
        calendar_token,
        laf_token,
        file_review_token,
        ocr_queue,
    )


def _release(
    tmp_path: Path,
    *,
    missing_role: str | None = None,
    include_cron_policy: bool = True,
    policy_cron_sha256: str | None = None,
) -> tuple[Path, Path]:
    release = tmp_path / "release"
    modules = {
        "gateway": "magi_v3/gateway.py",
        "control": "magi_v3/control.py",
        "supervisor": "magi_v3/supervisor_service.py",
    }
    for role, relative in modules.items():
        if role != missing_role:
            _write(release / relative, "def main():\n    return 0\n")
    service_scripts = (
        "api/discord_bot.py",
        "skills/ops/file_review_auto_worker.py",
        "skills/ops/heartbeat.py",
        "magi_v3/legacy_background_service.py",
        "scripts/ops/osc_shell_nas_helper.py",
        "gui/magi_menubar.py",
        "magi_v3/live_validation_probe_service.py",
    )
    for relative in service_scripts:
        _write(release / relative, "def main():\n    return 0\n")
    executable = release / "bin" / "magi-v3-python"
    _write(executable, "#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    roles_config = release / "config" / "v3_launchagent_roles.json"
    _write(
        roles_config,
        json.dumps(
            {
                "schema_version": 1,
                "roles": [
                    {
                        "role": "gateway",
                        "label": "com.magi.v3.gateway",
                        "entrypoint_module": "magi_v3.gateway",
                        "ports": [5002, 5003],
                        "ownership_domains": ["gateway", "webhook"],
                    },
                    {
                        "role": "control",
                        "label": "com.magi.v3.control",
                        "entrypoint_module": "magi_v3.control",
                        "ports": [8088],
                        "ownership_domains": ["scheduler", "writer", "browser", "model"],
                    },
                    {
                        "role": "supervisor",
                        "label": "com.magi.v3.supervisor",
                        "entrypoint_module": "magi_v3.supervisor_service",
                        "ports": [],
                        "ownership_domains": ["discord_consumer", "file_watcher", "notification_sender"],
                    },
                ],
            }
        ),
    )
    for name in ("v3_service_manifest.json", "v3_live_validation_service_manifest.json"):
        _write(release / "config" / name, (ROOT / "config" / name).read_text(encoding="utf-8"))
    if include_cron_policy:
        cron_source = Path(os.environ["MAGI_CRON_JOBS_FILE"])
        source_sha256 = policy_cron_sha256 or hashlib.sha256(
            cron_source.read_bytes()
        ).hexdigest()
        _write(
            release / deploy.CRON_DISPATCH_POLICY_NAME,
            json.dumps(
                {
                    "schema_version": 1,
                    "cron_jobs_sha256": source_sha256,
                }
            ),
        )
    for directory, _directory_names, file_names in os.walk(release):
        base = Path(directory)
        for name in file_names:
            member = base / name
            member.chmod(0o555 if member.stat().st_mode & 0o111 else 0o444)
    files = []
    for relative in (
        *modules.values(),
        "bin/magi-v3-python",
        "config/v3_launchagent_roles.json",
        "config/v3_service_manifest.json",
        "config/v3_live_validation_service_manifest.json",
        deploy.CRON_DISPATCH_POLICY_NAME,
        *service_scripts,
    ):
        path = release / relative
        if path.is_file():
            files.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                    "mode": f"{path.stat().st_mode & 0o777:04o}",
                }
            )
    files.sort(key=lambda row: row["path"])
    release_sha256 = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "release_id": "v3-test-release",
        "immutable": True,
        "source_snapshot_sha256": release_sha256,
        "release_sha256": release_sha256,
        "files": files,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    release.mkdir(exist_ok=True)
    (release / deploy.RELEASE_MANIFEST_NAME).write_bytes(manifest_bytes)
    marker = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "manifest": deploy.RELEASE_MANIFEST_NAME,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_snapshot_sha256": release_sha256,
        "release_sha256": release_sha256,
    }
    (release / deploy.RELEASE_MARKER_NAME).write_text(
        json.dumps(marker),
        encoding="utf-8",
    )
    _seal_test_release(release)
    installed = deploy._canonical_installed_release_root(manifest["release_id"])
    _remove_test_tree(installed)
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(release, installed)
    source_paths = {
        "env_file": Path(os.environ["MAGI_V3_TEST_STATIC_ENV_FILE"]),
        "website_root": Path(os.environ["MAGI_V3_TEST_STATIC_WEBSITE_ROOT"]),
        "config_file": Path(os.environ["MAGI_V3_TEST_STATIC_CONFIG_FILE"]),
        "google_credentials_file": Path(
            os.environ["MAGI_V3_TEST_STATIC_GOOGLE_CREDENTIALS_FILE"]
        ),
        "accounting_credentials_file": Path(
            os.environ["MAGI_V3_TEST_STATIC_ACCOUNTING_CREDENTIALS_FILE"]
        ),
    }
    release_manifest = release / deploy.RELEASE_MANIFEST_NAME
    release_manifest_sha = hashlib.sha256(release_manifest.read_bytes()).hexdigest()
    source_snapshot = static_external.snapshot_static_sources(
        release_manifest,
        expected_release_manifest_sha256=release_manifest_sha,
        **source_paths,
    )
    target = deploy._canonical_runtime_root() / "shared" / "external"
    refresh_sha: str | None = None
    if target.exists():
        refresh_sha = json.loads(
            (target / static_external.RECEIPT_NAME).read_text(encoding="utf-8")
        )["target_snapshot_sha256"]
    static_external.stage_static_external(
        release_manifest,
        expected_release_manifest_sha256=release_manifest_sha,
        expected_source_snapshot_sha256=source_snapshot["source_snapshot_sha256"],
        target_root=target,
        refresh_expected_target_snapshot_sha256=refresh_sha,
        **source_paths,
    )
    os.environ["MAGI_ENV_FILE"] = str(target / ".env")
    os.environ["MAGI_WEBSITE_ROOT"] = str(target / "website")
    os.environ["MAGI_LAF_CONFIG_FILE"] = str(target / "config.json")
    os.environ["MAGI_GOOGLE_CREDENTIALS_PATH"] = str(
        target / "google-credentials.json"
    )
    os.environ["MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH"] = str(
        target / "accounting-credentials.json"
    )
    os.environ["MAGI_V3_STATIC_EXTERNAL_RECEIPT"] = str(
        target / static_external.RECEIPT_NAME
    )
    return release, executable


@pytest.fixture(autouse=True)
def canonical_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    application_support = tmp_path / "Application Support"
    runtime = application_support / "MAGI" / "runtime" / "MAGI_v3"
    monkeypatch.setattr(deploy, "_application_support_root", lambda: application_support)
    monkeypatch.setattr(deploy, "_canonical_runtime_root", lambda: runtime)
    monkeypatch.setattr(
        deploy,
        "_probe_python_docx_runtime",
        lambda *_args, **_kwargs: _stub_docx_preflight(),
    )
    monkeypatch.setattr(
        deploy,
        "_validate_shapely_sealed_runtime",
        lambda *_args, **_kwargs: None,
    )
    env_file = tmp_path / "runtime-inputs" / "magi.env"
    _write(env_file, "TEST_ONLY=1\n")
    env_file.chmod(0o600)
    cron_jobs = tmp_path / "runtime-inputs" / "cron_jobs.json"
    _write(
        cron_jobs,
        '[{"id":"heartbeat","cron":"*/5 * * * *","command":"@MAGI health"}]\n',
    )
    website = tmp_path / "runtime-inputs" / "website"
    _write(website / "admin" / "admin_server.py", "class AdminHandler: pass\n")
    laf_config = tmp_path / "runtime-inputs" / "laf-config.json"
    _write(laf_config, "{}\n")
    laf_config.chmod(0o600)
    google_credentials = tmp_path / "runtime-inputs" / "credentials.json"
    _write(google_credentials, "{}\n")
    google_credentials.chmod(0o600)
    google_calendar_token = tmp_path / "runtime-inputs" / "google-calendar-token.json"
    _write(google_calendar_token, "{}\n")
    laf_gmail_token = tmp_path / "runtime-inputs" / "laf-gmail-token.pickle"
    _write(laf_gmail_token, "inert\n")
    file_review_token = tmp_path / "runtime-inputs" / "filereview-token.pickle"
    _write(file_review_token, "inert\n")
    accounting_credentials = tmp_path / "runtime-inputs" / "accounting-credentials.json"
    _write(accounting_credentials, "{}\n")
    accounting_credentials.chmod(0o600)
    accounting_token = tmp_path / "runtime-inputs" / "accounting-token.json"
    drive_write_token = tmp_path / "runtime-inputs" / "drive-write-token.json"
    _write(accounting_token, "{}\n")
    _write(drive_write_token, "{}\n")
    ocr_queue = tmp_path / "runtime-inputs" / "nas-ocr-queue.db"
    with sqlite3.connect(ocr_queue) as connection:
        connection.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(deploy, "_canonical_nas_ocr_queue_db", lambda: ocr_queue.resolve())
    python_runtime = _fake_venv(tmp_path / "runtime-inputs" / "python-venv")
    nas_root = tmp_path / "runtime-inputs" / "nas"
    case_root = nas_root / "01_案件"
    archive_root = nas_root / "10_結案"
    case_root.mkdir(parents=True)
    archive_root.mkdir(parents=True)
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(cron_jobs))
    monkeypatch.setenv("MAGI_WEBSITE_ROOT", str(website))
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(python_runtime))
    monkeypatch.setenv("MAGI_LAF_CONFIG_FILE", str(laf_config))
    monkeypatch.setenv("MAGI_GOOGLE_CREDENTIALS_PATH", str(google_credentials))
    monkeypatch.setenv("MAGI_GOOGLE_CALENDAR_TOKEN_PATH", str(google_calendar_token))
    monkeypatch.setenv("MAGI_LAF_GMAIL_TOKEN_PATH", str(laf_gmail_token))
    monkeypatch.setenv("MAGI_FILE_REVIEW_TOKEN_PATH", str(file_review_token))
    monkeypatch.setenv("MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH", str(accounting_credentials))
    monkeypatch.setenv("MAGI_V3_TEST_STATIC_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_V3_TEST_STATIC_WEBSITE_ROOT", str(website))
    monkeypatch.setenv("MAGI_V3_TEST_STATIC_CONFIG_FILE", str(laf_config))
    monkeypatch.setenv(
        "MAGI_V3_TEST_STATIC_GOOGLE_CREDENTIALS_FILE", str(google_credentials)
    )
    monkeypatch.setenv(
        "MAGI_V3_TEST_STATIC_ACCOUNTING_CREDENTIALS_FILE",
        str(accounting_credentials),
    )
    monkeypatch.setenv("MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN", str(accounting_token))
    monkeypatch.setenv("MAGI_DRIVE_SYNC_TOKEN", str(accounting_token))
    monkeypatch.setenv("MAGI_DRIVE_SYNC_WRITE_TOKEN", str(drive_write_token))
    monkeypatch.setenv("MAGI_NAS_OCR_QUEUE_DB_PATH", str(ocr_queue))
    monkeypatch.setenv("MAGI_V3_CASE_ROOT", str(case_root))
    monkeypatch.setenv("MAGI_V3_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("MAGI_V3_PATH_MAPPINGS_JSON", json.dumps([[str(nas_root), "Z:"]]))
    return runtime


def test_prepare_binds_all_roles_release_identity_and_ownership_with_hashes(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    staging = tmp_path / "caller-staging"
    runtime = canonical_runtime_root
    generated_at = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    marker = deploy.prepare_deployment(
        release,
        staging,
        runtime,
        executable,
        now=generated_at,
    )

    persisted_marker = json.loads(
        (staging / deploy.COMPLETION_MARKER_NAME).read_text(encoding="utf-8")
    )
    manifest_path = staging / deploy.DEPLOY_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ownership = json.loads(
        (staging / deploy.OWNERSHIP_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    release_digest = hashlib.sha256(
        (release / deploy.RELEASE_MANIFEST_NAME).read_bytes()
    ).hexdigest()
    installed_release = deploy._canonical_installed_release_root("v3-test-release")
    installed_executable = installed_release / "bin" / "magi-v3-python"

    assert marker == persisted_marker
    assert marker["status"] == "prepared_not_installed"
    assert marker["ready_to_install"] is True
    assert marker["mutation_performed"] is False
    assert marker["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert manifest["release_id"] == ownership["release_id"] == "v3-test-release"
    assert manifest["release_manifest_sha256"] == ownership["release_manifest_sha256"] == release_digest
    assert manifest["runtime_root"] == ownership["runtime_root"] == str(runtime)
    assert manifest["external_inputs"]["python_runtime_module_preflight"] == (
        ownership["external_inputs"]["python_runtime_module_preflight"]
    ) == _stub_docx_preflight()
    ownership_digest = hashlib.sha256(
        (staging / deploy.OWNERSHIP_MANIFEST_NAME).read_bytes()
    ).hexdigest()
    assert marker["ownership_manifest_sha256"] == ownership_digest
    assert manifest["ownership_manifest_sha256"] == ownership_digest
    assert manifest["ownership_manifest"] == str(runtime / deploy.OWNERSHIP_MANIFEST_NAME)
    assert manifest["generated_at"] == generated_at.isoformat()
    assert (runtime / "shared" / "external" / static_external.RECEIPT_NAME).is_file()
    static_receipt = runtime / "shared" / "external" / static_external.RECEIPT_NAME
    static_receipt_sha = hashlib.sha256(static_receipt.read_bytes()).hexdigest()
    assert manifest["static_external_receipt"] == ownership[
        "static_external_receipt"
    ] == str(static_receipt)
    assert manifest["static_external_receipt_sha256"] == ownership[
        "static_external_receipt_sha256"
    ] == static_receipt_sha
    assert manifest["static_external_target_snapshot_sha256"] == ownership[
        "static_external_target_snapshot_sha256"
    ]
    assert manifest["external_inputs"]["env_file"] == str(
        runtime / "shared" / "external" / ".env"
    )
    assert manifest["external_inputs"]["website_root"] == str(
        runtime / "shared" / "external" / "website"
    )
    expected_named = deploy.named_mutable_state_paths(runtime)
    assert {
        name: manifest["external_inputs"][name] for name in expected_named
    } == expected_named
    assert all(not Path(path).exists() for path in expected_named.values())

    roles = {binding["role"]: binding for binding in ownership["roles"]}
    manifest_roles = {binding["role"]: binding for binding in manifest["roles"]}
    nas_root = (tmp_path / "runtime-inputs" / "nas").resolve()
    case_root = nas_root / "01_案件"
    archive_root = nas_root / "10_結案"
    assert set(roles) == {"gateway", "control", "supervisor"}
    assert roles["gateway"]["ports"] == [5002, 5003]
    assert roles["control"]["ports"] == [8088]
    assert roles["supervisor"]["ports"] == []
    assert ownership["uninstall_labels"] == [
        "com.magi.v3.control",
        "com.magi.v3.gateway",
        "com.magi.v3.supervisor",
    ]
    for role, binding in roles.items():
        plist_path = staging / "launchagents" / f"{binding['label']}.plist"
        plist = plistlib.loads(plist_path.read_bytes())
        environment = plist["EnvironmentVariables"]
        assert {name: binding[name] for name in expected_named} == expected_named
        assert {
            name: manifest_roles[role][name] for name in expected_named
        } == expected_named
        assert {
            env_name: environment[env_name]
            for env_name in deploy.NAMED_MUTABLE_STATE_BINDINGS
        } == {
            env_name: expected_named[binding_name]
            for env_name, (binding_name, _relative) in deploy.NAMED_MUTABLE_STATE_BINDINGS.items()
        }
        assert plist["Label"] == deploy.EXPECTED_LABELS[role]
        assert plist["ProcessType"] == deploy.PROCESS_TYPE_BY_ROLE[role]
        assert plist["ProcessType"] == (
            "Interactive" if role == "gateway" else "Background"
        )
        assert plist["ProgramArguments"] == binding["ProgramArguments"]
        assert plist["ProgramArguments"][0] == str(installed_executable)
        assert plist["ProgramArguments"][1] == "-c"
        assert "runpy.run_module" in plist["ProgramArguments"][2]
        assert plist["ProgramArguments"][3] == binding["owner_token"]
        assert binding["owner_token"].endswith(".magi-v3-test-release-owner")
        assert plist["WorkingDirectory"] == binding["WorkingDirectory"] == str(
            installed_release
        )
        assert environment["MAGI_V3_RELEASE_ID"] == "v3-test-release"
        assert environment["MAGI_V3_STATIC_EXTERNAL_RECEIPT"] == str(static_receipt)
        assert environment["MAGI_V3_STATIC_EXTERNAL_RECEIPT_SHA256"] == static_receipt_sha
        assert environment["MAGI_V3_STATIC_EXTERNAL_TARGET_SNAPSHOT_SHA256"] == manifest[
            "static_external_target_snapshot_sha256"
        ]
        assert environment["MAGI_V3_OWNERSHIP_MANIFEST"] == str(
            runtime / deploy.OWNERSHIP_MANIFEST_NAME
        )
        assert environment["MAGI_V3_OWNERSHIP_MANIFEST_SHA256"] == ownership_digest
        assert environment["MAGI_V3_EXECUTABLE_PATH"] == str(installed_executable)
        assert environment["MAGI_V3_CASE_ROOT"] == str(case_root)
        assert environment["MAGI_V3_ARCHIVE_ROOT"] == str(archive_root)
        assert json.loads(environment["MAGI_V3_PATH_MAPPINGS_JSON"]) == [
            [str(nas_root), "Z:"]
        ]
        assert environment["MAGI_ENV_FILE"]
        assert environment["MAGI_ENV_FILE_SHA256"]
        assert environment["MAGI_CRON_JOBS_FILE"] == str(
            staging / "runtime-inputs" / "cron_jobs.v3.json"
        )
        assert environment["MAGI_CRON_JOBS_SHA256"] == hashlib.sha256(
            Path(environment["MAGI_CRON_JOBS_FILE"]).read_bytes()
        ).hexdigest()
        source_cron = Path(manifest["external_inputs"]["cron_jobs_source_file"])
        source_sha = hashlib.sha256(source_cron.read_bytes()).hexdigest()
        assert environment["MAGI_CRON_JOBS_SOURCE_SHA256"] == source_sha
        assert binding["cron_jobs_source_sha256"] == source_sha
        assert environment["MAGI_CRON_JOBS_SHA256"] != source_sha
        assert environment["MAGI_CRON_DEFINITIONS_IMMUTABLE"] == "1"
        assert environment["MAGI_USE_RUNTIME_DIR"] == "1"
        assert environment["MAGI_RUNTIME_DIR"] == str(runtime / "shared" / "runtime")
        assert environment["MAGI_V3_SHARED_STATE_DIR"] == str(runtime / "shared")
        assert environment["MAGI_SHARED_STATE_DIR"] == str(runtime / "shared")
        assert environment["MAGI_AGENT_DIR"] == str(runtime / "shared" / "agent")
        assert environment["MAGI_DATA_DIR"] == environment["MAGI_AGENT_DIR"]
        assert environment["MAGI_MUTABLE_STATIC_DIR"] == str(runtime / "shared" / "static")
        assert environment["MAGI_GCAL_DUP_AUDIT_OUTPUT_DIR"] == str(
            runtime / "shared" / "exports" / "gcal_dedup"
        )
        assert environment["MAGI_FILE_REVIEW_STATE_DIR"] == str(runtime / "shared" / "file-review")
        assert environment["MAGI_FILE_REVIEW_BG_JOB_DIR"] == str(
            runtime / "shared" / "file-review" / "bg-jobs"
        )
        assert environment["MAGI_EEFILE_DOWNLOAD_FOLDER"] == str(
            runtime / "shared" / "file-review" / "downloads"
        )
        assert environment["MAGI_BACKGROUND_LOCK_DIR"] == str(runtime / "shared" / "runtime" / "locks")
        assert environment["MAGI_LAF_GMAIL_STATE_PATH"] == str(
            runtime / "shared" / "static" / "laf_gmail_monitor_state.json"
        )
        assert environment["MAGI_LAF_GMAIL_MONITOR_STATE"] == environment["MAGI_LAF_GMAIL_STATE_PATH"]
        assert environment["MAGI_LAF_GMAIL_PENDING_PATH"] == str(
            runtime / "shared" / "runtime" / "laf_gmail_dispatch_pending.json"
        )
        assert environment["MAGI_FILE_REVIEW_EMAIL_MONITOR_STATE"] == str(
            runtime / "shared" / "static" / "file_review_email_monitor_state.json"
        )
        assert environment["MAGI_FILE_REVIEW_PENDING_PATH"] == str(
            runtime / "shared" / "agent" / "file-review" / "review_submit_pending.json"
        )
        assert environment["MAGI_BRAIN_SQLITE_PATH"] == str(
            runtime / "shared" / "agent" / "magi_brain.db"
        )
        assert environment["MAGI_CLOUDFLARED_LOG_PATH"] == str(
            Path(binding["log_dir"]) / "cloudflared.log"
        )
        assert environment["MAGI_DAEMON_LOG_PATH"] == str(
            runtime / "shared" / "agent" / "daemon.log"
        )
        assert environment["MAGI_ORCH_DIR"] == str(
            installed_release / "casper_ecosystem" / "law_firm_orchestrators"
        )
        assert environment["MAGI_CODE_DIR"] == environment["MAGI_ORCH_DIR"]
        assert environment["MAGI_JSON_DIR"] == str(
            Path(manifest["external_inputs"]["laf_config_file"]).parent
        )
        assert environment["MAGI_SKILL_PYTHON"] == environment["MAGI_V3_PYTHON_RUNTIME"]
        assert environment["MAGI_PDF_NAMER_STATE_DIR"] == str(runtime / "shared" / "pdf-namer")
        assert environment["MAGI_SKILL_OVERLAY_DIR"] == str(runtime / "shared" / "skill-overlays")
        assert environment["MAGI_SKILL_RUNTIME_SITE_PACKAGES"] == str(
            runtime / "shared" / "skill-overlays" / ".runtime-site-packages"
        )
        assert environment["MAGI_SKILL_EVENTS_FILE"] == str(
            runtime / "shared" / "skill-overlays" / ".logs" / "skill_runtime_events.jsonl"
        )
        assert environment["MAGI_SKILL_USAGE_TRACKER_FILE"] == str(
            runtime / "shared" / "skill-overlays" / ".logs" / "skill_usage_events.jsonl"
        )
        assert environment["MAGI_AUTORESEARCH_RUNS_DIR"] == str(
            runtime / "shared" / "autoresearch-runs"
        )
        assert environment["JUDICIAL_CACHE_DIR"] == str(
            runtime / "shared" / "runtime" / "cache" / "judicial_web_search"
        )
        assert environment["MAGI_LAW_CACHE_DIR"] == str(
            runtime / "shared" / "runtime" / "cache" / "laws"
        )
        assert environment["MAGI_LAW_VDB_STATE_PATH"] == str(
            runtime / "shared" / "agent" / "_statutes_vdb_state.json"
        )
        assert environment["FAISS_INDEX_DIR"] == str(
            runtime / "shared" / "memory" / "index_cache"
        )
        assert environment["MAGI_SKILL_INTERVIEW_HISTORY_FILE"] == str(
            runtime / "shared" / "skill-overlays" / ".logs" / "skill_interview_history.jsonl"
        )
        assert environment["MAGI_IRON_DOME_STATE_DIR"] == str(
            runtime / "shared" / "skill-overlays" / ".iron-dome"
        )
        assert environment["MAGI_IRON_DOME_DYNAMIC_RULES_PATH"] == str(
            runtime / "shared" / "skill-overlays" / ".iron-dome" / "dynamic_rules.json"
        )
        assert environment["MAGI_IRON_DOME_PATTERNS_CACHE_FILE"] == str(
            runtime / "shared" / "skill-overlays" / ".iron-dome" / "patterns_cache.json"
        )
        assert environment["MAGI_IRON_DOME_UPSTREAM_STATE_FILE"] == str(
            runtime / "shared" / "skill-overlays" / ".iron-dome" / "upstream_last.json"
        )
        assert environment["MAGI_WEBSITE_ROOT"]
        assert environment["MAGI_WEBSITE_ADMIN_SHA256"]
        assert environment["MAGI_V3_PYTHON_RUNTIME"]
        assert environment["MAGI_V3_PYTHON_RUNTIME_REALPATH"]
        assert environment["MAGI_V3_PYTHON_RUNTIME_SHA256"] == hashlib.sha256(
            Path(environment["MAGI_V3_PYTHON_RUNTIME"]).read_bytes()
        ).hexdigest()
        assert environment["MAGI_V3_RELEASE_MANIFEST_SHA256"] == release_digest
        assert environment["MAGI_V3_EXECUTABLE_SHA256"] == hashlib.sha256(
            executable.read_bytes()
        ).hexdigest()
        assert environment["MAGI_V3_STATE_DIR"] == binding["state_dir"]
        assert environment["MAGI_V3_PID_FILE"] == binding["pid_file"]
        assert environment["MAGI_V3_PORTS"] == ",".join(map(str, binding["ports"]))
        assert environment["MAGI_V3_OWNERSHIP_DOMAINS"] == ",".join(
            binding["ownership_domains"]
        )
        assert plist["RunAtLoad"] is True
        assert plist["KeepAlive"] == {"SuccessfulExit": False}
        assert plist["ThrottleInterval"] == 10

    for artifact in manifest["artifacts"]:
        data = (staging / artifact["path"]).read_bytes()
        assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
        assert artifact["size"] == len(data)


def test_prepare_atomically_publishes_with_final_absolute_bindings(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    staging = tmp_path / ".staging-deployment-v3-test"
    publish = tmp_path / "deployment-v3-test"

    marker = deploy.prepare_deployment(
        release,
        staging,
        canonical_runtime_root,
        executable,
        publish_dir=publish,
    )

    assert marker["ready_to_install"] is True
    assert not staging.exists()
    assert publish.is_dir()

    manifest = json.loads(
        (publish / deploy.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    ownership = json.loads(
        (publish / deploy.OWNERSHIP_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    installed_release = deploy._canonical_installed_release_root("v3-test-release")
    assert manifest["release_manifest"] == ownership["release_manifest"] == str(
        installed_release / deploy.RELEASE_MANIFEST_NAME
    )
    assert manifest["service_manifest"] == ownership["service_manifest"] == str(
        installed_release / deploy.SERVICE_MANIFEST_NAMES["production"]
    )
    expected_cron = publish / "runtime-inputs" / "cron_jobs.v3.json"
    expected_runtime_manifest = publish / "runtime-inputs" / "python-runtime-manifest.json"
    assert manifest["external_inputs"]["cron_jobs_file"] == str(expected_cron)
    assert manifest["external_inputs"]["python_runtime_manifest"] == str(
        expected_runtime_manifest
    )
    assert expected_cron.is_file()
    assert expected_runtime_manifest.is_file()

    for document in (manifest, ownership):
        assert str(staging) not in json.dumps(document, sort_keys=True)
    for role in manifest["roles"]:
        plist = plistlib.loads(
            (publish / "launchagents" / f"{role['label']}.plist").read_bytes()
        )
        environment = plist["EnvironmentVariables"]
        assert environment["MAGI_CRON_JOBS_FILE"] == str(expected_cron)
        assert environment["MAGI_CRON_JOBS_SOURCE_SHA256"] == manifest["external_inputs"][
            "cron_jobs_source_sha256"
        ]
        assert environment["MAGI_V3_PYTHON_RUNTIME_MANIFEST"] == str(
            expected_runtime_manifest
        )
        assert plist["ProgramArguments"][0] == str(
            installed_release / "bin" / "magi-v3-python"
        )
        assert plist["WorkingDirectory"] == str(installed_release)
        for name, suffix in deploy._PRODUCTION_RELEASE_ENV_SUFFIXES.items():
            expected = (
                installed_release
                if suffix == Path(".")
                else installed_release / suffix
            )
            assert environment[name] == str(expected)
        assert str(release) not in json.dumps(plist, sort_keys=True)
        assert str(staging) not in json.dumps(plist, sort_keys=True)

    persisted_marker = publish / deploy.COMPLETION_MARKER_NAME
    deploy_manifest = publish / marker["manifest"]
    assert marker["manifest_sha256"] == hashlib.sha256(
        deploy_manifest.read_bytes()
    ).hexdigest()
    assert persisted_marker.is_file()


def test_production_requires_matching_canonical_installed_release_before_staging(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path / "candidate")
    installed = deploy._canonical_installed_release_root("v3-test-release")

    _remove_test_tree(installed)
    missing_staging = tmp_path / "missing-installed"
    with pytest.raises(deploy.DeployPrepareBlocked, match="release root is missing"):
        deploy.prepare_deployment(
            release,
            missing_staging,
            canonical_runtime_root,
            executable,
        )
    assert not missing_staging.exists()

    _release(tmp_path / "different-installed", missing_role="gateway")
    mismatch_staging = tmp_path / "mismatched-installed"
    with pytest.raises(deploy.DeployPrepareBlocked, match="identity does not match"):
        deploy.prepare_deployment(
            release,
            mismatch_staging,
            canonical_runtime_root,
            executable,
        )
    assert not mismatch_staging.exists()

    _remove_test_tree(installed)
    shutil.copytree(release, installed)
    manifest_path = installed / deploy.RELEASE_MANIFEST_NAME
    installed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    installed_manifest["immutable"] = False
    manifest_path.chmod(0o644)
    manifest_path.write_text(
        json.dumps(installed_manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    immutable_staging = tmp_path / "mutable-installed"
    with pytest.raises(deploy.DeployPrepareBlocked, match="not an immutable"):
        deploy.prepare_deployment(
            release,
            immutable_staging,
            canonical_runtime_root,
            executable,
        )
    assert not immutable_staging.exists()

    _remove_test_tree(installed)
    shutil.copytree(release, installed)
    installed.chmod(0o755)
    mutable_tree_staging = tmp_path / "mutable-installed-tree"
    with pytest.raises(deploy.DeployPrepareBlocked, match="immutable 0555"):
        deploy.prepare_deployment(
            release,
            mutable_tree_staging,
            canonical_runtime_root,
            executable,
        )
    assert not mutable_tree_staging.exists()


def test_production_rejects_installed_hash_drift_noncanonical_root_and_env_residue(
    tmp_path: Path,
    canonical_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, executable = _release(tmp_path / "candidate")
    installed = deploy._canonical_installed_release_root("v3-test-release")
    installed_gateway = installed / "magi_v3" / "gateway.py"
    installed_gateway.chmod(0o644)
    installed_gateway.write_text("def main():\n    return 9\n", encoding="utf-8")
    hash_staging = tmp_path / "hash-drift"
    with pytest.raises(deploy.DeployPrepareBlocked, match="member hash mismatch"):
        deploy.prepare_deployment(
            release,
            hash_staging,
            canonical_runtime_root,
            executable,
        )
    assert not hash_staging.exists()

    _remove_test_tree(installed)
    shutil.copytree(release, installed)
    noncanonical = tmp_path / "published-elsewhere"
    shutil.copytree(release, noncanonical)
    outside_staging = tmp_path / "noncanonical-root"
    with pytest.raises(deploy.DeployPrepareBlocked, match="canonical Application Support"):
        deploy.prepare_deployment(
            release,
            outside_staging,
            canonical_runtime_root,
            executable,
            installed_release_root=noncanonical,
        )
    assert not outside_staging.exists()

    real_plist = deploy._plist

    def leaked_plist(binding: dict[str, object]) -> bytes:
        payload = plistlib.loads(real_plist(binding))
        payload["EnvironmentVariables"]["MAGI_TEST_STAGING_RESIDUE"] = str(
            release / "evidence-only"
        )
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    monkeypatch.setattr(deploy, "_plist", leaked_plist)
    leaked_staging = tmp_path / "candidate-residue"
    with pytest.raises(deploy.DeployPrepareBlocked, match="retained candidate"):
        deploy.prepare_deployment(
            release,
            leaked_staging,
            canonical_runtime_root,
            executable,
        )
    assert not leaked_staging.exists()


@pytest.mark.parametrize("leak_kind", ["argv", "nested_plist", "relative_parent"])
def test_production_recursively_rejects_candidate_paths_anywhere_in_plist(
    tmp_path: Path,
    canonical_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    leak_kind: str,
) -> None:
    release, executable = _release(tmp_path / "candidate")
    installed = deploy._canonical_installed_release_root("v3-test-release")
    hidden = release / "hidden-candidate-member"
    real_plist = deploy._plist

    def leaked_plist(binding: dict[str, object]) -> bytes:
        payload = plistlib.loads(real_plist(binding))
        if leak_kind == "argv":
            payload["ProgramArguments"][2] = str(hidden)
        elif leak_kind == "nested_plist":
            payload["SyntheticNested"] = {"rows": [{"path": str(hidden)}]}
        else:
            payload["ProgramArguments"][2] = os.path.relpath(hidden, installed)
            assert ".." in Path(payload["ProgramArguments"][2]).parts
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    monkeypatch.setattr(deploy, "_plist", leaked_plist)
    staging = tmp_path / f"recursive-residue-{leak_kind}"
    with pytest.raises(deploy.DeployPrepareBlocked, match="metadata retained candidate"):
        deploy.prepare_deployment(
            release,
            staging,
            canonical_runtime_root,
            executable,
        )
    assert not staging.exists()


def test_production_recursively_rejects_candidate_paths_in_binding_metadata(
    tmp_path: Path,
    canonical_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, executable = _release(tmp_path / "candidate")
    real_role_binding = deploy._role_binding

    def leaked_role_binding(*args, **kwargs):
        binding = real_role_binding(*args, **kwargs)
        binding["synthetic_nested"] = {
            "rows": [{"path": str(release / "hidden-binding-member")}]
        }
        return binding

    monkeypatch.setattr(deploy, "_role_binding", leaked_role_binding)
    staging = tmp_path / "binding-residue"
    with pytest.raises(deploy.DeployPrepareBlocked, match="metadata retained candidate"):
        deploy.prepare_deployment(
            release,
            staging,
            canonical_runtime_root,
            executable,
        )
    assert not staging.exists()


def test_prepare_blocks_runtime_mutation_after_docx_probe_before_publish(
    tmp_path: Path,
    canonical_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, executable = _release(tmp_path)
    staging = tmp_path / ".staging-runtime-drift"
    publish = tmp_path / "published-runtime-drift"
    python_runtime = Path(os.environ["MAGI_V3_PYTHON_RUNTIME"])
    mutable_member = (
        python_runtime.parent.parent
        / "lib"
        / "python3.14"
        / "site-packages"
        / "example.py"
    )
    real_build_runtime_manifest = deploy.build_runtime_manifest
    calls = 0

    def mutate_after_first_snapshot(runtime: Path):
        nonlocal calls
        result = real_build_runtime_manifest(runtime)
        calls += 1
        if calls == 1:
            mutable_member.write_text("VALUE = 2\n", encoding="utf-8")
        return result

    monkeypatch.setattr(
        deploy,
        "build_runtime_manifest",
        mutate_after_first_snapshot,
    )

    with pytest.raises(
        deploy.DeployPrepareBlocked,
        match="tree changed after python-docx preflight",
    ):
        deploy.prepare_deployment(
            release,
            staging,
            canonical_runtime_root,
            executable,
            publish_dir=publish,
        )

    assert calls == 2
    assert not publish.exists()
    assert not (staging / deploy.COMPLETION_MARKER_NAME).exists()


def test_production_prepare_requires_staging_receipt_and_exact_target(
    tmp_path: Path,
    canonical_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, executable = _release(tmp_path)
    monkeypatch.delenv("MAGI_V3_STATIC_EXTERNAL_RECEIPT", raising=False)
    with pytest.raises(deploy.DeployPrepareBlocked, match="requires.*static external receipt"):
        deploy.prepare_deployment(
            release,
            tmp_path / "missing-static-receipt",
            canonical_runtime_root,
            executable,
        )
    assert not (tmp_path / "missing-static-receipt").exists()

    wrong_receipt = tmp_path / "wrong" / static_external.RECEIPT_NAME
    wrong_receipt.parent.mkdir()
    wrong_receipt.write_text("{}\n", encoding="utf-8")
    with pytest.raises(deploy.DeployPrepareBlocked, match="canonical V3 shared target"):
        deploy.prepare_deployment(
            release,
            tmp_path / "wrong-static-receipt",
            canonical_runtime_root,
            executable,
            static_external_receipt=wrong_receipt,
        )
    assert not (tmp_path / "wrong-static-receipt").exists()


def test_production_prepare_rejects_staged_target_drift(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    target_config = canonical_runtime_root / "shared" / "external" / "config.json"
    target_config.write_text('{"drift":true}\n', encoding="utf-8")
    target_config.chmod(0o600)
    with pytest.raises(deploy.DeployPrepareBlocked, match="static external receipt is invalid"):
        deploy.prepare_deployment(
            release,
            tmp_path / "drifted-static-target",
            canonical_runtime_root,
            executable,
        )
    assert not (tmp_path / "drifted-static-target").exists()


def test_prepare_isolated_live_validation_binds_only_fail_closed_safety_mode(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    (
        validation_root, env_file, cron, website, laf_config, credentials,
        calendar_token, laf_token, file_review_token, ocr_queue,
    ) = _validation_inputs(tmp_path)
    staging = tmp_path / "validation-deployment"
    policy = json.loads(
        (release / deploy.CRON_DISPATCH_POLICY_NAME).read_text(encoding="utf-8")
    )
    assert policy["cron_jobs_sha256"] != hashlib.sha256(cron.read_bytes()).hexdigest()

    marker = deploy.prepare_deployment(
        release,
        staging,
        canonical_runtime_root,
        executable,
        deployment_mode="isolated_live_validation",
        validation_input_root=validation_root,
        env_file=env_file,
        cron_jobs_file=cron,
        website_root=website,
        laf_config_file=laf_config,
        google_credentials_file=credentials,
        google_calendar_token_file=calendar_token,
        laf_gmail_token_file=laf_token,
        file_review_token_file=file_review_token,
        nas_ocr_queue_db_path=ocr_queue,
    )

    assert marker["deployment_mode"] == "isolated_live_validation"
    deployment = json.loads((staging / deploy.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert deployment["validation_input_root"] == str(validation_root.resolve())
    assert deployment["service_manifest"] == str(
        release / "config" / "v3_live_validation_service_manifest.json"
    )
    for binding in deployment["roles"]:
        plist = plistlib.loads(
            (staging / "launchagents" / f"{binding['label']}.plist").read_bytes()
        )
        environment = plist["EnvironmentVariables"]
        assert plist["ProcessType"] == deploy.PROCESS_TYPE_BY_ROLE[binding["role"]]
        assert environment["MAGI_V3_DEPLOYMENT_MODE"] == "isolated_live_validation"
        assert environment["MAGI_V3_LIVE_VALIDATION"] == "1"
        assert environment["MAGI_V3_EXTERNAL_WRITES_ENABLED"] == "0"
        assert environment["MAGI_V3_NOTIFICATIONS_ENABLED"] == "0"
        assert environment["MAGI_V3_SCHEDULER_ENABLED"] == "0"
        assert environment["MAGI_ENV_FILE"] == str(env_file.resolve())
        assert environment["MAGI_CRON_JOBS_SOURCE_SHA256"] == hashlib.sha256(
            cron.read_bytes()
        ).hexdigest()
        assert environment["MAGI_WEBSITE_ROOT"] == str(website.resolve())
        assert environment["MAGI_LAF_CONFIG_FILE"] == str(laf_config.resolve())
        assert environment["MAGI_LAF_CONFIG_SHA256"] == hashlib.sha256(
            laf_config.read_bytes()
        ).hexdigest()
        assert environment["MAGI_CONFIG_PATH"] == str(laf_config.resolve())
        assert "MAGI_V3_CASE_ROOT" not in environment
        assert "MAGI_V3_ARCHIVE_ROOT" not in environment
        assert "MAGI_V3_PATH_MAPPINGS_JSON" not in environment


def test_production_cron_source_must_match_release_policy_before_staging(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    cron_source = tmp_path / "runtime-inputs" / "cron_jobs.json"
    cron_source.write_text(
        '[{"id":"different-live-job","cron":"0 0 * * *","command":"@MAGI health"}]\n',
        encoding="utf-8",
    )
    staging = tmp_path / "wrong-cron-source"

    with pytest.raises(
        deploy.DeployPrepareBlocked,
        match="does not match the release cron dispatch policy",
    ):
        deploy.prepare_deployment(
            release,
            staging,
            canonical_runtime_root,
            executable,
            cron_jobs_file=cron_source,
        )

    assert not staging.exists()


@pytest.mark.parametrize(
    ("release_options", "message"),
    [
        ({"include_cron_policy": False}, "cron dispatch policy is missing"),
        (
            {"policy_cron_sha256": "not-a-sha256"},
            "cron_jobs_sha256 must be lowercase SHA-256",
        ),
    ],
)
def test_production_cron_policy_missing_or_malformed_fails_before_staging(
    tmp_path: Path,
    canonical_runtime_root: Path,
    release_options: dict[str, object],
    message: str,
) -> None:
    release, executable = _release(tmp_path, **release_options)
    staging = tmp_path / "invalid-cron-policy"

    with pytest.raises(deploy.DeployPrepareBlocked, match=message):
        deploy.prepare_deployment(
            release,
            staging,
            canonical_runtime_root,
            executable,
        )

    assert not staging.exists()


def test_production_case_roots_and_canonical_mapping_fail_closed(
    tmp_path: Path,
    canonical_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, executable = _release(tmp_path)
    monkeypatch.delenv("MAGI_V3_CASE_ROOT")
    monkeypatch.delenv("MAGI_V3_ARCHIVE_ROOT")
    monkeypatch.delenv("MAGI_V3_PATH_MAPPINGS_JSON")

    with pytest.raises(deploy.DeployPrepareBlocked, match="case and archive roots"):
        deploy.prepare_deployment(
            release,
            tmp_path / "missing-roots",
            canonical_runtime_root,
            executable,
        )

    nas_root = tmp_path / "nas"
    case_root = nas_root / "01_案件"
    archive_root = nas_root / "10_結案"
    unrelated = tmp_path / "unrelated"
    for directory in (case_root, archive_root, unrelated):
        directory.mkdir(parents=True, exist_ok=True)
    with pytest.raises(deploy.DeployPrepareBlocked, match="not covered"):
        deploy.prepare_deployment(
            release,
            tmp_path / "uncovered-roots",
            canonical_runtime_root,
            executable,
            case_root=case_root,
            archive_root=archive_root,
            path_mappings=((str(unrelated), "Z:"),),
        )


def test_isolated_validation_rejects_secrets_live_inputs_and_enabled_cron(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    (
        validation_root, env_file, cron, website, laf_config, credentials,
        calendar_token, laf_token, file_review_token, ocr_queue,
    ) = _validation_inputs(tmp_path)
    env_file.write_text("MAGI_V3_VALIDATION_FIXTURE=1\nDISCORD_TOKEN=secret\n", encoding="utf-8")
    with pytest.raises(deploy.DeployPrepareBlocked, match="only the inert fixture marker"):
        deploy.prepare_deployment(
            release,
            tmp_path / "secret-staging",
            canonical_runtime_root,
            executable,
            deployment_mode="isolated_live_validation",
            validation_input_root=validation_root,
            env_file=env_file,
            cron_jobs_file=cron,
            website_root=website,
            laf_config_file=laf_config,
            google_credentials_file=credentials,
            google_calendar_token_file=calendar_token,
            laf_gmail_token_file=laf_token,
            file_review_token_file=file_review_token,
            nas_ocr_queue_db_path=ocr_queue,
        )
    assert not (tmp_path / "secret-staging").exists()

    env_file.write_bytes(deploy.VALIDATION_ENV_BYTES)
    cron.write_text(json.dumps([{**deploy.VALIDATION_CRON_JOB, "enabled": True}]), encoding="utf-8")
    with pytest.raises(deploy.DeployPrepareBlocked, match="disabled inert job"):
        deploy.prepare_deployment(
            release,
            tmp_path / "cron-staging",
            canonical_runtime_root,
            executable,
            deployment_mode="isolated_live_validation",
            validation_input_root=validation_root,
            env_file=env_file,
            cron_jobs_file=cron,
            website_root=website,
            laf_config_file=laf_config,
            google_credentials_file=credentials,
            google_calendar_token_file=calendar_token,
            laf_gmail_token_file=laf_token,
            file_review_token_file=file_review_token,
            nas_ocr_queue_db_path=ocr_queue,
        )
    assert not (tmp_path / "cron-staging").exists()


@pytest.mark.parametrize("missing_role", ["gateway", "control", "supervisor"])
def test_missing_entrypoint_is_blocked_exit_two_without_staging(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing_role: str,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path, missing_role=missing_role)
    staging = tmp_path / "blocked-staging"

    exit_code = deploy.main(
        [
            "--release-root",
            str(release),
            "--staging-dir",
            str(staging),
            "--runtime-root",
            str(canonical_runtime_root),
            "--python-executable",
            str(executable),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert report["status"] == "blocked"
    assert report["ready_to_install"] is False
    assert report["mutation_performed"] is False
    assert "required entrypoint is missing" in report["reason"]
    assert not staging.exists()


def test_missing_executable_and_tampered_release_manifest_fail_closed(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    executable.parent.chmod(0o755)
    executable.unlink()
    with pytest.raises(deploy.DeployPrepareBlocked, match="missing|contents differ from manifest"):
        deploy.prepare_deployment(
            release,
            tmp_path / "missing-executable",
            canonical_runtime_root,
            executable,
        )
    assert not (tmp_path / "missing-executable").exists()

    release, _release_executable = _release(tmp_path / "external")
    external_executable = tmp_path / "external-python3"
    _write(external_executable, "#!/bin/sh\nexit 0\n")
    external_executable.chmod(0o755)
    with pytest.raises(deploy.DeployPrepareBlocked, match="inside the immutable release root"):
        deploy.prepare_deployment(
            release,
            tmp_path / "external-executable",
            canonical_runtime_root,
            external_executable,
        )
    assert not (tmp_path / "external-executable").exists()

    release, executable = _release(tmp_path / "external-config")
    external_config = tmp_path / "external-roles.json"
    external_config.write_bytes((release / "config" / "v3_launchagent_roles.json").read_bytes())
    with pytest.raises(deploy.DeployPrepareBlocked, match="inside the immutable release root"):
        deploy.prepare_deployment(
            release,
            tmp_path / "external-config-staging",
            canonical_runtime_root,
            executable,
            roles_config=external_config,
        )
    assert not (tmp_path / "external-config-staging").exists()

    release, executable = _release(tmp_path / "executable-tampered")
    executable.chmod(0o755)
    executable.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    executable.chmod(0o755)
    with pytest.raises(deploy.DeployPrepareBlocked, match="hash"):
        deploy.prepare_deployment(
            release,
            tmp_path / "executable-tampered-staging",
            canonical_runtime_root,
            executable,
        )
    assert not (tmp_path / "executable-tampered-staging").exists()

    release, executable = _release(tmp_path / "extra-member")
    release.chmod(0o755)
    _write(release / "unmanifested.py", "raise SystemExit(9)\n")
    with pytest.raises(deploy.DeployPrepareBlocked, match="contents differ from manifest"):
        deploy.prepare_deployment(
            release,
            tmp_path / "extra-member-staging",
            canonical_runtime_root,
            executable,
        )
    assert not (tmp_path / "extra-member-staging").exists()

    release, executable = _release(tmp_path / "tampered")
    (release / deploy.RELEASE_MANIFEST_NAME).chmod(0o644)
    (release / deploy.RELEASE_MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
    with pytest.raises(deploy.DeployPrepareBlocked, match="release manifest"):
        deploy.prepare_deployment(
            release,
            tmp_path / "tampered-staging",
            canonical_runtime_root,
            executable,
        )
    assert not (tmp_path / "tampered-staging").exists()


def test_existing_staging_and_noncanonical_runtime_are_never_touched(
    tmp_path: Path,
    canonical_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, executable = _release(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "keep"
    sentinel.write_text("unchanged", encoding="utf-8")
    with pytest.raises(deploy.DeployPrepareBlocked, match="must not already exist"):
        deploy.prepare_deployment(
            release,
            existing,
            canonical_runtime_root,
            executable,
        )
    assert sentinel.read_text(encoding="utf-8") == "unchanged"

    with pytest.raises(deploy.DeployPrepareBlocked, match="canonical V3 runtime"):
        deploy.prepare_deployment(
            release,
            tmp_path / "safe-staging",
            canonical_runtime_root.parent / "MAGI_v2",
            executable,
    )
    assert not (tmp_path / "safe-staging").exists()
    assert not (canonical_runtime_root.parent / "MAGI_v2").exists()

    application_support = tmp_path / "staging-application-support"
    application_support.mkdir()
    monkeypatch.setattr(deploy, "_application_support_root", lambda: application_support)
    installed = deploy._canonical_installed_release_root("v3-test-release")
    installed.parent.mkdir(parents=True)
    shutil.copytree(release, installed)
    with pytest.raises(deploy.DeployPrepareBlocked, match="staging directory"):
        deploy.prepare_deployment(
            release,
            application_support / "deploy-staging",
            canonical_runtime_root,
            executable,
        )
    assert not (application_support / "deploy-staging").exists()


def test_external_python_runtime_must_exist_and_symlink_target_is_hash_bound(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    missing = tmp_path / "missing-python"
    with pytest.raises(deploy.DeployPrepareBlocked, match="external runtime input is missing"):
        deploy.prepare_deployment(
            release,
            tmp_path / "missing-runtime-staging",
            canonical_runtime_root,
            executable,
            python_runtime=missing,
        )
    assert not (tmp_path / "missing-runtime-staging").exists()

    base = tmp_path / "base-python"
    real = base / "bin" / "python"
    _write(real, "#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    _write(base / "INSTALL_RECEIPT.json", "{}\n")
    _write(base / "lib" / "python3.14" / "stdlib.py", "VALUE = 1\n")
    venv = tmp_path / "linked-venv"
    linked = venv / "bin" / "python"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(real)
    _write(
        venv / "pyvenv.cfg",
        "home = " + str(real.parent) + "\n"
        "include-system-site-packages = false\n"
        "executable = " + str(real) + "\n",
    )
    _write(venv / "lib" / "python3.14" / "site-packages" / "example.py", "VALUE = 1\n")
    staging = tmp_path / "linked-runtime-staging"
    deploy.prepare_deployment(
        release,
        staging,
        canonical_runtime_root,
        executable,
        python_runtime=linked,
    )
    plist_path = staging / "launchagents" / "com.magi.v3.gateway.plist"
    environment = plistlib.loads(plist_path.read_bytes())["EnvironmentVariables"]
    assert environment["MAGI_V3_PYTHON_RUNTIME"] == str(linked)
    assert environment["MAGI_V3_PYTHON_RUNTIME_REALPATH"] == str(real.resolve())
    assert environment["MAGI_V3_PYTHON_RUNTIME_SHA256"] == hashlib.sha256(
        real.read_bytes()
    ).hexdigest()


def test_external_cron_jobs_must_be_regular_json_list_with_unique_ids(
    tmp_path: Path,
    canonical_runtime_root: Path,
) -> None:
    release, executable = _release(tmp_path)
    duplicate = tmp_path / "duplicate-cron.json"
    _write(duplicate, '[{"id":"same"},{"id":"same"}]\n')
    with pytest.raises(deploy.DeployPrepareBlocked, match="ids must be unique"):
        deploy.prepare_deployment(
            release,
            tmp_path / "duplicate-cron-staging",
            canonical_runtime_root,
            executable,
            cron_jobs_file=duplicate,
        )
    assert not (tmp_path / "duplicate-cron-staging").exists()

    real = tmp_path / "real-cron.json"
    _write(real, '[{"id":"one"}]\n')
    linked = tmp_path / "linked-cron.json"
    linked.symlink_to(real)
    with pytest.raises(deploy.DeployPrepareBlocked, match="absolute non-symlink"):
        deploy.prepare_deployment(
            release,
            tmp_path / "linked-cron-staging",
            canonical_runtime_root,
            executable,
            cron_jobs_file=linked,
        )
    assert not (tmp_path / "linked-cron-staging").exists()
