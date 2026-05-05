from __future__ import annotations


def test_synology_drive_fallback_counts_as_homes_available(tmp_path, monkeypatch):
    from api import nas_mount_guard as mod

    fallback = tmp_path / "SynologyDrive-homes"
    fallback.mkdir()
    (fallback / "01_案件").mkdir()
    monkeypatch.setattr(mod, "_SYNOLOGY_DRIVE_CANDIDATES", (str(fallback),))
    monkeypatch.setattr(mod, "_is_mounted", lambda path: False)

    assert mod.get_synology_drive_fallback_path() == str(fallback)
    assert mod.get_share_available_path("homes", "/Volumes/homes") == str(fallback)
    assert mod.get_share_available_path("lumi", "/Volumes/lumi") == ""
