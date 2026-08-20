"""Global side-effect fuses for ordinary pytest runs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


FORBIDDEN_CASE_TOKENS = ("2026-9998", "測試消債")


class SideEffectBlocked(RuntimeError):
    """Raised when a normal unit test attempts a live write."""


def _truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def live_enabled() -> bool:
    return _truthy("MAGI_ENABLE_LIVE_TESTS") or not _truthy("MAGI_TEST_MODE")


def _looks_like_real_nas_root(text: str) -> bool:
    norm = str(text or "").replace("\\", "/")
    if _is_pytest_temp_path(norm):
        return False
    return (
        norm.startswith("/Volumes/")
        or norm.startswith("/Network/")
        or norm.startswith("Z:/")
        or norm.startswith("Y:/")
        or norm.startswith("//")
        or "/Library/CloudStorage/SynologyDrive" in norm
    )


def _is_pytest_temp_path(text: str) -> bool:
    temp_root = str(Path(tempfile.gettempdir()).resolve()).replace("\\", "/").rstrip("/")
    candidates = {temp_root}
    if temp_root.startswith("/var/"):
        candidates.add("/private" + temp_root)
    return any(text == root or text.startswith(root + "/") for root in candidates)


def assert_safe_path(path: Any, *, operation: str = "filesystem write") -> None:
    if live_enabled():
        return
    text = str(path or "").replace("\\", "/")
    for token in FORBIDDEN_CASE_TOKENS:
        if token in text:
            raise SideEffectBlocked(f"Blocked {operation} touching sentinel case token: {token}")
    if _looks_like_real_nas_root(text):
        raise SideEffectBlocked(f"Blocked {operation} touching real NAS/Drive root: {path}")


def block_live_writer(name: str):
    def _blocked(*args, **kwargs):
        if live_enabled():
            return None
        raise SideEffectBlocked(f"Blocked live writer in ordinary pytest: {name}")

    return _blocked


def install(monkeypatch) -> None:
    if live_enabled():
        return

    _original_makedirs = os.makedirs
    _original_path_mkdir = Path.mkdir

    def _blocked_build_drive_service(*args, **kwargs):
        if kwargs.get("write"):
            raise SideEffectBlocked("Blocked Google Drive write service in ordinary pytest")
        raise SideEffectBlocked("Blocked Google Drive service construction in ordinary pytest")

    try:
        monkeypatch.setattr("api.osc.drive_case_sync.build_drive_service", _blocked_build_drive_service, raising=False)
    except Exception:
        pass

    def _guarded_create_folder_structure(base_path: str, case_category: str = "一般案件") -> dict:
        assert_safe_path(base_path, operation="OSC case folder creation")
        from api.osc.folder_utils import _create_folder_structure_unchecked

        return _create_folder_structure_unchecked(base_path, case_category)

    try:
        monkeypatch.setattr("api.osc.folder_utils.create_folder_structure", _guarded_create_folder_structure, raising=False)
    except Exception:
        pass
    try:
        monkeypatch.setattr(
            "casper_ecosystem.law_firm_orchestrators.osc.folder_utils.create_folder_structure",
            _guarded_create_folder_structure,
            raising=False,
        )
    except Exception:
        pass

    def _guarded_makedirs(path, *args, **kwargs):
        assert_safe_path(path, operation="os.makedirs")
        return _original_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", _guarded_makedirs)

    def _guarded_path_mkdir(self, *args, **kwargs):
        assert_safe_path(self, operation="Path.mkdir")
        return _original_path_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _guarded_path_mkdir)
