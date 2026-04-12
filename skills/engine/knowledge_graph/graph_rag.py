from __future__ import annotations

import os
from typing import Dict, List

from .entity_extractor import extract_entities
from .graph_store import GraphStore


def _default_graph_path() -> str:
    return os.environ.get(
        "MAGI_GRAPH_STORE_PATH",
        "/Users/ai/Desktop/MAGI_v2_architecture_graph.json",
    )


def graph_context(query: str, top_k: int = 5, graph_path: str = "") -> List[Dict[str, str]]:
    store = GraphStore.load(graph_path or _default_graph_path())
    entities = extract_entities(query, max_keywords=5)
    tokens = [item["value"] for item in entities] or [str(query or "").strip()]
    matches = store.find_nodes(tokens, limit=top_k)
    context: List[Dict[str, str]] = []
    for match in matches:
        node_id = str(match["id"])
        attrs = match.get("attrs") or {}
        context.append(
            {
                "content": f"[Graph] {attrs.get('label') or node_id}",
                "source": f"graph_rag|node={node_id}",
            }
        )
        for rel in store.neighbors(node_id, limit=2):
            context.append(
                {
                    "content": f"[Graph] {node_id} --{rel.get('relation') or 'related_to'}--> {rel.get('target')}",
                    "source": f"graph_rag|edge={node_id}->{rel.get('target')}",
                }
            )
    return context[: max(1, int(top_k))]
