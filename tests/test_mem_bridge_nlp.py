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


def test_remember_batch_dedupes_same_content_across_different_sources(monkeypatch):
    class _FakeCursor:
        def __init__(self):
            self._fetch_rows = []
            self.lastrowid = 100
            self.docs_inserted = 0
            self.vectors_inserted = 0

        def execute(self, sql, params=None):
            s = " ".join(str(sql).split()).strip().lower()
            if s.startswith("select distinct md5(content) from documents where md5(content) in"):
                self._fetch_rows = []
                return
            if s.startswith("insert into documents (content, source, synced)"):
                self.docs_inserted += 1
                self.lastrowid += 1
                return
            if s.startswith("insert into vectors"):
                self.vectors_inserted += 1
                return
            raise AssertionError(f"unexpected sql: {sql}")

        def fetchall(self):
            return list(self._fetch_rows)

        def close(self):
            return None

    class _FakeConn:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def commit(self):
            return None

        def is_connected(self):
            return True

        def close(self):
            return None

    fake_cursor = _FakeCursor()
    fake_conn = _FakeConn(fake_cursor)

    monkeypatch.setattr(mem_bridge, "_keeper_offline", lambda: False)
    monkeypatch.setattr(mem_bridge, "_get_conn", lambda: fake_conn)
    monkeypatch.setattr(mem_bridge, "get_embeddings_batch", lambda texts, batch_size=32: [[0.1], [0.2]])
    monkeypatch.setattr(mem_bridge, "ENABLE_FAISS", False)

    result = mem_bridge.remember_batch(
        [
            {"content": "完全相同內容", "source": "research-brief:語言政策"},
            {"content": "完全相同內容", "source": "research-brief:通譯"},
        ]
    )

    assert result["inserted"] == 1
    assert result["skipped"] == 1
    assert fake_cursor.docs_inserted == 1
    assert fake_cursor.vectors_inserted == 1
