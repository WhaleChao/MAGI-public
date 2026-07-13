from __future__ import annotations

import pytest

from tests.support import side_effect_guard


def test_side_effect_guard_blocks_sentinel_case_tokens():
    with pytest.raises(side_effect_guard.SideEffectBlocked, match="2026-9998"):
        side_effect_guard.assert_safe_path("/tmp/2026-9998-測試消債當事人")


def test_side_effect_guard_blocks_real_nas_roots():
    with pytest.raises(side_effect_guard.SideEffectBlocked, match="NAS/Drive"):
        side_effect_guard.assert_safe_path("/Volumes/homes/01_案件/一般案件/2026-0001")


def test_osc_folder_creation_blocks_sentinel_in_ordinary_pytest():
    from api.osc.folder_utils import create_folder_structure

    with pytest.raises(Exception, match="2026-9998|sentinel"):
        create_folder_structure("/tmp/2026-9998-測試消債當事人", "一般案件")


def test_osc_folder_creation_allows_tmp_sandbox(tmp_path):
    from api.osc.folder_utils import create_folder_structure

    result = create_folder_structure(str(tmp_path / "case"), "一般案件")

    assert result["ok"] is True
    assert (tmp_path / "case").is_dir()


def test_drive_service_construction_blocked_in_ordinary_pytest():
    from api.osc.drive_case_sync import build_drive_service

    with pytest.raises(Exception, match="Drive|pytest"):
        build_drive_service(write=True)
