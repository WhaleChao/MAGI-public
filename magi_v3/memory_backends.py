"""Concrete MemoryRecord v2 tombstone propagation adapters.

Adapters operate only on an already tombstoned, non-legal-hold record.  They
return content-free receipts.  The formal case files behind an Obsidian note or
legal source are never unlinked by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from magi_v3.memory_lifecycle import MemoryLifecycleError, MemoryRecord


def _receipt(backend: str, memory_id: str, status: str, detail: Mapping[str, Any]) -> str:
    payload = {
        "schema": "magi.memory-backend-receipt/v1",
        "backend": backend,
        "memory_id": memory_id,
        "status": status,
        "detail": dict(detail),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _eligible(record: MemoryRecord) -> None:
    if record.status != "tombstoned":
        raise MemoryLifecycleError("backend propagation requires a tombstoned record")
    if record.protected_legal_record:
        raise MemoryLifecycleError("protected legal record cannot enter memory deletion adapters")


class SqlMemoryBackend:
    def __init__(self, backend: str, connection_config: Mapping[str, Any]) -> None:
        if backend not in {"primary", "keeper", "mariadb"}:
            raise MemoryLifecycleError("SQL memory backend name is invalid")
        self.backend = backend
        self.connection_config = dict(connection_config)

    def __call__(self, record: MemoryRecord) -> dict[str, Any]:
        _eligible(record)
        import mysql.connector

        connection = mysql.connector.connect(**self.connection_config)
        cursor = connection.cursor()
        matched_ids: list[int] = []
        try:
            cursor.execute("SELECT id,content FROM documents WHERE source=%s", (record.source,))
            for doc_id, content in cursor.fetchall() or []:
                digest = hashlib.sha256(str(content or "").encode("utf-8", errors="replace")).hexdigest()
                if digest == record.content_sha256:
                    matched_ids.append(int(doc_id))
            if matched_ids:
                placeholders = ",".join("%s" for _ in matched_ids)
                cursor.execute(f"DELETE FROM vectors WHERE doc_id IN ({placeholders})", tuple(matched_ids))
                cursor.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", tuple(matched_ids))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()
        detail = {"deleted_document_count": len(matched_ids), "matched_ids_sha256": hashlib.sha256(
            ",".join(str(item) for item in sorted(matched_ids)).encode("ascii")
        ).hexdigest()}
        status = "not_present"
        return {
            "status": status,
            "receipt_sha256": _receipt(self.backend, record.memory_id, status, detail),
            "detail": json.dumps(detail, sort_keys=True, separators=(",", ":")),
        }


class FaissRebuildBackend:
    def __init__(self, db_config: Mapping[str, Any]) -> None:
        self.db_config = dict(db_config)

    def __call__(self, record: MemoryRecord) -> dict[str, Any]:
        _eligible(record)
        from skills.memory.faiss_index import FAISSMemoryIndex

        index = FAISSMemoryIndex(load_existing=False)
        stats = index.build_from_db_streaming(self.db_config, publish=True)
        status = "tombstoned"
        detail = {
            "indexed_rows": int(stats.get("indexed_rows") or 0),
            "index_type": str(stats.get("index_type") or ""),
            "generation_publish": True,
        }
        return {
            "status": status,
            "receipt_sha256": _receipt("faiss", record.memory_id, status, detail),
            "detail": json.dumps(detail, sort_keys=True, separators=(",", ":")),
        }


class KnowledgeGraphBackend:
    def __init__(self, graph_path: Path | str) -> None:
        self.graph_path = Path(graph_path).expanduser()

    def __call__(self, record: MemoryRecord) -> dict[str, Any]:
        _eligible(record)
        if not self.graph_path.exists():
            status, removed = "not_present", 0
        else:
            from skills.engine.knowledge_graph.graph_store import GraphStore

            store = GraphStore.load(str(self.graph_path))
            matched = []
            for node_id, attrs in store.graph.nodes(data=True):
                if str(attrs.get("memory_id") or "") == record.memory_id or str(
                    attrs.get("content_sha256") or ""
                ) == record.content_sha256:
                    matched.append(node_id)
            store.graph.remove_nodes_from(matched)
            if matched:
                store.save(str(self.graph_path))
            removed = len(matched)
            status = "tombstoned" if removed else "not_present"
        detail = {"removed_nodes": removed, "formal_legal_files_affected": False}
        return {
            "status": status,
            "receipt_sha256": _receipt("knowledge_graph", record.memory_id, status, detail),
            "detail": json.dumps(detail, sort_keys=True, separators=(",", ":")),
        }


class ObsidianIndexBackend:
    """Mark the derived memory index only; never delete a vault note or source file."""

    def __init__(self, index_path: Path | str) -> None:
        self.index_path = Path(index_path).expanduser()

    def __call__(self, record: MemoryRecord) -> dict[str, Any]:
        _eligible(record)
        payload: dict[str, Any] = {}
        if self.index_path.exists():
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping):
                payload = dict(raw)
        tombstones = dict(payload.get("memory_tombstones") or {})
        tombstones[record.memory_id] = {
            "content_sha256": record.content_sha256,
            "tombstoned_at": record.tombstoned_at,
            "reason_sha256": hashlib.sha256(record.tombstone_reason.encode("utf-8")).hexdigest(),
        }
        payload["memory_tombstones"] = tombstones
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(self.index_path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.index_path)
        status = "tombstoned"
        detail = {"vault_notes_deleted": 0, "source_files_deleted": 0}
        return {
            "status": status,
            "receipt_sha256": _receipt("obsidian", record.memory_id, status, detail),
            "detail": json.dumps(detail, sort_keys=True, separators=(",", ":")),
        }


def logical_backend(backend: str, status: str = "tombstoned"):
    def apply(record: MemoryRecord) -> dict[str, Any]:
        _eligible(record)
        detail = {"logical_index_only": True, "formal_legal_files_affected": False}
        return {
            "status": status,
            "receipt_sha256": _receipt(backend, record.memory_id, status, detail),
            "detail": json.dumps(detail, sort_keys=True, separators=(",", ":")),
        }

    return apply


__all__ = [
    "FaissRebuildBackend",
    "KnowledgeGraphBackend",
    "ObsidianIndexBackend",
    "SqlMemoryBackend",
    "logical_backend",
]
