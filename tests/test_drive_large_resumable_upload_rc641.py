from __future__ import annotations

from contextlib import contextmanager

from api.osc import drive_case_sync


def _plan(path: str, *, size: int) -> dict:
    return {
        "cases": [
            {
                "case_number": "synthetic-case",
                "drive_id": "synthetic-drive-folder",
                "drive_path": "synthetic",
                "nas_only": [
                    {
                        "path": path,
                        "relative_path": "large.pdf",
                        "target_relative_path": "large.pdf",
                        "size": size,
                    }
                ],
            }
        ]
    }


def test_oversized_first_item_is_classified_instead_of_zero_attempt_retry(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "large.pdf"
    source.write_bytes(b"fixture")
    monkeypatch.setenv("MAGI_DRIVE_SYNC_MAX_SINGLE_UPLOAD_BYTES", "1500000000")

    result = drive_case_sync.execute_nas_to_drive_uploads(
        object(),
        _plan(str(source), size=2_370_435_052),
        max_upload_bytes=1_500_000_000,
    )

    assert result["summary"]["attempted"] == 0
    assert result["summary"]["large_upload_deferred"] == 1
    assert result["summary"]["stopped_by_bytes"] is False
    assert result["manifest"][0]["status"] == "deferred_large_file"


def test_resumable_large_file_within_rc641_envelope_is_attempted(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "large.pdf"
    source.write_bytes(b"fixture")
    declared_size = 2_370_435_052
    monkeypatch.setenv("MAGI_DRIVE_SYNC_MAX_SINGLE_UPLOAD_BYTES", "3000000000")

    @contextmanager
    def staged(path):
        assert path == source
        yield path

    monkeypatch.setattr(drive_case_sync, "staged_upload_source", staged)
    monkeypatch.setattr(
        drive_case_sync,
        "upload_local_file_to_drive",
        lambda *_args, **_kwargs: {
            "status": "uploaded",
            "bytes": declared_size,
            "created_folders": [],
            "hash_verification": "verified_checksum",
            "destination_proof": "a" * 64,
        },
    )

    result = drive_case_sync.execute_nas_to_drive_uploads(
        object(),
        _plan(str(source), size=declared_size),
        max_upload_bytes=3_000_000_000,
    )

    assert result["ok"] is True
    assert result["summary"]["attempted"] == 1
    assert result["summary"]["uploaded"] == 1
    assert result["summary"]["bytes"] == declared_size
    assert result["summary"]["stopped_by_bytes"] is False
