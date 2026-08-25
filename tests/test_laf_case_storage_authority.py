from __future__ import annotations

from pathlib import Path

from api import case_path_mapper as mapper
from casper_ecosystem.law_firm_orchestrators import laf_folder_builder as folder_builder


def _builder(*, root: str | None, windows_base: str = "Z:/office/01_案件"):
    builder = folder_builder.LAFFolderBuilder.__new__(folder_builder.LAFFolderBuilder)
    builder.config = {}
    builder.experiment_base_dir = None
    builder.windows_base = windows_base
    builder.mac_smb_base = "smb://nas/homes/office/01_案件"
    builder.mac_local_base = root
    builder.laf_target = "法扶案件"
    return builder


def test_authoritative_storage_requires_real_mount_record(monkeypatch):
    path = "/Volumes/homes/office/01_案件"
    monkeypatch.setattr(mapper, "_is_dir_accessible", lambda value: value == path)

    monkeypatch.setattr(mapper, "_mount_output_lines", lambda: [])
    assert mapper.is_authoritative_case_storage_path(path) is False
    assert mapper.is_authoritative_nas_write_path(path) is False

    monkeypatch.setattr(
        mapper,
        "_mount_output_lines",
        lambda: ["//office@nas/homes on /Volumes/homes (smbfs, nodev, nosuid)"],
    )
    assert mapper.is_authoritative_case_storage_path(path) is True
    assert mapper.is_authoritative_nas_write_path(path) is True


def test_external_volume_is_storage_evidence_but_not_active_nas_write(monkeypatch):
    path = "/Volumes/Archive/03_工作資料/10_結案"
    monkeypatch.setattr(mapper, "_is_dir_accessible", lambda value: value == path)
    monkeypatch.setattr(
        mapper,
        "_mount_output_lines",
        lambda: ["/dev/disk9s1 on /Volumes/Archive (apfs, local, nodev)"],
    )

    assert mapper.is_authoritative_case_storage_path(path) is True
    assert mapper.is_authoritative_nas_write_path(path) is False


def test_file_provider_and_user_mount_are_never_authoritative(monkeypatch):
    monkeypatch.setattr(
        mapper,
        "_mount_output_lines",
        lambda: ["//office@nas/homes on /Volumes/homes (smbfs, nodev)"],
    )
    monkeypatch.setattr(mapper, "_is_dir_accessible", lambda _value: True)

    assert mapper.is_authoritative_case_storage_path(
        "/Users/test/Library/CloudStorage/SynologyDrive-homes/01_案件"
    ) is False
    assert mapper.is_authoritative_nas_write_path(
        "/Users/test/.magi_mounts/homes/office/01_案件"
    ) is False


def test_folder_builder_selects_only_authoritative_smb(monkeypatch):
    cloud = "/Users/test/Library/CloudStorage/SynologyDrive-homes/01_案件"
    smb = "/Volumes/homes/office/01_案件"
    builder = _builder(root=None)
    monkeypatch.setattr(
        folder_builder,
        "local_case_path_candidates",
        lambda *_args, **_kwargs: [cloud, smb],
    )
    monkeypatch.setattr(folder_builder, "authoritative_active_case_roots", lambda: [])
    monkeypatch.setattr(
        folder_builder,
        "is_authoritative_nas_write_path",
        lambda value: value == smb,
    )

    builder._detect_local_mount()

    assert builder.mac_local_base == smb


def test_folder_builder_fails_closed_when_only_file_provider_exists(monkeypatch):
    cloud = "/Users/test/Library/CloudStorage/SynologyDrive-homes/01_案件"
    builder = _builder(root=None)
    monkeypatch.setattr(
        folder_builder,
        "local_case_path_candidates",
        lambda *_args, **_kwargs: [cloud],
    )
    monkeypatch.setattr(folder_builder, "authoritative_active_case_roots", lambda: [])
    monkeypatch.setattr(folder_builder, "is_authoritative_nas_write_path", lambda _value: False)
    monkeypatch.setattr(
        "api.nas_mount_guard.ensure_nas_mounts",
        lambda: {"ok": False},
    )
    touched: list[str] = []
    monkeypatch.setattr(builder, "_safe_makedirs", lambda path: touched.append(path))

    result = builder.create_case_folder(
        {"case_number": "2099-0001", "client_name": "隔離測試", "case_type": "民事"}
    )

    assert result is None
    assert builder.mac_local_base is None
    assert touched == []


def test_folder_builder_returns_canonical_only_after_authoritative_creation(
    monkeypatch, tmp_path: Path
):
    root = tmp_path / "mounted-smb" / "01_案件"
    root.mkdir(parents=True)
    builder = _builder(root=str(root))
    monkeypatch.setattr(folder_builder, "is_authoritative_nas_write_path", lambda value: value == str(root))
    monkeypatch.setattr(
        builder,
        "_local_to_canonical",
        lambda _path: r"Z:\office\01_案件\法扶案件\民事\2099-0002-隔離測試-民事",
    )

    result = builder.create_case_folder(
        {"case_number": "2099-0002", "client_name": "隔離測試", "case_type": "民事"}
    )

    assert result and result.startswith("Z:\\office\\01_案件\\")
    created = root / "法扶案件" / "民事" / "2099-0002-隔離測試-民事"
    assert created.is_dir()
    assert all((created / name).is_dir() for name in folder_builder.STANDARD_SUBFOLDERS)


def test_folder_builder_rejects_canonical_mapping_drift(monkeypatch, tmp_path: Path):
    root = tmp_path / "mounted-smb" / "01_案件"
    root.mkdir(parents=True)
    builder = _builder(root=str(root))
    monkeypatch.setattr(folder_builder, "is_authoritative_nas_write_path", lambda value: value == str(root))
    monkeypatch.setattr(builder, "_local_to_canonical", lambda _path: r"Z:\wrong\01_案件\case")

    assert builder.create_case_folder(
        {"case_number": "2099-0003", "client_name": "隔離測試", "case_type": "民事"}
    ) is None
