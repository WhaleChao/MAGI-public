from __future__ import annotations

from pathlib import Path

import pytest

from magi_v3.memory_lifecycle import (
    BACKENDS,
    MemoryLifecycleError,
    MemoryLifecycleStore,
    canonical_memory_id,
    filter_tombstoned_results,
    propagate_tombstone,
)
from magi_v3.memory_backends import KnowledgeGraphBackend, ObsidianIndexBackend


def test_record_v2_stores_hashes_not_memory_content(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryLifecycleStore(path)
    secret_content = "本件私密案件內容不可出現在 metadata sidecar"
    record = store.register(
        secret_content,
        "user_profile|source_id=profile-1",
        metadata={"case_scope": "case-opaque", "confidence": 0.91},
    )
    assert record.memory_id == canonical_memory_id("user_profile|source_id=profile-1", secret_content)
    assert record.case_scope == "case-opaque"
    assert record.status == "active"
    assert secret_content.encode("utf-8") not in path.read_bytes()
    assert path.stat().st_mode & 0o077 == 0


def test_exact_tombstone_stops_recall_and_never_uses_fuzzy_delete(tmp_path: Path):
    store = MemoryLifecycleStore(tmp_path / "memory.sqlite3")
    content = "使用者可依法刪除的偏好"
    source = "user_profile|source_id=preference-1"
    record = store.register(content, source)
    store.tombstone(record.memory_id, reason="user correction", actor="test-user")
    rows = [
        {"id": 1, "content": content, "source": source, "score": 1.0},
        {"id": 2, "content": "保留", "source": "manual", "score": 0.8},
    ]
    assert [item["id"] for item in filter_tombstoned_results(rows, store=store)] == [2]
    with pytest.raises(MemoryLifecycleError, match="exact memory_id"):
        store.get("使用者偏好")


def test_formal_legal_records_and_legal_hold_cannot_be_deleted(tmp_path: Path):
    store = MemoryLifecycleStore(tmp_path / "memory.sqlite3")
    record = store.register(
        "正式卷證的索引內容",
        "case_file|source_id=evidence-1",
        metadata={"retention_policy": "formal_legal_record", "legal_hold": True},
    )
    assert record.protected_legal_record is True
    with pytest.raises(MemoryLifecycleError, match="formal legal records"):
        store.tombstone(record.memory_id, reason="must not delete", actor="test-user")
    assert store.get(record.memory_id).status == "active"


def test_correction_archives_old_record_and_links_replacement(tmp_path: Path):
    store = MemoryLifecycleStore(tmp_path / "memory.sqlite3")
    original = store.register("錯誤偏好", "user_profile|source_id=pref")
    archived, replacement = store.correct(
        original.memory_id,
        "正確偏好",
        reason="user corrected fact",
        actor="test-user",
    )
    assert archived.status == "archived"
    assert replacement.status == "active"
    assert replacement.supersedes == original.memory_id
    assert replacement.memory_id != original.memory_id


def test_correction_rejects_identical_content_without_archiving_original(tmp_path: Path):
    store = MemoryLifecycleStore(tmp_path / "memory.sqlite3")
    original = store.register("原內容", "user_profile|source_id=pref")
    with pytest.raises(MemoryLifecycleError, match="must change"):
        store.correct(original.memory_id, "原內容", reason="no-op", actor="test-user")
    assert store.get(original.memory_id).status == "active"


def test_deletion_requires_all_backends_and_receipts(tmp_path: Path):
    store = MemoryLifecycleStore(tmp_path / "memory.sqlite3")
    record = store.register("可刪除內容", "user_profile|source_id=x")
    store.tombstone(record.memory_id, reason="user requested", actor="test-user")
    assert store.deletion_consistency(record.memory_id)["complete"] is False

    def adapter(memory):
        return {"status": "not_present", "receipt_sha256": "a" * 64, "detail": memory.memory_id}

    result = propagate_tombstone(store, record.memory_id, {backend: adapter for backend in BACKENDS})
    assert result["complete"] is True
    assert result["formal_legal_files_affected"] is False


def test_derived_graph_and_obsidian_indexes_tombstone_without_deleting_files(tmp_path: Path):
    import json

    from skills.engine.knowledge_graph.graph_store import GraphStore

    store = MemoryLifecycleStore(tmp_path / "memory.sqlite3")
    record = store.register("可刪除的衍生記憶", "user_profile|source_id=derived")
    record = store.tombstone(record.memory_id, reason="user requested", actor="test-user")

    graph_path = tmp_path / "graph.json"
    graph = GraphStore()
    graph.upsert_node("derived-node", memory_id=record.memory_id, label="derived")
    graph.upsert_node("formal-node", label="formal source remains")
    graph.save(str(graph_path))
    result = KnowledgeGraphBackend(graph_path)(record)
    assert result["status"] == "tombstoned"
    reloaded = GraphStore.load(str(graph_path))
    assert "derived-node" not in reloaded.graph
    assert "formal-node" in reloaded.graph

    vault_note = tmp_path / "formal-note.md"
    vault_note.write_text("formal legal note", encoding="utf-8")
    index_path = tmp_path / "obsidian_index.json"
    index_path.write_text(json.dumps({"notes": {str(vault_note): {"hash": "x"}}}), encoding="utf-8")
    result = ObsidianIndexBackend(index_path)(record)
    assert result["status"] == "tombstoned"
    assert vault_note.read_text(encoding="utf-8") == "formal legal note"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert record.memory_id in payload["memory_tombstones"]
