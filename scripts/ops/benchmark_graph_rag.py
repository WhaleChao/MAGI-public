#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path
import sys

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from skills.engine.knowledge_graph import GraphStore, graph_context


QUERIES = [
    "預售屋遲延交屋",
    "侵權行為損害賠償",
    "消費者債務清理條例更生",
    "法扶結案報告",
    "閱卷聲請流程",
]


def _build_fixture_graph(graph_path: str) -> None:
    store = GraphStore()
    store.upsert_node("預售屋遲延交屋", label="預售屋遲延交屋", kind="issue")
    store.upsert_node("民法第229條", label="民法第229條", kind="statute")
    store.upsert_node("侵權行為損害賠償", label="侵權行為損害賠償", kind="issue")
    store.upsert_node("民法第184條", label="民法第184條", kind="statute")
    store.upsert_node("消費者債務清理條例更生", label="消費者債務清理條例更生", kind="procedure")
    store.upsert_node("法扶結案報告", label="法扶結案報告", kind="laf")
    store.upsert_node("閱卷流程", label="閱卷流程", kind="file_review")
    store.add_edge("預售屋遲延交屋", "民法第229條", "applies_to", weight=1.0)
    store.add_edge("侵權行為損害賠償", "民法第184條", "applies_to", weight=1.0)
    store.add_edge("消費者債務清理條例更生", "法扶結案報告", "related_to", weight=0.6)
    store.add_edge("閱卷流程", "法扶結案報告", "adjacent_to", weight=0.4)
    store.save(graph_path)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="magi_graph_rag_") as tmpdir:
        graph_path = str(Path(tmpdir) / "graph.json")
        _build_fixture_graph(graph_path)

        latencies_ms = []
        hits = []
        for query in QUERIES:
            started = time.perf_counter()
            items = graph_context(query, top_k=5, graph_path=graph_path)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            latencies_ms.append(elapsed_ms)
            hits.append(bool(items))

        ordered = sorted(latencies_ms)
        p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
        result = {
            "success": True,
            "queries": len(QUERIES),
            "hit_rate": sum(1 for item in hits if item) / max(1, len(hits)),
            "latency_ms": {
                "min": round(min(latencies_ms), 3),
                "mean": round(statistics.mean(latencies_ms), 3),
                "max": round(max(latencies_ms), 3),
                "p95": round(ordered[p95_index], 3),
            },
            "target_met": ordered[p95_index] < 200.0,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
