from __future__ import annotations

import json
from pathlib import Path

from scripts.v3_cutover.core import assess_snapshot
from scripts.v3_cutover.probe import (
    DEFAULT_PORTS,
    ProcessInfo,
    ReleaseSpec,
    _host_singleton_process_identity,
    _launchd_status,
    _listener_pid_map,
    _release_process_tree,
    collect_snapshot,
    discover_release_spec,
)


def test_default_ports_cover_v2_v3_model_and_rpc_surfaces() -> None:
    assert DEFAULT_PORTS == (5002, 5003, 5014, 50052, 5102, 5103, 8188, 8080, 8081, 8082, 8083, 8088, 8090)


def test_listener_inventory_uses_one_bounded_nonblocking_lsof_pass(monkeypatch) -> None:
    class Result:
        returncode = 0
        stderr = "filesystem warnings are irrelevant to socket fields"
        stdout = "\n".join(
            (
                "p101",
                "f9",
                "n127.0.0.1:5002",
                "p102",
                "f10",
                "n[::1]:5003",
                "f11",
                "n*:8088",
                "p103",
                "f4",
                "n127.0.0.1:9999",
            )
        )

    calls = []
    monkeypatch.setattr("scripts.v3_cutover.probe.shutil.which", lambda name: "/usr/sbin/lsof")
    monkeypatch.setattr("scripts.v3_cutover.probe.Path.exists", lambda _path: True)
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._run_probe_command",
        lambda argv, **kwargs: calls.append((tuple(argv), kwargs)) or Result(),
    )

    owners, errors = _listener_pid_map((5002, 5003, 8088))

    assert errors == []
    assert owners == {5002: {101}, 5003: {102}, 8088: {102}}
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == (
        "/usr/sbin/lsof",
        "-b",
        "-nP",
        "-iTCP",
        "-sTCP:LISTEN",
        "-Fpn",
    )
    assert kwargs["timeout"] == 5


def test_snapshot_excludes_only_observer_chain_and_children_not_siblings(
    tmp_path: Path, monkeypatch
) -> None:
    v2 = tmp_path / "v2"
    v3 = tmp_path / "v3"
    v2.mkdir()
    v3.mkdir()
    specs = (
        ReleaseSpec("v2", v2, "ns-v2", pidfiles_required=False, launchd_labels_required=False),
        ReleaseSpec("v3", v3, "ns-v3", pidfiles_required=False, launchd_labels_required=False),
    )
    observer = 500
    monkeypatch.setattr("scripts.v3_cutover.probe.os.getpid", lambda: observer)
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._read_processes",
        lambda: (
            [
                ProcessInfo(400, 1, f"shell --release {v3}"),
                ProcessInfo(observer, 400, f"python {v3}/scripts/v3_pre_cutover.py"),
                ProcessInfo(501, observer, "/bin/ps -axo"),
                ProcessInfo(600, 400, f"python {v2}/daemon.py"),
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._listener_pid_map",
        lambda ports: ({int(port): set() for port in ports}, []),
    )
    snapshot = collect_snapshot(specs)

    process_owners = [owner for owner in snapshot.owners if owner.source == "process"]
    assert {owner.pid for owner in process_owners} == {600}
    assert process_owners[0].release == "v2"
    assert snapshot.metadata["observer_processes_excluded"] == [400, 500, 501]


def test_collect_snapshot_attributes_pid_port_launchd_and_lock_without_mutation(tmp_path: Path, monkeypatch) -> None:
    v2 = tmp_path / "v2"
    v3 = tmp_path / "v3"
    for root in (v2, v3):
        (root / ".runtime" / "locks").mkdir(parents=True)
    pidfile = v2 / ".runtime" / "daemon.pid"
    pidfile.write_text("101\n", encoding="utf-8")
    lockfile = v2 / ".runtime" / "locks" / "cron_scheduler_owner.lock.json"
    lockfile.write_text(json.dumps({"pid": 101, "domain": "cron_scheduler_owner", "owner": "test"}), encoding="utf-8")

    specs = (
        ReleaseSpec(
            "v2",
            v2,
            "ns-v2",
            pidfiles=(pidfile,),
            launchd_labels=("com.magi.v2",),
            ownership_files=(lockfile,),
        ),
        ReleaseSpec(
            "v3",
            v3,
            "ns-v3",
            pidfiles=(v3 / ".runtime" / "not-running.pid",),
            launchd_labels=("com.magi.v3",),
        ),
    )
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._read_processes",
        lambda: ([ProcessInfo(101, 1, f"python {v2}/daemon.py --namespace ns-v2")], []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._pid_alive", lambda pid: pid == 101)
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._listener_pid_map",
        lambda ports: ({int(port): ({101} if int(port) == 5002 else set()) for port in ports}, []),
    )
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._launchd_status",
        lambda label: ({"loaded": label.endswith("v2"), "pid": 101 if label.endswith("v2") else None}, None),
    )

    snapshot = collect_snapshot(specs)
    assessment = assess_snapshot(snapshot, expected="v2")
    assert assessment.go is False  # missing V3 pidfile is deliberately fail-closed preflight evidence
    assert assessment.active_releases == ("v2",)
    assert any(owner.source == "listener:5002" for owner in snapshot.owners)
    assert any(owner.source.startswith("ownership:") for owner in snapshot.owners)
    assert all("mutation" not in error for error in snapshot.probe_errors)


def test_unattributed_listener_is_no_go(tmp_path: Path, monkeypatch) -> None:
    v2 = tmp_path / "v2"
    v3 = tmp_path / "v3"
    v2.mkdir()
    v3.mkdir()
    specs = (
        ReleaseSpec("v2", v2, "ns-v2", pidfiles=(v2 / "none.pid",), launchd_labels=("v2",)),
        ReleaseSpec("v3", v3, "ns-v3", pidfiles=(v3 / "none.pid",), launchd_labels=("v3",)),
    )
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._read_processes", lambda: ([ProcessInfo(777, 1, "python unknown.py")], [])
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._pid_alive", lambda pid: False)
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._listener_pid_map",
        lambda ports: ({int(port): ({777} if int(port) == 5002 else set()) for port in ports}, []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._launchd_status", lambda label: ({"loaded": False}, None))
    snapshot = collect_snapshot(specs)
    assessment = assess_snapshot(snapshot)
    assert assessment.go is False
    assert any("unclassified active owner" in reason for reason in assessment.reasons)


def test_relative_listener_inherits_release_from_absolute_root_ancestor(tmp_path: Path, monkeypatch) -> None:
    v2 = tmp_path / "v2"
    v3 = tmp_path / "v3"
    v2.mkdir()
    v3.mkdir()
    specs = (
        ReleaseSpec("v2", v2, "ns-v2", pidfiles=(v2 / "daemon.pid",), launchd_labels=("v2",)),
        ReleaseSpec("v3", v3, "ns-v3", pidfiles=(v3 / "daemon.pid",), launchd_labels=("v3",)),
    )
    (v2 / "daemon.pid").write_text("800", encoding="utf-8")
    (v3 / "daemon.pid").write_text("999", encoding="utf-8")
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._read_processes",
        lambda: (
            [
                ProcessInfo(800, 1, f"python {v2}/run_daemon.py"),
                ProcessInfo(801, 800, "python api/server.py"),
            ],
            [],
        ),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._pid_alive", lambda pid: pid == 800)
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._listener_pid_map",
        lambda ports: ({int(port): ({801} if int(port) == 5002 else set()) for port in ports}, []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._launchd_status", lambda label: ({"loaded": False}, None))
    snapshot = collect_snapshot(specs)
    listeners = [owner for owner in snapshot.owners if owner.source == "listener:5002"]
    assert len(listeners) == 1
    assert listeners[0].release == "v2"
    assert listeners[0].ambiguous is False


def test_known_root_general_process_is_not_ambiguous(tmp_path: Path, monkeypatch) -> None:
    v2 = tmp_path / "v2"
    v3 = tmp_path / "v3"
    v2.mkdir()
    v3.mkdir()
    specs = (
        ReleaseSpec("v2", v2, "ns-v2", pidfiles=(v2 / "none.pid",), launchd_labels=("v2",)),
        ReleaseSpec("v3", v3, "ns-v3", pidfiles=(v3 / "none.pid",), launchd_labels=("v3",)),
    )
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._read_processes",
        lambda: ([ProcessInfo(901, 1, f"python {v2}/worker.py")], []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._pid_alive", lambda pid: False)
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._listener_pid_map",
        lambda ports: ({int(port): set() for port in ports}, []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._launchd_status", lambda label: ({"loaded": False}, None))
    snapshot = collect_snapshot(specs)
    process_owner = next(owner for owner in snapshot.owners if owner.source == "process")
    assert process_owner.release == "v2"
    assert process_owner.domain == "release"
    assert process_owner.ambiguous is False


def test_relative_process_is_attributed_from_full_inventory_cwd(tmp_path: Path, monkeypatch) -> None:
    v2 = tmp_path / "v2"
    v3 = tmp_path / "v3"
    (v2 / "services").mkdir(parents=True)
    v3.mkdir()
    specs = (
        ReleaseSpec("v2", v2, "ns-v2", pidfiles=(v2 / "none.pid",), launchd_labels=("v2",)),
        ReleaseSpec("v3", v3, "ns-v3", pidfiles=(v3 / "none.pid",), launchd_labels=("v3",)),
    )
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._read_processes",
        lambda: ([ProcessInfo(1001, 1, "python worker.py", str(v2 / "services"))], []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._pid_alive", lambda pid: False)
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._listener_pid_map",
        lambda ports: ({int(port): set() for port in ports}, []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._launchd_status", lambda label: ({"loaded": False}, None))

    snapshot = collect_snapshot(specs)

    process_owner = next(owner for owner in snapshot.owners if owner.source == "process")
    assert process_owner.release == "v2"


def test_browser_descendants_share_one_logical_owner_but_independent_roots_do_not(tmp_path: Path) -> None:
    v2 = tmp_path / "v2"
    v3 = tmp_path / "v3"
    v2.mkdir()
    v3.mkdir()
    specs = (
        ReleaseSpec("v2", v2, "ns-v2"),
        ReleaseSpec("v3", v3, "ns-v3"),
    )
    processes = (
        ProcessInfo(100, 1, "python review_worker.py", str(v2)),
        ProcessInfo(101, 100, "node node_modules/playwright/cli.js", str(v2)),
        ProcessInfo(102, 101, "chromium --type=browser", str(v2)),
        ProcessInfo(103, 102, "chromium --type=renderer", str(v2)),
        ProcessInfo(201, 100, "node node_modules/playwright/cli.js --second-session", str(v2)),
    )

    releases, anchors = _release_process_tree(processes, specs, {})

    assert all(releases[pid] == "v2" for pid in (100, 101, 102, 103, 201))
    assert anchors[101] == anchors[102] == anchors[103] == 101
    assert anchors[201] == 201
    assert anchors[100] == 100


def test_model_host_singletons_are_exactly_identified_and_counted(tmp_path: Path, monkeypatch) -> None:
    v2 = tmp_path / "v2"
    v3 = tmp_path / "v3"
    v2.mkdir()
    v3.mkdir()
    specs = (
        ReleaseSpec("v2", v2, "ns-v2", pidfiles=(v2 / "none.pid",), launchd_labels=("v2",)),
        ReleaseSpec("v3", v3, "ns-v3", pidfiles=(v3 / "none.pid",), launchd_labels=("v3",)),
    )
    command = (
        f"/usr/bin/python /opt/homebrew/bin/omlx serve --base-path {Path.home() / '.omlx'} --port 8080"
    )
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._read_processes",
        lambda: ([ProcessInfo(1101, 1, command), ProcessInfo(1102, 1, command)], []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._pid_alive", lambda pid: False)
    monkeypatch.setattr(
        "scripts.v3_cutover.probe._listener_pid_map",
        lambda ports: ({int(port): set() for port in ports}, []),
    )
    monkeypatch.setattr("scripts.v3_cutover.probe._launchd_status", lambda label: ({"loaded": False}, None))

    snapshot = collect_snapshot(specs)
    assessment = assess_snapshot(snapshot)

    assert len([owner for owner in snapshot.owners if owner.source == "process:host-singleton"]) == 2
    assert any("multiple model_host_8080 owners" in reason for reason in assessment.reasons)
    assert _host_singleton_process_identity("python /tmp/not-omlx-helper.py") is None


def test_launchctl_known_missing_service_is_safely_unloaded(monkeypatch) -> None:
    class Result:
        returncode = 113
        stdout = ""
        stderr = "Could not find service"

    monkeypatch.setattr("scripts.v3_cutover.probe.platform.system", lambda: "Darwin")
    monkeypatch.setattr("scripts.v3_cutover.probe.shutil.which", lambda name: "/bin/launchctl")
    monkeypatch.setattr("scripts.v3_cutover.probe.subprocess.run", lambda *args, **kwargs: Result())

    status, error = _launchd_status("com.magi.missing")

    assert status == {"loaded": False}
    assert error is None


def test_launchctl_permission_or_unknown_error_is_probe_error(monkeypatch) -> None:
    class Result:
        returncode = 1
        stdout = ""
        stderr = "Operation not permitted"

    monkeypatch.setattr("scripts.v3_cutover.probe.platform.system", lambda: "Darwin")
    monkeypatch.setattr("scripts.v3_cutover.probe.shutil.which", lambda name: "/bin/launchctl")
    monkeypatch.setattr("scripts.v3_cutover.probe.subprocess.run", lambda *args, **kwargs: Result())

    status, error = _launchd_status("com.magi.denied")

    assert status == {}
    assert "Operation not permitted" in str(error)


def test_exact_host_executable_with_spaces_is_recognized() -> None:
    executable = str(
        Path.home() / "Library" / "Application Support" / "MAGI" / "rpc-bin" / "rpc-server"
    )

    assert _host_singleton_process_identity(f"{executable} -H 127.0.0.1 -p 50052") == (
        "ingress",
        executable,
    )


def test_malformed_launchd_plist_is_preserved_as_fail_closed_probe_error(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.magi.broken.plist").write_text("not a plist", encoding="utf-8")
    root = tmp_path / "v2"
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))

    spec = discover_release_spec("v2", root, "ns-v2")

    assert any("plist unreadable" in error for error in spec.probe_errors)


def test_discover_v3_pidfiles_from_external_runtime_root(tmp_path: Path) -> None:
    release = tmp_path / "immutable-release"
    runtime = tmp_path / "runtime" / "MAGI_v3"
    release.mkdir()
    (runtime / "pids").mkdir(parents=True)
    control = runtime / "pids" / "control.pid"
    child = runtime / "pids" / "service-discord.pid"
    control.write_text('{"pid": 101}\n', encoding="utf-8")
    child.write_text('{"pid": 102}\n', encoding="utf-8")

    spec = discover_release_spec(
        "v3",
        release,
        "magi-v3-production",
        runtime_root=runtime,
        pidfiles_required=False,
        launchd_labels_required=False,
    )

    assert spec.pidfiles == (control, child)
    assert spec.pidfiles_required is False
