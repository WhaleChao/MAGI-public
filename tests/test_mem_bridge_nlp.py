from skills.memory import mem_bridge


def test_augment_query_for_retrieval_appends_chinese_keywords(monkeypatch):
    monkeypatch.setattr(mem_bridge, "_query_prefers_chatlog", lambda query: False)
    monkeypatch.setattr(
        mem_bridge,
        "_augment_query_for_retrieval",
        mem_bridge._augment_query_for_retrieval,
    )

    import skills.engine.chinese_nlp as chinese_nlp

    monkeypatch.setattr(
        chinese_nlp,
        "extract_keywords",
        lambda text, max_keywords=5: ["消費者債務清理條例", "更生方案"],
    )

    out = mem_bridge._augment_query_for_retrieval("請找消費者債務清理條例的更生方案重點")
    assert out.startswith("請找消費者債務清理條例的更生方案重點")
    assert "消費者債務清理條例" not in out[len("請找消費者債務清理條例的更生方案重點") :]


def test_remember_batch_uses_embedding_input_when_present(monkeypatch):
    captured = {}

    monkeypatch.setattr(mem_bridge, "_keeper_offline", lambda: True)
    monkeypatch.setattr(mem_bridge, "_save_local_backup", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        mem_bridge,
        "get_embeddings_batch",
        lambda texts, batch_size=32: captured.setdefault("texts", list(texts)) or [[0.1, 0.2]],
    )

    result = mem_bridge.remember_batch(
        [{"content": "原始內容", "source": "manual", "embedding_input": "分詞 索引"}]
    )

    assert result["inserted"] == 1
    assert captured["texts"] == ["分詞 索引"]


def test_recall_merges_graph_context_when_keeper_is_offline(monkeypatch):
    monkeypatch.setattr(mem_bridge, "_keeper_offline", lambda: True)
    monkeypatch.setattr(mem_bridge, "_fallback_local_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        mem_bridge,
        "_graph_context_results",
        lambda query, want, source_contains="": [
            {"id": "graph:1", "content": "[Graph] 預售屋遲延交屋", "source": "graph_rag|node=預售屋", "score": 0.18}
        ],
    )

    results = mem_bridge.recall("預售屋遲延交屋", top_k=3)

    assert results
    assert results[0]["source"].startswith("graph_rag|")
