from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from magi_v3.case_filesystem import NativeCaseFilesystemEffects
from magi_v3.osc_cases import OscCasesError, OscCasesService, SQLiteCaseStore, initialize_sqlite_cases_schema


@pytest.fixture
def connection() -> sqlite3.Connection:
    value = sqlite3.connect(":memory:")
    initialize_sqlite_cases_schema(value)
    yield value
    value.close()


def _effects(tmp_path: Path) -> NativeCaseFilesystemEffects:
    case_root = tmp_path / "01_案件"
    archive_root = tmp_path / "10_結案"
    case_root.mkdir()
    archive_root.mkdir()
    return NativeCaseFilesystemEffects(
        case_root=case_root,
        archive_root=archive_root,
        canonicalize=lambda value: value,
        localize=lambda value: value,
    )


def _service(
    connection: sqlite3.Connection,
    effects: NativeCaseFilesystemEffects,
    *,
    post_persist: Any | None = None,
) -> OscCasesService:
    return OscCasesService(
        SQLiteCaseStore(connection),
        id_factory=lambda: "native-filesystem-case",
        year_provider=lambda: 2026,
        post_persist=post_persist or effects,
        side_effects_enabled=True,
    )


def test_folder_creation_and_archive_are_transactionally_reflected(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    effects = _effects(tmp_path)
    service = _service(connection, effects)
    created = service.create_case(
        {
            "id": "folder-case",
            "case_number": "2026-0001",
            "client_name": "當事人",
            "case_category": "一般案件",
            "case_type": "民事",
            "case_stage": "一審",
            "case_reason": "損害賠償",
            "status": "進行中",
            "auto_create_folder": True,
        }
    )

    folder = Path(created.effects["folder"]["path"])
    assert folder.is_dir()
    assert len(list(folder.glob("*/.gitkeep"))) == 10
    assert connection.execute(
        "SELECT folder_path FROM cases WHERE id='folder-case'"
    ).fetchone()[0] == str(folder)

    (folder / "payload.txt").write_text("evidence\n", encoding="utf-8")
    archived = service.create_case(
        {
            "id": "folder-case",
            "case_number": "2026-0001",
            "client_name": "當事人",
            "case_category": "一般案件",
            "case_type": "民事",
            "status": "已結案",
        }
    )

    target = Path(archived.effects["archive"]["to"])
    assert archived.effects["archive"]["ok"] is True
    assert not folder.exists()
    assert (target / "payload.txt").read_text(encoding="utf-8") == "evidence\n"
    assert (target.parent / ".archive_incoming").is_dir()
    status, folder_path = connection.execute(
        "SELECT status,folder_path FROM cases WHERE id='folder-case'"
    ).fetchone()
    assert status == "已結案" and folder_path == str(target)


def test_pending_closure_archives_every_case_category_without_marking_final(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    effects = _effects(tmp_path)
    service = _service(connection, effects)
    created = service.create_case(
        {
            "id": "general-closing",
            "case_number": "2026-0091",
            "client_name": "待結案案件",
            "case_category": "一般案件",
            "case_type": "民事",
            "case_stage": "一審",
            "case_reason": "損害賠償",
            "status": "進行中",
            "auto_create_folder": True,
        }
    )
    source = Path(created.effects["folder"]["path"])
    (source / "proof.txt").write_text("keep\n", encoding="utf-8")

    archived = service.create_case(
        {
            "id": "general-closing",
            "case_number": "2026-0091",
            "client_name": "待結案案件",
            "case_category": "一般案件",
            "case_type": "民事",
            "status": "待送出",
        }
    )

    target = Path(archived.effects["archive"]["to"])
    assert target.is_dir()
    assert not source.exists()
    status, folder_path = connection.execute(
        "SELECT status,folder_path FROM cases WHERE id='general-closing'"
    ).fetchone()
    assert status == "結案中"
    assert folder_path == str(target)


def test_folder_is_removed_and_database_rolled_back_if_later_hook_fails(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    effects = _effects(tmp_path)

    def fail_after_filesystem(transaction: Any, result: Any, payload: Any) -> None:
        effects(transaction, result, payload)
        raise RuntimeError("injected after filesystem mutation")

    service = _service(connection, effects, post_persist=fail_after_filesystem)
    with pytest.raises(RuntimeError, match="injected"):
        service.create_case(
            {
                "id": "rollback-folder",
                "case_number": "2026-0002",
                "client_name": "回滾",
                "case_category": "一般案件",
                "case_type": "民事",
                "status": "進行中",
                "auto_create_folder": True,
            }
        )

    assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    assert list((tmp_path / "01_案件").rglob("2026-0002*")) == []


def test_archive_move_is_reversed_if_database_transaction_fails(
    tmp_path: Path,
    connection: sqlite3.Connection,
) -> None:
    effects = _effects(tmp_path)
    source = tmp_path / "01_案件" / "一般案件" / "民事" / "2026-0003-回滾"
    source.mkdir(parents=True)
    (source / "proof.txt").write_text("keep\n", encoding="utf-8")
    base = OscCasesService(
        SQLiteCaseStore(connection),
        id_factory=lambda: "archive-rollback",
        year_provider=lambda: 2026,
    )
    base.create_case(
        {
            "id": "archive-rollback",
            "case_number": "2026-0003",
            "client_name": "回滾",
            "case_type": "民事",
            "status": "進行中",
            "folder_path": str(source),
        }
    )

    def fail_after_archive(transaction: Any, result: Any, payload: Any) -> None:
        effects(transaction, result, payload)
        raise RuntimeError("injected archive rollback")

    service = _service(connection, effects, post_persist=fail_after_archive)
    with pytest.raises(RuntimeError, match="archive rollback"):
        service.create_case(
            {
                "id": "archive-rollback",
                "case_number": "2026-0003",
                "client_name": "回滾",
                "case_type": "民事",
                "status": "已結案",
            }
        )

    assert (source / "proof.txt").read_text(encoding="utf-8") == "keep\n"
    assert not list((tmp_path / "10_結案").rglob("2026-0003-回滾"))
    status, folder_path = connection.execute(
        "SELECT status,folder_path FROM cases WHERE id='archive-rollback'"
    ).fetchone()
    assert status == "進行中" and folder_path == str(source)


def test_environment_composition_requires_write_flag_or_contained_disposable_root(
    tmp_path: Path,
) -> None:
    case_root = tmp_path / "01_案件"
    archive_root = tmp_path / "10_結案"
    case_root.mkdir()
    archive_root.mkdir()
    base = {
        "MAGI_V3_CASE_ROOT": str(case_root),
        "MAGI_V3_ARCHIVE_ROOT": str(archive_root),
    }

    with pytest.raises(OscCasesError, match="writes are disabled"):
        NativeCaseFilesystemEffects.from_environment(
            base,
            canonicalize=lambda value: value,
            localize=lambda value: value,
        )

    effects = NativeCaseFilesystemEffects.from_environment(
        {**base, "MAGI_V3_DISPOSABLE_NAS_ROOT": str(tmp_path)},
        canonicalize=lambda value: value,
        localize=lambda value: value,
    )
    assert effects.case_root == case_root.resolve()
