from __future__ import annotations

import subprocess


def test_synology_drive_fallback_counts_as_homes_available(tmp_path, monkeypatch):
    from api import nas_mount_guard as mod

    fallback = tmp_path / "SynologyDrive-homes"
    fallback.mkdir()
    (fallback / "01_案件").mkdir()
    monkeypatch.setattr(mod, "_SYNOLOGY_DRIVE_CANDIDATES", (str(fallback),))
    monkeypatch.setattr(mod, "_is_mounted", lambda path: False)

    assert mod.get_synology_drive_fallback_path() == str(fallback)
    assert mod.get_share_available_path("homes", "/Volumes/homes") == str(fallback)
    assert mod.get_share_available_path("lumi", "/Volumes/lumi") == str(fallback)
    assert mod.get_share_mount_path("homes", "/Volumes/homes") == ""
    assert mod.get_share_status("lumi", "/Volumes/lumi")["mode"] == "synology_drive"
    assert mod.get_share_status("lumi", "/Volumes/lumi")["mounted"] is False


def test_ensure_nas_mounts_does_not_treat_fallback_as_smb_mount(tmp_path, monkeypatch):
    from api import nas_mount_guard as mod

    fallback = tmp_path / "SynologyDrive-homes"
    fallback.mkdir()
    (fallback / "01_案件").mkdir()
    attempts = []

    monkeypatch.setattr(mod, "_SHARES", [("lumi", "/Volumes/lumi")])
    monkeypatch.setattr(mod, "_SYNOLOGY_DRIVE_CANDIDATES", (str(fallback),))
    monkeypatch.setattr(mod, "_is_mounted", lambda path: False)
    monkeypatch.setattr(mod, "_ping_ok", lambda host, timeout=2: True)
    monkeypatch.setattr(mod, "resolve_nas_host", lambda: "192.0.2.10")
    monkeypatch.setattr(mod, "_cleanup_wrong_host_mounts", lambda: None)
    monkeypatch.setattr(mod, "_dispatch_transition_notifications", lambda results: None)
    monkeypatch.setattr(mod, "_LAST_MOUNT_ATTEMPT", {})
    monkeypatch.setenv("MAGI_NAS_MOUNT_RETRY_COOLDOWN_SEC", "0")

    def fake_mount(share_name, volume_path):
        attempts.append((share_name, volume_path))
        return False

    monkeypatch.setattr(mod, "_mount_share", fake_mount)

    assert mod.ensure_nas_mounts() == {"lumi": False}
    assert attempts == [("lumi", "/Volumes/lumi")]


def test_ensure_nas_mounts_ping_failure_returns_share_names(monkeypatch):
    from api import nas_mount_guard as mod

    monkeypatch.setattr(mod, "_SHARES", [("homes", "/Volumes/homes")])
    monkeypatch.setattr(mod, "resolve_nas_host", lambda: "192.0.2.10")
    monkeypatch.setattr(mod, "_ping_ok", lambda host, timeout=2: False)

    assert mod.ensure_nas_mounts() == {"homes": False}


def test_guard_loop_checks_before_first_sleep(monkeypatch):
    from api import nas_mount_guard as mod

    calls = []

    def fake_ensure():
        calls.append("ensure")

    def fake_sleep(interval):
        calls.append(f"sleep:{interval}")
        raise SystemExit

    monkeypatch.setattr(mod, "ensure_nas_mounts", fake_ensure)
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    try:
        mod._guard_loop(120)
    except SystemExit:
        pass

    assert calls == ["ensure", "sleep:120"]


def test_resolve_nas_user_prefers_explicit_user(monkeypatch):
    from api import nas_mount_guard as mod

    monkeypatch.setenv("MAGI_NAS_USER", "lumi63181107")
    monkeypatch.setenv("MAGI_NAS_HOME_USER", "home")

    assert mod.resolve_nas_user() == "lumi63181107"


def test_resolve_nas_user_falls_back_to_home_user(monkeypatch):
    from api import nas_mount_guard as mod

    monkeypatch.delenv("MAGI_NAS_USER", raising=False)
    monkeypatch.setenv("MAGI_NAS_HOME_USER", "lumi63181107")

    assert mod.resolve_nas_user() == "lumi63181107"


def test_mount_smbfs_fallback_never_puts_password_in_argv(tmp_path, monkeypatch):
    from api import nas_mount_guard as mod

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd and cmd[0] == "osascript":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        return subprocess.CompletedProcess(cmd, 1, "", "auth failed")

    monkeypatch.setenv("MAGI_NAS_ALLOW_CLI_MOUNT", "1")
    monkeypatch.setenv("MAGI_NAS_USER", "user")
    monkeypatch.setattr(mod, "NAS_HOST", "192.0.2.10")
    monkeypatch.setattr(mod, "_is_mounted", lambda path: False)
    monkeypatch.setattr(mod, "_force_unmount_stale", lambda path: None)
    monkeypatch.setattr(mod, "_ensure_volume_mount_point", lambda path: None)
    monkeypatch.setattr(mod.os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod._mount_share("lumi", str(tmp_path / "lumi")) is False
    mount_calls = [cmd for cmd in calls if cmd and cmd[0] == "mount_smbfs"]
    assert mount_calls
    assert all("//user:" not in " ".join(cmd) for cmd in mount_calls)
    assert all("//user@192.0.2.10/lumi" in cmd for cmd in mount_calls)
