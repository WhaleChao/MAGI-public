from skills.documents import vector_pipeline


def test_prepare_embedding_inputs_uses_chinese_nlp(monkeypatch):
    monkeypatch.setattr(
        vector_pipeline,
        "_prepare_embedding_inputs",
        lambda parts: ["分詞 一", "分詞 二"],
    )

    captured = {}

    def _fake_remember_batch(items):
        captured["items"] = items
        return {"inserted": len(items)}

    monkeypatch.setattr(vector_pipeline, "remember_batch", _fake_remember_batch)
    monkeypatch.setattr(vector_pipeline, "_save_index", lambda data: None)

    result = vector_pipeline.ingest_text_to_vector_memory(
        kind="file",
        primary="doc-1",
        title="測試文件",
        text="A" * 2600,
        chunk_chars=1200,
        overlap=0,
        max_chunks_total=2,
    )

    assert result["success"] is True
    assert len(captured["items"]) == 2
    assert captured["items"][0]["embedding_input"] == "分詞 一"
    assert captured["items"][1]["embedding_input"] == "分詞 二"


def test_ingest_sections_to_vector_memory_initializes_budget(monkeypatch):
    monkeypatch.setattr(vector_pipeline, "_prepare_embedding_inputs", lambda parts: list(parts))
    monkeypatch.setattr(vector_pipeline, "_save_index", lambda data: None)
    monkeypatch.setattr(
        vector_pipeline,
        "remember_batch",
        lambda items: {"inserted": len(items), "failed": 0, "total": len(items)},
    )

    result = vector_pipeline.ingest_sections_to_vector_memory(
        url="https://example.com/doc",
        title="Example",
        sections=[{"id": "s1", "title": "Section 1", "content": "甲" * 2600}],
        chunk_chars=1200,
        overlap=0,
        max_chunks_total=3,
    )

    assert result["success"] is True
    assert result["chunks_written"] == 3
