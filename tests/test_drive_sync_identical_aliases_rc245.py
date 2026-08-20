from __future__ import annotations

from api.osc import drive_case_sync


def _drive_entry(path: str, *, md5: str, size: int = 120_025) -> drive_case_sync.FileEntry:
    return drive_case_sync.FileEntry(
        source="drive",
        path=path,
        relative_path=path,
        name=path.rsplit("/", 1)[-1],
        is_folder=False,
        size=size,
        md5=md5,
        drive_id=path,
        mime_type="application/pdf",
    )


def test_identical_drive_aliases_do_not_block_sync() -> None:
    digest = "856ead3ec15f9bd2b8f06c91400c3fa9"
    alias = _drive_entry("調解通知/同一份調解通知.pdf", md5=digest)
    canonical = _drive_entry("法院通知/同一份調解通知.pdf", md5=digest)

    unique, collisions, duplicates = drive_case_sync._semantic_file_index(
        [alias, canonical],
        drive_side=True,
    )

    key = drive_case_sync.normalized_relative_file_key(
        drive_case_sync.semantic_relative_path(canonical.relative_path)
    )
    assert collisions == {}
    assert unique[key] is canonical
    assert duplicates[key] == [alias, canonical]


def test_different_content_in_alias_folders_remains_fail_closed() -> None:
    first = _drive_entry("調解通知/同名通知.pdf", md5="1" * 32)
    second = _drive_entry("法院通知/同名通知.pdf", md5="2" * 32)

    unique, collisions, duplicates = drive_case_sync._semantic_file_index(
        [first, second],
        drive_side=True,
    )

    assert unique == {}
    assert len(collisions) == 1
    assert duplicates == {}


def test_unverifiable_aliases_remain_fail_closed() -> None:
    first = _drive_entry("調解通知/同名通知.pdf", md5="")
    second = _drive_entry("法院通知/同名通知.pdf", md5="")

    unique, collisions, duplicates = drive_case_sync._semantic_file_index(
        [first, second],
        drive_side=True,
    )

    assert unique == {}
    assert len(collisions) == 1
    assert duplicates == {}
