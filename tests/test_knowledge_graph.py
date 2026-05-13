from __future__ import annotations

from pathlib import Path

from skills.engine.knowledge_graph import GraphStore, build_relations, detect_communities, extract_entities, graph_context


def test_graph_store_roundtrip(tmp_path: Path):
    store = GraphStore()
    store.upsert_node("民法第184條", label="民法第184條", kind="article")
    store.upsert_node("侵權行為", label="侵權行為", kind="keyword")
    store.add_edge("侵權行為", "民法第184條", "applies_to", weight=2.0)

    path = tmp_path / "graph.json"
    store.save(str(path))

    loaded = GraphStore.load(str(path))
    neighbors = loaded.neighbors("侵權行為")
    assert neighbors[0]["target"] == "民法第184條"
    assert neighbors[0]["relation"] == "applies_to"


def test_entity_and_relation_extraction():
    entities = extract_entities("民法第184條與114年度台上字第3753號都提到侵權行為")
    values = {item["value"] for item in entities}
    assert "民法第184條" in values
    assert any("侵權行為" in value for value in values)

    relations = build_relations("民法第184條與侵權行為")
    assert relations
    assert relations[0]["relation"] == "co_occurs"


def test_graph_context_reads_store(tmp_path: Path):
    store = GraphStore()
    store.upsert_node("預售屋遲延交屋", label="預售屋遲延交屋", kind="keyword")
    store.upsert_node("民法第184條", label="民法第184條", kind="article")
    store.add_edge("預售屋遲延交屋", "民法第184條", "related_to", weight=1.0)
    path = tmp_path / "graph.json"
    store.save(str(path))

    items = graph_context("預售屋遲延交屋", graph_path=str(path))
    assert items
    assert any("民法第184條" in item["content"] for item in items)


def test_graph_store_load_uses_cache_and_invalidates_on_change(tmp_path: Path):
    path = tmp_path / "graph.json"
    first = GraphStore()
    first.upsert_node("a", label="A")
    first.save(str(path))

    loaded_a = GraphStore.load(str(path))
    loaded_b = GraphStore.load(str(path))
    assert "a" in loaded_a.graph.nodes
    assert "a" in loaded_b.graph.nodes

    second = GraphStore()
    second.upsert_node("b", label="B")
    second.save(str(path))

    loaded_c = GraphStore.load(str(path))
    assert "b" in loaded_c.graph.nodes
    assert "a" not in loaded_c.graph.nodes


def test_community_detector_fallback_or_leiden():
    store = GraphStore()
    store.upsert_node("a")
    store.upsert_node("b")
    store.add_edge("a", "b", "related_to")
    communities = detect_communities(store.graph)
    assert communities
    assert communities[0]["size"] >= 2
