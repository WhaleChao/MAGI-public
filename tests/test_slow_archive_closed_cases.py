from __future__ import annotations


def test_closed_rows_includes_closed_active_residue(monkeypatch):
    from scripts.ops import slow_archive_closed_cases as mod

    captured = {}

    def fake_exec(sql, params=(), fetch="none"):
        captured["sql"] = sql
        captured["params"] = params
        captured["fetch"] = fetch
        return (
            [
                {
                    "id": "case-1",
                    "case_number": "2025-0002",
                    "client_name": "游秀鈴",
                    "status": "已結案",
                    "legal_aid_status": "已結案，待送出",
                    "folder_path": r"Z:\lumi63181107\01_案件\法扶案件\刑事\2025-0002-游秀鈴-一審-傷害致死",
                }
            ],
            None,
        )

    monkeypatch.setattr(mod, "_osc_exec", fake_exec)

    rows = mod._closed_rows(case_number="2025-0002", limit=1)

    assert rows and rows[0]["case_number"] == "2025-0002"
    assert "03_工作資料" in captured["sql"]
    assert "01_案件" in captured["sql"]
    assert captured["params"] == ("2025-0002", 1)


def test_active_source_for_active_path_prefers_smb_over_cloud(monkeypatch, tmp_path):
    from scripts.ops import slow_archive_closed_cases as mod

    cloud = tmp_path / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件" / "case"
    smb = tmp_path / "Volumes" / "homes" / "lumi63181107" / "01_案件" / "case"
    cloud.mkdir(parents=True)
    smb.mkdir(parents=True)

    monkeypatch.setattr(mod, "local_case_path_candidates", lambda _path: [str(cloud), str(smb)])
    monkeypatch.setattr(mod, "_is_dir_quick", lambda path: path.exists() and path.is_dir())

    row = {"folder_path": r"Z:\lumi63181107\01_案件\case", "case_number": "2025-0002"}

    assert mod._active_source_for(row) == str(smb)


def test_active_source_for_closed_path_prefers_smb_residue(monkeypatch, tmp_path):
    from scripts.ops import slow_archive_closed_cases as mod

    cloud = tmp_path / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件" / "法扶案件" / "刑事" / "case"
    smb = tmp_path / "Volumes" / "homes" / "lumi63181107" / "01_案件" / "法扶案件" / "刑事" / "case"
    cloud.mkdir(parents=True)
    smb.mkdir(parents=True)

    monkeypatch.setattr(mod, "_active_candidates_for_closed_path", lambda _path: [str(cloud), str(smb)])
    monkeypatch.setattr(mod, "_is_dir_quick", lambda path: path.exists() and path.is_dir())

    row = {
        "folder_path": r"Y:\lumi\03_工作資料\10_結案\法扶案件\刑事\case",
        "case_number": "2025-0002",
    }

    assert mod._active_source_for(row) == str(smb)


def test_run_rsync_sets_io_timeout(monkeypatch, tmp_path):
    from scripts.ops import slow_archive_closed_cases as mod

    captured: dict[str, list[str]] = {}

    class FakeProc:
        returncode = 0

        def __init__(self, cmd, **_kwargs):
            captured["cmd"] = list(cmd)

        def communicate(self, timeout=None):
            captured["communicate_timeout"] = [str(timeout)]
            return ("", "")

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/rsync" if name == "rsync" else None)
    monkeypatch.setattr(mod.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(mod, "_rsync_append_flag", lambda _rsync: "--append")

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    result = mod._run_rsync(
        src,
        dst,
        dry_run=False,
        bwlimit_mbps=25,
        timeout_sec=1800,
        rsync_timeout_sec=600,
    )

    assert result["ok"] is True
    assert "--bwlimit=25600" in captured["cmd"]
    assert "--timeout=600" in captured["cmd"]
    assert "--append" in captured["cmd"]
    assert captured["communicate_timeout"] == ["1800"]


def test_rsync_append_flag_prefers_verify_when_available(monkeypatch):
    from scripts.ops import slow_archive_closed_cases as mod

    class FakeRun:
        stdout = "--append --append-verify"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *_args, **_kwargs: FakeRun())
    assert mod._rsync_append_flag("/usr/bin/rsync") == "--append-verify"


def test_rsync_append_flag_falls_back_to_append(monkeypatch):
    from scripts.ops import slow_archive_closed_cases as mod

    class FakeRun:
        stdout = "--append"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *_args, **_kwargs: FakeRun())
    assert mod._rsync_append_flag("/usr/bin/rsync") == "--append"


def test_server_side_rsync_command_uses_env_mapping(monkeypatch):
    from scripts.ops import slow_archive_closed_cases as mod

    monkeypatch.setenv("MAGI_SLOW_ARCHIVE_SERVER_SIDE", "1")
    monkeypatch.setenv("MAGI_NAS_SSH_HOST", "192.168.1.3")
    monkeypatch.setenv("MAGI_NAS_SSH_USER", "lumi")
    monkeypatch.setenv(
        "MAGI_NAS_SERVER_PATH_MAP_JSON",
        '[{"local_prefix": "/Volumes/homes", "remote_prefix": "/volume1/homes"},'
        '{"local_prefix": "/Volumes/lumi", "remote_prefix": "/volume2/lumi"}]',
    )

    cmd = mod._build_server_side_rsync_command(
        mod.Path("/Volumes/homes/lumi63181107/01_案件/case"),
        mod.Path("/Volumes/lumi/lumi/03_工作資料/10_結案/case"),
        dry_run=False,
        bwlimit_mbps=40,
        rsync_timeout_sec=600,
    )

    assert cmd[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    assert cmd[5] == "lumi@192.168.1.3"
    assert "/volume1/homes/lumi63181107/01_案件/case/" in cmd[-1]
    assert "/volume2/lumi/lumi/03_工作資料/10_結案/case/" in cmd[-1]
    assert "--bwlimit=40960" in cmd[-1]
    assert "Crimecall" not in " ".join(cmd)
