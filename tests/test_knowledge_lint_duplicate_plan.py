# -*- coding: utf-8 -*-
"""Tests for duplicate vector cleanup planning/apply flow in knowledge_lint."""

import copy
import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "knowledge_lint.py"


def _load_module():
    name = "knowledge_lint_for_test"
    spec = importlib.util.spec_from_file_location(name, str(SCRIPT_PATH))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class _FixtureConn:
    def __init__(self):
        self.documents = {
            1: {
                "id": 1,
                "content": "same content A",
                "source": "src-1",
                "created_at": "2026-04-26 10:00:00",
                "synced": 0,
            },
            2: {
                "id": 2,
                "content": "same content A",
                "source": "src-2",
                "created_at": "2026-04-26 10:00:01",
                "synced": 0,
            },
            3: {
                "id": 3,
                "content": "same content A",
                "source": "src-3",
                "created_at": "2026-04-26 10:00:02",
                "synced": 0,
            },
            4: {
                "id": 4,
                "content": "unique content B",
                "source": "src-4",
                "created_at": "2026-04-26 10:00:03",
                "synced": 0,
            },
        }
        self.vectors = {
            1: {"doc_id": 1, "embedding": "[0.1]"},
            2: {"doc_id": 2, "embedding": "[0.2]"},
            3: {"doc_id": 3, "embedding": "[0.3]"},
            4: {"doc_id": 4, "embedding": "[0.4]"},
        }

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _fixture_duplicate_rows(conn, max_groups):
    grouped = {}
    for row in conn.documents.values():
        content = row["content"]
        h = hashlib.md5(content.encode("utf-8")).hexdigest()
        grouped.setdefault(h, []).append(row["id"])

    rows = []
    for h, ids in grouped.items():
        if len(ids) < 2:
            continue
        ids = sorted(ids)
        rows.append(
            {
                "h": h,
                "cnt": len(ids),
                "ids": ",".join(str(i) for i in ids),
                "min_len": min(len(conn.documents[i]["content"]) for i in ids),
            }
        )
    rows.sort(key=lambda r: int(r["cnt"]), reverse=True)
    return rows[: max(1, int(max_groups))]


def _patch_fixture_db(monkeypatch, mod, conn, tmp_path):
    monkeypatch.setattr(mod, "_db_connect", lambda _name: conn)
    monkeypatch.setattr(mod, "_fetch_duplicate_rows", lambda c, max_groups=50: _fixture_duplicate_rows(c, max_groups))
    monkeypatch.setattr(
        mod,
        "_fetch_documents_for_ids",
        lambda c, ids: [copy.deepcopy(c.documents[i]) for i in sorted(ids) if i in c.documents],
    )
    monkeypatch.setattr(
        mod,
        "_fetch_vectors_for_ids",
        lambda c, ids: [copy.deepcopy(c.vectors[i]) for i in sorted(ids) if i in c.vectors],
    )
    monkeypatch.setattr(
        mod,
        "_delete_vectors_for_ids",
        lambda c, ids: sum(1 for i in ids if c.vectors.pop(int(i), None) is not None),
    )
    monkeypatch.setattr(
        mod,
        "_delete_documents_for_ids",
        lambda c, ids: sum(1 for i in ids if c.documents.pop(int(i), None) is not None),
    )
    monkeypatch.setattr(
        mod,
        "_count_documents_for_ids",
        lambda c, ids: sum(1 for i in ids if int(i) in c.documents),
    )
    monkeypatch.setattr(
        mod,
        "_count_vectors_for_ids",
        lambda c, ids: sum(1 for i in ids if int(i) in c.vectors),
    )
    monkeypatch.setattr(
        mod,
        "_fetch_existing_document_ids",
        lambda c, ids: {int(i) for i in ids if int(i) in c.documents},
    )
    monkeypatch.setattr(
        mod,
        "_fetch_duplicate_counts_for_hashes",
        lambda c, hashes: {
            h: sum(
                1
                for row in c.documents.values()
                if hashlib.md5(row["content"].encode("utf-8")).hexdigest() == h
            )
            for h in hashes
        },
    )
    monkeypatch.setattr(
        mod,
        "_fetch_ids_for_hash",
        lambda c, h: sorted(
            r["id"]
            for r in c.documents.values()
            if hashlib.md5(r["content"].encode("utf-8")).hexdigest() == h
        ),
    )
    monkeypatch.setattr(
        mod,
        "_insert_document_rows",
        lambda c, rows: _fixture_insert_documents(c, rows),
    )
    monkeypatch.setattr(
        mod,
        "_insert_vector_rows",
        lambda c, rows: _fixture_insert_vectors(c, rows),
    )

    fake_faiss_backup = tmp_path / "faiss-backup"
    fake_faiss_backup.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        mod,
        "_backup_faiss_files",
        lambda _backup_dir: {
            "ok": True,
            "status": "ok",
            "path": str(fake_faiss_backup),
            "files": [],
            "source_dir": str(tmp_path / "faiss-live"),
        },
    )
    monkeypatch.setattr(mod, "_restore_faiss_files", lambda _payload: {"restored": True, "files_restored": 0})
    monkeypatch.setattr(
        mod,
        "_rebuild_faiss_index",
        lambda: {"ok": True, "vectors_indexed": len(conn.vectors), "index_type": "flat"},
    )

    monkeypatch.setattr(mod, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(mod, "CLEANUP_REPORT_PATH", tmp_path / "knowledge_duplicate_cleanup_latest.json")


def _fixture_insert_documents(conn, rows):
    inserted = 0
    for row in rows or []:
        row_id = int(row.get("id"))
        if row_id in conn.documents:
            continue
        conn.documents[row_id] = copy.deepcopy(row)
        inserted += 1
    return inserted


def _fixture_insert_vectors(conn, rows):
    inserted = 0
    for row in rows or []:
        doc_id = int(row.get("doc_id"))
        if doc_id in conn.vectors:
            continue
        conn.vectors[doc_id] = copy.deepcopy(row)
        inserted += 1
    return inserted


def test_build_duplicate_cleanup_plan_keeps_lowest_id():
    mod = _load_module()
    plan = mod.build_duplicate_cleanup_plan(
        [
            {"h": "abc", "cnt": 3, "ids": "9,3,6"},
        ],
        max_groups=10,
    )

    assert plan["groups_planned"] == 1
    group = plan["groups"][0]
    assert group["keep_id"] == 3
    assert group["remove_ids"] == [6, 9]
    assert plan["estimated_delete_documents"] == 2


def test_check_duplicate_vectors_includes_cleanup_plan(monkeypatch):
    mod = _load_module()
    class _Conn:
        def close(self):
            return None

    monkeypatch.setattr(mod, "_db_connect", lambda _name: _Conn())
    monkeypatch.setattr(
        mod,
        "_fetch_duplicate_rows",
        lambda _conn, max_groups=50: [{"h": "h1", "cnt": 2, "ids": "11,12", "min_len": 1200}],
    )
    monkeypatch.setattr(mod, "_duplicate_cleanup_gate", lambda _rows: {"status": "pending_apply"})

    result = mod.check_duplicate_vectors()
    assert result["status"] == "warn"
    assert result["duplicate_groups"] == 1
    assert "cleanup_plan" in result
    assert result["cleanup_plan"]["groups_planned"] == 1
    assert result["cleanup_gate"]["status"] == "pending_apply"


def test_cleanup_duplicate_vectors_dry_run_does_not_mutate_fixture(monkeypatch, tmp_path):
    mod = _load_module()
    conn = _FixtureConn()
    before_docs = sorted(conn.documents.keys())
    before_vectors = sorted(conn.vectors.keys())
    _patch_fixture_db(monkeypatch, mod, conn, tmp_path)

    summary = mod.cleanup_duplicate_vectors(
        apply=False,
        max_groups=10,
        backup_dir=tmp_path / "backup",
        quiet=True,
    )

    assert summary["mode"] == "dry_run"
    assert summary["status"] == "dry_run"
    assert summary["removed_count"] == 0
    assert summary["verified"] is False
    assert summary["before"]["duplicate_groups"] == 1
    assert summary["after"]["duplicate_groups"] == 1
    assert sorted(conn.documents.keys()) == before_docs
    assert sorted(conn.vectors.keys()) == before_vectors


def test_cleanup_duplicate_vectors_apply_removes_duplicates_and_verifies(monkeypatch, tmp_path):
    mod = _load_module()
    conn = _FixtureConn()
    _patch_fixture_db(monkeypatch, mod, conn, tmp_path)

    summary = mod.cleanup_duplicate_vectors(
        apply=True,
        max_groups=10,
        backup_dir=tmp_path / "backup",
        quiet=True,
    )

    assert summary["mode"] == "apply"
    assert summary["status"] == "ok"
    assert summary["verified"] is True
    assert summary["removed_count"] == 2
    assert summary["before"]["duplicate_groups"] == 1
    assert summary["after"]["duplicate_groups"] == 0
    assert sorted(conn.documents.keys()) == [1, 4]
    assert sorted(conn.vectors.keys()) == [1, 4]
    assert Path(summary["backup_path"]).exists()


def test_cleanup_duplicate_vectors_verify_fail_does_not_silent_success(monkeypatch, tmp_path):
    mod = _load_module()
    conn = _FixtureConn()
    _patch_fixture_db(monkeypatch, mod, conn, tmp_path)
    monkeypatch.setattr(
        mod,
        "_verify_duplicate_cleanup",
        lambda _conn, _groups, _removed_ids, faiss_ok=True: {
            "ok": False,
            "keep_ok": False,
            "remove_ok": False,
            "hash_ok": False,
            "faiss_ok": faiss_ok,
        },
    )

    summary = mod.cleanup_duplicate_vectors(
        apply=True,
        max_groups=10,
        backup_dir=tmp_path / "backup",
        quiet=True,
    )

    assert summary["mode"] == "apply"
    assert summary["status"] == "error"
    assert summary["verified"] is False
    assert summary["rollback"]["attempted"] is True
    assert summary["rollback"]["restored"] is True
    assert sorted(conn.documents.keys()) == [1, 2, 3, 4]
    assert sorted(conn.vectors.keys()) == [1, 2, 3, 4]


def test_contradiction_scan_ignores_info_gap_only_sections(monkeypatch, tmp_path):
    mod = _load_module()
    vault = tmp_path / "vault"
    case_dir = vault / "30_Wiki" / "2026-0001-測試"
    case_dir.mkdir(parents=True)
    (case_dir / "overview.md").write_text(
        "## ⚠️ 矛盾與待確認\n"
        "- **資訊缺口**：目前只有開庭通知，缺少起訴書。\n"
        "- **待確認事項**：需確認法院函文內容。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_get_vault_path", lambda: vault)

    result = mod.check_contradiction_scan(use_llm=True)

    assert result["status"] == "ok"
    assert result["cases_with_contradictions"] == 0
    assert result["info_gap_only_cases"] == 1


def test_contradiction_scan_keeps_actual_conflict(monkeypatch, tmp_path):
    mod = _load_module()
    vault = tmp_path / "vault"
    case_dir = vault / "30_Wiki" / "2026-0002-測試"
    case_dir.mkdir(parents=True)
    (case_dir / "overview.md").write_text(
        "## ⚠️ 矛盾與待確認\n"
        "- ⚠️ 矛盾：同一筆付款日期，版本一記載 2026-01-01，版本二記載 2026-02-01。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_get_vault_path", lambda: vault)

    result = mod.check_contradiction_scan(use_llm=True)

    assert result["status"] == "warn"
    assert result["cases_with_contradictions"] == 1
