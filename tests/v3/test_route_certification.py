from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import scripts.v3_validation.route_certification as route_certification_module

from magi_v3.compat.gateway import RouteInventory
from scripts.v3_validation.route_certification import (
    ReleaseBinding,
    RuntimeBinding,
    _child_environment,
    _expected_external_storage_roots,
    _live_magi_root,
    _pytest_pythonpath,
    _seatbelt_attestation,
    _seatbelt_command,
    _write_seatbelt_profile,
    compile_route_evidence,
    load_trace_targets,
    main,
    runtime_binding_from_environment,
)
from scripts.v3_validation.route_reviews import RouteMethodKey


def test_actual_worker_protects_login_account_live_root_when_home_is_isolated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts.v3_validation import actual_route_replay as replay

    account = tmp_path / "login-account"
    observed = {}

    def run(argv, **kwargs):
        observed["argv"] = list(argv)
        observed["home"] = kwargs["env"]["HOME"]
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(replay, "account_home", lambda: account)
    monkeypatch.setattr(replay.subprocess, "run", run)
    monkeypatch.setenv("PYTHONPATH", _pytest_pythonpath())
    sandbox = tmp_path / "isolated"
    replay._run_worker(sandbox)

    live_root = account / "Library" / "Application Support" / "MAGI"
    assert observed["argv"][-2:] == ["--live-root", str(live_root)]
    assert observed["home"] == str(sandbox / "home")


def _dispositions(*, gap: RouteMethodKey) -> list[dict[str, object]]:
    inventory = RouteInventory.load()
    rows: list[dict[str, object]] = []
    for route in inventory.routes:
        for method in route.methods:
            key = RouteMethodKey(route.service, route.rule, method, route.endpoint)
            rows.append(
                {
                    "service": key.service,
                    "rule": key.rule,
                    "method": key.method,
                    "endpoint": key.endpoint,
                    "disposition": (
                        "validation_guard_only" if key == gap else "actual_handler_passed"
                    ),
                }
            )
    return rows


def _binding(tmp_path: Path) -> ReleaseBinding:
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    return ReleaseBinding(
        release_id="v3-route-test",
        release_sha="a" * 64,
        release_manifest=manifest,
        release_manifest_sha256="b" * 64,
        release_commit="c" * 40,
    )


def _runtime_binding(tmp_path: Path) -> RuntimeBinding:
    site_packages = tmp_path / "runtime" / "lib" / "python3.14" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    return RuntimeBinding(
        certifying=True,
        mode="formal_manifest_bound",
        python_runtime=str(tmp_path / "runtime" / "bin" / "python"),
        python_runtime_realpath=str(tmp_path / "base" / "bin" / "python3"),
        python_runtime_sha256="1" * 64,
        runtime_manifest=str(tmp_path / "python-runtime-manifest.json"),
        runtime_manifest_sha256="2" * 64,
        runtime_tree_sha256="3" * 64,
        runtime_root=str(tmp_path / "runtime"),
        base_runtime_root=str(tmp_path / "base"),
        pythonpath_roots=(Path(__file__).resolve().parents[2], site_packages),
    )


def _trace_safety(
    workspace: Path, *, external_storage_attempts: int = 0
) -> dict[str, object]:
    isolation_attempts = (
        {"external_storage_access": external_storage_attempts}
        if external_storage_attempts
        else {}
    )
    return {
        "isolation_attempts": isolation_attempts,
        "external_storage_roots": [
            str(root) for root in _expected_external_storage_roots()
        ],
        "external_storage_access_attempts": external_storage_attempts,
        "seatbelt": _seatbelt_attestation(workspace),
    }


def _base_safety(workspace: Path) -> dict[str, object]:
    return {
        "safe_execution": True,
        "nas_accessed": False,
        "external_storage_roots": [
            str(root) for root in _expected_external_storage_roots()
        ],
        "external_storage_access_attempts": 0,
        "isolation_attempts": {"external_storage_access": 0},
        "seatbelt": _seatbelt_attestation(workspace),
    }


def test_reviewed_passing_trace_promotes_real_handler_success_not_guard(tmp_path: Path) -> None:
    key = RouteMethodKey(
        "5002",
        "/api/osc/archive-wizard/execute",
        "POST",
        "osc_cases.osc_archive_wizard_execute_api",
    )
    nodeid = "tests/test_archive_wizard_execute.py::test_archive_execute_uses_selected_case_lookup"
    report = compile_route_evidence(
        {
            "execution_passed": True,
            "evidence_sha256": "d" * 64,
            "safety": _base_safety(tmp_path),
            "route_method_dispositions": _dispositions(gap=key),
        },
        {
            "pytest_exit_status": 0,
            **_trace_safety(tmp_path),
            "observations": [
                {
                    "service": "5002",
                    "rule": key.rule,
                    "method": key.method,
                    "endpoint": key.endpoint,
                    "status": 200,
                    "content_type": "application/json",
                    "location_path": "",
                    "test_nodeid": nodeid,
                    "success_assertion_lines": [
                        "/release/tests/test_archive_wizard_execute.py:61"
                    ],
                }
            ],
        },
        {key: nodeid},
        _binding(tmp_path),
        runtime_binding=_runtime_binding(tmp_path),
    )

    assert report["passed"] is True
    assert report["status"] == "passed"
    assert report["coverage_complete"] is True
    assert report["trace_promotions"] == 1
    promoted = next(
        row
        for row in report["route_method_dispositions"]
        if (row["service"], row["rule"], row["method"], row["endpoint"])
        == (key.service, key.rule, key.method, key.endpoint)
    )
    assert promoted["disposition"] == "actual_handler_passed"
    assert promoted["reason_code"] == "REVIEWED_PYTEST_REPRESENTATIVE_SUCCESS_PATH"
    assert promoted["side_effect_class"] == "destructive"


def test_validation_guard_cannot_be_promoted_by_passing_test(tmp_path: Path) -> None:
    key = RouteMethodKey("5002", "/login", "POST", "login")
    nodeid = "tests/test_incidental.py::test_request"
    report = compile_route_evidence(
        {
            "execution_passed": True,
            "evidence_sha256": "d" * 64,
            "safety": _base_safety(tmp_path),
            "route_method_dispositions": _dispositions(gap=key),
        },
        {
            "pytest_exit_status": 0,
            **_trace_safety(tmp_path),
            "observations": [
                {
                    "rule": key.rule,
                    "method": key.method,
                    "endpoint": key.endpoint,
                    "status": 400,
                    "content_type": "text/html",
                    "location_path": "",
                    "test_nodeid": nodeid,
                    "success_assertion_lines": ["/release/tests/test_incidental.py:2"],
                }
            ],
        },
        {key: nodeid},
        _binding(tmp_path),
        runtime_binding=_runtime_binding(tmp_path),
    )

    assert report["passed"] is False
    assert report["status"] == "failed"
    assert report["trace_promotions"] == 0
    assert report["remaining_route_methods"] == 1
    assert report["trace_rejections"][0]["reason"] == "not_success_status"


def test_unpinned_success_trace_cannot_promote_route_method(tmp_path: Path) -> None:
    key = RouteMethodKey(
        "5002",
        "/api/osc/archive-wizard/execute",
        "POST",
        "osc_cases.osc_archive_wizard_execute_api",
    )
    nodeid = "tests/test_archive_wizard_execute.py::test_archive_execute_uses_selected_case_lookup"
    report = compile_route_evidence(
        {
            "execution_passed": True,
            "evidence_sha256": "d" * 64,
            "safety": _base_safety(tmp_path),
            "route_method_dispositions": _dispositions(gap=key),
        },
        {
            "pytest_exit_status": 0,
            **_trace_safety(tmp_path),
            "observations": [
                {
                    "rule": key.rule,
                    "method": key.method,
                    "endpoint": key.endpoint,
                    "status": 200,
                    "content_type": "application/json",
                    "location_path": "",
                    "test_nodeid": nodeid,
                    "success_assertion_lines": [
                        "/release/tests/test_archive_wizard_execute.py:61"
                    ],
                }
            ],
        },
        {},
        _binding(tmp_path),
        runtime_binding=_runtime_binding(tmp_path),
    )

    assert report["passed"] is False
    assert report["trace_promotions"] == 0
    assert report["trace_rejections"][0]["reason"] == "success_proof_not_pinned"


def test_external_storage_attempt_counter_cannot_certify_route_success(
    tmp_path: Path,
) -> None:
    key = RouteMethodKey(
        "5002",
        "/api/osc/archive-wizard/execute",
        "POST",
        "osc_cases.osc_archive_wizard_execute_api",
    )
    nodeid = "tests/test_archive_wizard_execute.py::test_archive_execute_uses_selected_case_lookup"
    report = compile_route_evidence(
        {
            "execution_passed": True,
            "evidence_sha256": "d" * 64,
            "safety": _base_safety(tmp_path),
            "route_method_dispositions": _dispositions(gap=key),
        },
        {
            "pytest_exit_status": 0,
            **_trace_safety(tmp_path, external_storage_attempts=1),
            "observations": [
                {
                    "rule": key.rule,
                    "method": key.method,
                    "endpoint": key.endpoint,
                    "status": 200,
                    "content_type": "application/json",
                    "location_path": "",
                    "test_nodeid": nodeid,
                    "success_assertion_lines": [
                        "/release/tests/test_archive_wizard_execute.py:61"
                    ],
                }
            ],
        },
        {key: nodeid},
        _binding(tmp_path),
        runtime_binding=_runtime_binding(tmp_path),
    )

    assert report["coverage_complete"] is True
    assert report["status"] == "failed"
    assert report["passed"] is False
    assert report["safety"]["nas_accessed"] is True
    assert report["safety"]["external_storage_attested"] is False
    assert report["safety"]["external_storage_access_attempts"] == 1


def test_cli_emits_campaign_evidence_prefix_and_uses_isolated_state_workspace(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    observed: dict[str, object] = {}

    def run(workspace: Path, *, python: Path) -> dict[str, object]:
        observed.update(workspace=workspace, python=python)
        return {"passed": True, "status": "passed"}

    monkeypatch.setattr(
        "scripts.v3_validation.route_certification.run_route_certification", run
    )
    monkeypatch.setenv("MAGI_V3_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MAGI_V3_VALIDATION_PROFILE_ID", "ordinary-week")

    assert main([]) == 0
    output = capsys.readouterr().out.strip()
    assert output.startswith("MAGI_V3_OFFLINE_EVIDENCE=")
    assert observed["workspace"] == (
        tmp_path / "state" / "route-certification" / "ordinary-week"
    )


def test_every_pinned_success_proof_target_is_in_the_release_allowlist() -> None:
    from scripts.v3_release_bundle import REQUIRED_TEST_TARGETS, SOURCE_DIRECTORIES

    files = {target.split("::", 1)[0] for target in load_trace_targets()}
    missing = {
        path
        for path in files
        if path not in REQUIRED_TEST_TARGETS
        and not any(path.startswith(f"{directory}/") for directory in SOURCE_DIRECTORIES)
    }

    assert not missing


def test_trace_plugin_records_only_success_from_a_passing_test(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_fixture_route.py"
    test_file.parent.mkdir()
    test_file.write_text(
        """
from flask import Flask, jsonify

def test_success_and_guard():
    app = Flask(__name__)
    app.add_url_rule('/ok', 'fixture.ok', lambda: jsonify(ok=True))
    app.add_url_rule('/guard', 'fixture.guard', lambda: (jsonify(error='bad'), 400))
    client = app.test_client()
    assert client.get('/ok').status_code == 200
    assert client.get('/guard').status_code == 400
""".lstrip(),
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONPATH": _pytest_pythonpath(),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "MAGI_V3_ROUTE_TRACE_FILE": str(trace),
        "MAGI_V3_ROUTE_TRACE_SANDBOX": str(tmp_path),
        "MAGI_V3_ROUTE_TRACE_LIVE_ROOT": str(
            Path.home() / "Library" / "Application Support" / "MAGI"
        ),
    }
    (tmp_path / "home").mkdir()
    seatbelt_profile = _write_seatbelt_profile(tmp_path)
    result = subprocess.run(
        _seatbelt_command(
            seatbelt_profile,
                [
                    sys.executable,
                    "-S",
                    "-m",
                "pytest",
                "-q",
                "-p",
                "scripts.v3_validation.route_success_trace_plugin",
                "-p",
                "no:cacheprovider",
                str(test_file),
            ],
        ),
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    evidence = json.loads(trace.read_text(encoding="utf-8"))
    assert evidence["pytest_exit_status"] == 0
    assert evidence["isolation_attempts"] == {}
    assert evidence["external_storage_roots"] == [
        str(root) for root in _expected_external_storage_roots()
    ]
    assert evidence["external_storage_access_attempts"] == 0
    assert [(row["rule"], row["status"]) for row in evidence["observations"]] == [
        ("/ok", 200)
    ]
    assert evidence["observations"][0]["success_assertion_lines"]


@pytest.mark.parametrize("external_root", _expected_external_storage_roots())
def test_trace_plugin_blocks_reads_from_external_storage_roots(
    tmp_path: Path,
    external_root: Path,
) -> None:
    test_file = tmp_path / "tests" / "test_external_storage_read.py"
    test_file.parent.mkdir()
    target = external_root / "magi-route-certification-forbidden-read"
    test_file.write_text(
        (
            "from pathlib import Path\n\n"
            "def test_external_read_is_blocked():\n"
            "    try:\n"
            f"        Path({str(target)!r}).read_bytes()\n"
            "    except RuntimeError as exc:\n"
            "        assert 'external_storage_access' in str(exc)\n"
            "    else:\n"
            "        raise AssertionError('external storage read was not blocked')\n"
        ),
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONPATH": _pytest_pythonpath(),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "MAGI_V3_ROUTE_TRACE_FILE": str(trace),
        "MAGI_V3_ROUTE_TRACE_SANDBOX": str(tmp_path),
        "MAGI_V3_ROUTE_TRACE_LIVE_ROOT": str(
            Path.home() / "Library" / "Application Support" / "MAGI"
        ),
    }
    (tmp_path / "home").mkdir()
    seatbelt_profile = _write_seatbelt_profile(tmp_path)
    result = subprocess.run(
        _seatbelt_command(
            seatbelt_profile,
                [
                    sys.executable,
                    "-S",
                    "-m",
                "pytest",
                "-q",
                "-p",
                "scripts.v3_validation.route_success_trace_plugin",
                "-p",
                "no:cacheprovider",
                str(test_file),
            ],
        ),
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    evidence = json.loads(trace.read_text(encoding="utf-8"))
    assert evidence["pytest_exit_status"] == 0
    assert evidence["external_storage_access_attempts"] == 1
    assert evidence["isolation_attempts"] == {"external_storage_access": 1}
    assert len(evidence["unsafe_test_nodeids"]) == 1
    assert evidence["unsafe_test_nodeids"][0].endswith(
        "test_external_storage_read.py::test_external_read_is_blocked"
    )
    assert evidence["observations"] == []


@pytest.mark.parametrize(
    "probe_code",
    [
        "from pathlib import Path; assert Path('/Volumes').exists() is False",
        (
            "import errno,os; "
            "\ntry: os.stat('/Volumes')\n"
            "except OSError as exc: assert exc.errno in {errno.EPERM,errno.EACCES}\n"
            "else: raise AssertionError('Seatbelt allowed os.stat')"
        ),
        "import os; assert os.access('/Volumes', os.R_OK) is False",
        (
            "import errno,os; "
            "\ntry: os.readlink('/Volumes/Macintosh HD')\n"
            "except OSError as exc: assert exc.errno in {errno.EPERM,errno.EACCES}\n"
            "else: raise AssertionError('Seatbelt allowed os.readlink')"
        ),
        (
            "import sqlite3; "
            "\ntry: sqlite3.connect('file:/Volumes/magi-seatbelt-never.sqlite?mode=ro', uri=True)\n"
            "except sqlite3.OperationalError: pass\n"
            "else: raise AssertionError('Seatbelt allowed sqlite external storage')"
        ),
    ],
    ids=("exists", "stat", "access", "readlink", "sqlite"),
)
def test_seatbelt_denies_metadata_and_sqlite_external_storage_reads(
    tmp_path: Path,
    probe_code: str,
) -> None:
    profile = _write_seatbelt_profile(tmp_path)
    result = subprocess.run(
        _seatbelt_command(
            profile,
            [sys.executable, "-I", "-S", "-c", probe_code + "\nprint('DENIED')"],
        ),
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "DENIED"


def test_seatbelt_denies_symlink_alias_to_external_storage(tmp_path: Path) -> None:
    profile = _write_seatbelt_profile(tmp_path)
    alias = tmp_path / "volumes-alias"
    code = (
        "import errno,os,sys; "
        "os.symlink('/Volumes', sys.argv[1]); "
        "\ntry: os.stat(sys.argv[1])\n"
        "except OSError as exc: assert exc.errno in {errno.EPERM,errno.EACCES}\n"
        "else: raise AssertionError('Seatbelt allowed symlink alias read')\n"
        "print('DENIED')"
    )
    result = subprocess.run(
        _seatbelt_command(
            profile,
            [sys.executable, "-I", "-S", "-c", code, str(alias)],
        ),
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "DENIED"


@pytest.mark.parametrize(
    "probe_code",
    [
        (
            "import errno,socket; "
            "sock=socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
            "\ntry: sock.connect(('127.0.0.1',9))\n"
            "except OSError as exc: assert exc.errno in {errno.EPERM,errno.EACCES}\n"
            "else: raise AssertionError('Seatbelt allowed a Python network connect')"
        ),
        (
            "import ctypes,errno,socket,struct; "
            "libc=ctypes.CDLL(None,use_errno=True); "
            "fd=libc.socket(socket.AF_INET,socket.SOCK_STREAM,0); "
            "assert fd >= 0; "
            "raw=struct.pack('BBH4s8s',16,socket.AF_INET,socket.htons(9),"
            "socket.inet_aton('127.0.0.1'),bytes(8)); "
            "addr=ctypes.create_string_buffer(raw); "
            "rc=libc.connect(fd,addr,len(raw)); "
            "saved=ctypes.get_errno(); libc.close(fd); "
            "assert rc == -1 and saved in {errno.EPERM,errno.EACCES}"
        ),
    ],
    ids=("python-socket", "native-c-socket"),
)
def test_seatbelt_denies_python_and_native_network_sockets(
    tmp_path: Path, probe_code: str
) -> None:
    profile = _write_seatbelt_profile(tmp_path)
    result = subprocess.run(
        _seatbelt_command(
            profile,
            [sys.executable, "-I", "-S", "-c", probe_code + "\nprint('DENIED')"],
        ),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "DENIED"


def test_seatbelt_denies_live_reads_and_writes_and_non_workspace_writes(
    tmp_path: Path,
) -> None:
    profile = _write_seatbelt_profile(tmp_path)
    live_root = _live_magi_root()
    live_mutable_root = live_root / "runtime"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-write"
    code = (
        "import errno,os,sys; denied={errno.EPERM,errno.EACCES}; "
        "targets=[('stat',sys.argv[1]),('write',sys.argv[2]),('write',sys.argv[3]),('write',sys.argv[4])]; "
        "results=[]; "
        "exec(\"for operation,path in targets:\\n"
        " try:\\n"
        "  os.stat(path) if operation == 'stat' else open(path,'wb').close()\\n"
        " except OSError as exc:\\n  results.append(exc.errno in denied)\\n"
        " else:\\n  results.append(False)\"); "
        "assert all(results),results; print('DENIED')"
    )
    result = subprocess.run(
        _seatbelt_command(
            profile,
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                code,
                str(live_mutable_root),
                str(live_root / "route-certification-forbidden"),
                os.path.join(os.sep, "Volumes", "route-certification-forbidden"),
                str(outside),
            ],
        ),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "DENIED"
    assert not outside.exists()


def test_child_environment_does_not_inherit_magi_path_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    decoy = tmp_path / "decoy-nas"
    monkeypatch.setenv("MAGI_NAS_HOMES_MOUNT", str(decoy))
    monkeypatch.setenv("MAGI_DRIVE_ROOT", str(tmp_path / "decoy-drive"))
    monkeypatch.setenv("MAGI_ROOT_DIR", "/parent/live/override")

    environment = _child_environment(tmp_path / "child")

    assert "MAGI_NAS_HOMES_MOUNT" not in environment
    assert "MAGI_DRIVE_ROOT" not in environment
    assert environment["MAGI_ROOT_DIR"] == str(tmp_path / "child" / "magi-root")
    assert _seatbelt_attestation(tmp_path)["path_overrides_inherited"] is False


def test_seatbelt_attestation_sha_is_bound_to_exact_workspace(tmp_path: Path) -> None:
    first = _seatbelt_attestation(tmp_path / "first")
    second = _seatbelt_attestation(tmp_path / "second")

    assert first["allowed_write_roots"] == [str((tmp_path / "first").resolve())]
    assert first["profile_sha256"] != second["profile_sha256"]
    assert first["network_denied"] is True
    assert first["live_magi_write_denied"] is True
    assert first["live_magi_mutable_read_write_denied"] is True
    assert first["live_magi_immutable_read_roots"] == [
        str(_live_magi_root() / "releases"),
        str(_live_magi_root() / "runtimes"),
    ]
    assert first["external_storage_read_write_denied"] is True
    assert first["workspace_only_write"] is True


def test_source_route_evidence_is_explicitly_noncertifying(tmp_path: Path) -> None:
    key = RouteMethodKey(
        "5002", "/api/osc/archive-wizard/execute", "POST",
        "osc_cases.osc_archive_wizard_execute_api",
    )
    nodeid = "tests/test_archive_wizard_execute.py::test_archive_execute_uses_selected_case_lookup"
    report = compile_route_evidence(
        {
            "execution_passed": True,
            "evidence_sha256": "d" * 64,
            "safety": _base_safety(tmp_path),
            "route_method_dispositions": _dispositions(gap=key),
        },
        {
            "pytest_exit_status": 0,
            **_trace_safety(tmp_path),
            "observations": [{
                "rule": key.rule,
                "method": key.method,
                "endpoint": key.endpoint,
                "status": 200,
                "content_type": "application/json",
                "location_path": "",
                "test_nodeid": nodeid,
                "success_assertion_lines": ["/release/tests/test.py:1"],
            }],
        },
        {key: nodeid},
        _binding(tmp_path),
    )

    assert report["coverage_complete"] is True
    assert report["certifying"] is False
    assert report["diagnostic_passed"] is True
    assert report["status"] == "diagnostic_passed"
    assert report["passed"] is False
    assert report["runtime_binding"]["parent_sys_path_inherited"] is False


def _formal_runtime_environment(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    runtime_root = tmp_path / "runtime"
    base_root = tmp_path / "base-runtime"
    site_packages = runtime_root / "lib" / "python3.14" / "site-packages"
    base_site_packages = base_root / "lib" / "python3.14" / "site-packages"
    site_packages.mkdir(parents=True)
    base_site_packages.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "runtime_root": str(runtime_root),
        "base_runtime_root": str(base_root),
        "python_runtime": str(Path(sys.executable)),
        "python_runtime_realpath": str(Path(sys.executable).resolve()),
        "python_runtime_sha256": "1" * 64,
        "tree_sha256": "3" * 64,
        "directories": [{"path": "lib/python3.14/site-packages", "mode": "0555"}],
        "base_directories": [{"path": "lib/python3.14/site-packages", "mode": "0555"}],
    }
    manifest = tmp_path / "python-runtime-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    values = {
        "MAGI_V3_ROUTE_CERTIFYING": "1",
        "MAGI_V3_PYTHON_RUNTIME": str(Path(sys.executable)),
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": str(Path(sys.executable).resolve()),
        "MAGI_V3_PYTHON_RUNTIME_SHA256": "1" * 64,
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST": str(manifest),
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256": hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest(),
        "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256": "3" * 64,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        route_certification_module,
        "verify_runtime_manifest",
        lambda *args, **kwargs: {"tree_sha256": "3" * 64},
    )
    return manifest, site_packages


def test_formal_runtime_pythonpath_ignores_parent_sys_path(
    tmp_path: Path, monkeypatch
) -> None:
    _manifest, site_packages = _formal_runtime_environment(tmp_path, monkeypatch)
    unbound = tmp_path / "unbound-parent-site-packages"
    unbound.mkdir()
    monkeypatch.setattr(sys, "path", [str(unbound), *sys.path])

    binding = runtime_binding_from_environment(Path(sys.executable))

    assert binding.certifying is True
    assert binding.pythonpath_roots[0] == Path(__file__).resolve().parents[2]
    assert site_packages in binding.pythonpath_roots
    assert unbound not in binding.pythonpath_roots
    assert str(unbound) not in _pytest_pythonpath(binding)


def test_formal_child_environment_drives_actual_worker_from_verified_binding(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts.v3_validation import actual_route_replay as replay

    _manifest, site_packages = _formal_runtime_environment(tmp_path, monkeypatch)
    binding = runtime_binding_from_environment(Path(sys.executable))
    for name in (
        "MAGI_V3_PYTHON_RUNTIME",
        "MAGI_V3_PYTHON_RUNTIME_REALPATH",
        "MAGI_V3_PYTHON_RUNTIME_SHA256",
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST",
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
        "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256",
    ):
        monkeypatch.setenv(name, "poisoned-parent-value")

    child = _child_environment(
        tmp_path / "formal-child", runtime_binding=binding
    )
    assert child["MAGI_V3_ROUTE_CERTIFYING"] == "1"
    assert child["MAGI_V3_PYTHON_RUNTIME"] == binding.python_runtime
    assert child["MAGI_V3_PYTHON_RUNTIME_MANIFEST"] == binding.runtime_manifest
    assert child["MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256"] == (
        binding.runtime_manifest_sha256
    )
    assert "poisoned-parent-value" not in child.values()

    monkeypatch.setattr(replay.os, "environ", child.copy())
    monkeypatch.setattr(
        replay,
        "verify_runtime_manifest",
        lambda *args, **kwargs: {"tree_sha256": binding.runtime_tree_sha256},
    )
    worker_python, roots = replay._worker_runtime()
    nested = replay._worker_environment(
        tmp_path / "actual-worker",
        worker_python=worker_python,
        python_roots=roots,
    )

    assert worker_python.resolve() == Path(sys.executable).resolve()
    assert site_packages.resolve() in roots
    assert all(
        nested[name] == child[name]
        for name in replay.FORMAL_RUNTIME_ENVIRONMENT_KEYS
    )
    assert nested["HOME"] == str(tmp_path / "actual-worker" / "home")


def test_actual_worker_rechecks_formal_manifest_sha_after_child_handoff(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts.v3_validation import actual_route_replay as replay

    manifest, _site_packages = _formal_runtime_environment(tmp_path, monkeypatch)
    binding = runtime_binding_from_environment(Path(sys.executable))
    child = _child_environment(
        tmp_path / "formal-child", runtime_binding=binding
    )
    monkeypatch.setattr(replay.os, "environ", child.copy())
    monkeypatch.setattr(
        replay,
        "verify_runtime_manifest",
        lambda *args, **kwargs: {"tree_sha256": binding.runtime_tree_sha256},
    )
    manifest.write_bytes(manifest.read_bytes() + b"\n")

    with pytest.raises(Exception, match="manifest SHA-256 drifted"):
        replay._worker_runtime()


def test_actual_worker_rechecks_formal_runtime_tree_after_child_handoff(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts.v3_python_runtime_snapshot import PythonRuntimeBlocked
    from scripts.v3_validation import actual_route_replay as replay

    _manifest, _site_packages = _formal_runtime_environment(tmp_path, monkeypatch)
    binding = runtime_binding_from_environment(Path(sys.executable))
    child = _child_environment(
        tmp_path / "formal-child", runtime_binding=binding
    )
    monkeypatch.setattr(replay.os, "environ", child.copy())

    def reject_tree(*_args, **_kwargs):
        raise PythonRuntimeBlocked("runtime tree drift")

    monkeypatch.setattr(replay, "verify_runtime_manifest", reject_tree)

    with pytest.raises(Exception, match="runtime tree drift"):
        replay._worker_runtime()


def test_formal_runtime_manifest_sha_tamper_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    _formal_runtime_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256", "0" * 64)

    with pytest.raises(Exception, match="manifest SHA-256 drifted"):
        runtime_binding_from_environment(Path(sys.executable))


def test_formal_runtime_user_site_fails_closed(tmp_path: Path, monkeypatch) -> None:
    manifest, _site_packages = _formal_runtime_environment(tmp_path, monkeypatch)
    user_site = tmp_path / "user-library" / "site-packages"
    user_site.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        route_certification_module.site,
        "getusersitepackages",
        lambda: str(user_site),
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["runtime_root"] = str(user_site.parent)
    payload["directories"] = [{"path": "site-packages", "mode": "0755"}]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )

    with pytest.raises(Exception, match="cannot include user site-packages"):
        runtime_binding_from_environment(Path(sys.executable))
