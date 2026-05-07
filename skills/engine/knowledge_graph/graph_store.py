from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import networkx as nx


class GraphStore:
    _LOAD_CACHE: Dict[str, Dict[str, Any]] = {}
    _CACHE_STATS: Dict[str, int] = {"hits": 0, "misses": 0}

    def __init__(self, graph: Optional[nx.DiGraph] = None) -> None:
        self.graph = graph or nx.DiGraph()

    def upsert_node(self, node_id: str, **attrs: Any) -> None:
        current = dict(self.graph.nodes.get(node_id, {}))
        current.update({k: v for k, v in attrs.items() if v is not None})
        self.graph.add_node(node_id, **current)

    def add_edge(self, source: str, target: str, relation: str, weight: float = 1.0, **attrs: Any) -> None:
        edge_attrs = dict(attrs)
        edge_attrs["relation"] = relation
        edge_attrs["weight"] = float(weight)
        self.graph.add_edge(source, target, **edge_attrs)

    def neighbors(self, node_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for target in self.graph.successors(node_id):
            edge = dict(self.graph.get_edge_data(node_id, target) or {})
            rows.append(
                {
                    "source": node_id,
                    "target": target,
                    "relation": edge.get("relation", ""),
                    "weight": float(edge.get("weight", 1.0) or 1.0),
                    "target_attrs": dict(self.graph.nodes.get(target, {})),
                }
            )
        rows.sort(key=lambda item: item["weight"], reverse=True)
        return rows[: max(1, int(limit))]

    def find_nodes(self, tokens: Iterable[str], limit: int = 10) -> List[Dict[str, Any]]:
        wanted = [str(token or "").strip().lower() for token in tokens if str(token or "").strip()]
        rows: List[Dict[str, Any]] = []
        if not wanted:
            return rows
        for node_id, attrs in self.graph.nodes(data=True):
            haystack = " ".join(
                [
                    str(node_id),
                    str(attrs.get("label", "")),
                    str(attrs.get("kind", "")),
                    str(attrs.get("aliases", "")),
                ]
            ).lower()
            score = sum(1 for token in wanted if token in haystack)
            if score:
                rows.append({"id": node_id, "score": score, "attrs": dict(attrs)})
        rows.sort(key=lambda item: (item["score"], item["id"]), reverse=True)
        return rows[: max(1, int(limit))]

    def save(self, path: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = nx.node_link_data(self.graph)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str) -> "GraphStore":
        source = Path(path)
        if not source.exists():
            return cls()
        cache_key = str(source.resolve())
        stat = source.stat()
        cached = cls._LOAD_CACHE.get(cache_key)
        if cached and cached.get("mtime_ns") == stat.st_mtime_ns and cached.get("size") == stat.st_size:
            cls._CACHE_STATS["hits"] += 1
            return cls(cached["graph"].copy())
        cls._CACHE_STATS["misses"] += 1
        data = json.loads(source.read_text(encoding="utf-8"))
        graph = nx.node_link_graph(data, directed=True)
        cls._LOAD_CACHE[cache_key] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "graph": graph,
        }
        return cls(graph.copy())

    @classmethod
    def cache_stats(cls):
        # type: () -> Dict[str, int]
        """Return cache hit/miss counters."""
        return dict(cls._CACHE_STATS)
