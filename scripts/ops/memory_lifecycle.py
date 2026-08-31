#!/usr/bin/env python3
"""Inspect and safely propagate MemoryRecord v2 tombstones."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.memory_backends import (
    FaissRebuildBackend,
    KnowledgeGraphBackend,
    ObsidianIndexBackend,
    SqlMemoryBackend,
    logical_backend,
)
from magi_v3.memory_lifecycle import MemoryLifecycleError, MemoryLifecycleStore


def _json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _apply_backend(store, record, backend, adapter) -> str:
    try:
        result = dict(adapter(record))
        store.mark_backend(
            record.memory_id,
            backend,
            str(result.get("status") or "failed"),
            receipt_sha256=str(result.get("receipt_sha256") or ""),
            detail=str(result.get("detail") or ""),
        )
        return str(result.get("status") or "failed")
    except Exception as exc:
        store.mark_backend(record.memory_id, backend, "failed", detail=type(exc).__name__)
        return "failed"


def propagate(store: MemoryLifecycleStore, memory_id: str) -> dict:
    record = store.get(memory_id)
    if record.status != "tombstoned":
        raise MemoryLifecycleError("record must be tombstoned before propagation")

    from skills.memory.local_db import LOCAL_DB_CONFIG
    from skills.memory.mem_bridge import DB_CONFIG

    keeper_status = _apply_backend(store, record, "keeper", SqlMemoryBackend("keeper", DB_CONFIG))
    local_status = _apply_backend(store, record, "mariadb", SqlMemoryBackend("mariadb", LOCAL_DB_CONFIG))
    sql_terminal = {"not_present", "tombstoned", "not_applicable"}
    if keeper_status in sql_terminal and local_status in sql_terminal:
        _apply_backend(store, record, "primary", logical_backend("primary", "not_present"))
        _apply_backend(store, record, "faiss", FaissRebuildBackend(DB_CONFIG))

    runtime = Path(str(os.environ.get("MAGI_RUNTIME_DIR") or ROOT)).expanduser()
    graph_path = Path(
        str(os.environ.get("MAGI_GRAPH_STORE_PATH") or runtime / "architecture_graph.json")
    ).expanduser()
    _apply_backend(store, record, "knowledge_graph", KnowledgeGraphBackend(graph_path))

    obsidian_agent = str(os.environ.get("MAGI_OBSIDIAN_AGENT_DIR") or "").strip()
    if obsidian_agent:
        _apply_backend(store, record, "obsidian", ObsidianIndexBackend(Path(obsidian_agent) / "obsidian_index.json"))
    else:
        _apply_backend(store, record, "obsidian", logical_backend("obsidian", "not_applicable"))
    _apply_backend(store, record, "backup_index", logical_backend("backup_index", "tombstoned"))
    return store.deletion_consistency(memory_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI MemoryRecord v2 lifecycle")
    parser.add_argument("action", choices=("view", "archive", "tombstone", "status", "propagate"))
    parser.add_argument("memory_id")
    parser.add_argument("--reason", default="")
    parser.add_argument("--actor", default="magi-operator")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-memory-id", default="")
    args = parser.parse_args(argv)
    store = MemoryLifecycleStore.from_env()
    try:
        if args.action == "view":
            _json(store.get(args.memory_id).to_dict())
        elif args.action == "archive":
            _json(store.archive(args.memory_id, reason=args.reason, actor=args.actor).to_dict())
        elif args.action == "tombstone":
            _json(store.tombstone(args.memory_id, reason=args.reason, actor=args.actor).to_dict())
        elif args.action == "status":
            _json(store.deletion_consistency(args.memory_id))
        else:
            if not args.apply or args.confirm_memory_id != args.memory_id:
                raise MemoryLifecycleError(
                    "propagation requires --apply and an exact matching --confirm-memory-id"
                )
            _json(propagate(store, args.memory_id))
    except MemoryLifecycleError as exc:
        _json({"ok": False, "error": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
