# -*- coding: utf-8 -*-

from skills.engine import chinese_nlp


class _DummySegmenter:
    def cut(self, text):
        return ["消費者債務清理條例", "之", "更生方案", "在", "法院"]

    def cut_many(self, texts):
        return [
            ["消費者債務清理條例", "之", "更生方案"],
            ["法律扶助", "在", "法院"],
        ]


def test_extract_keywords_removes_stopwords_and_preserves_order(monkeypatch):
    monkeypatch.setattr(chinese_nlp, "get_segmenter", lambda: _DummySegmenter())
    keywords = chinese_nlp.extract_keywords("ignored", max_keywords=10)
    assert keywords == ["消費者債務清理條例", "更生方案", "法院"]


def test_segment_for_indexing_many_only_processes_chinese(monkeypatch):
    monkeypatch.setattr(chinese_nlp, "get_segmenter", lambda: _DummySegmenter())
    out = chinese_nlp.segment_for_indexing_many(
        ["消費者債務清理條例之更生方案", "plain english text"],
    )
    assert out[0] == "消費者債務清理條例 更生方案"
    assert out[1] == "plain english text"


def test_sidecar_segmenter_parses_json(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = '[["法律扶助","更生方案"]]'
        stderr = ""

    monkeypatch.setattr(chinese_nlp.subprocess, "run", lambda *args, **kwargs: _Proc())
    seg = chinese_nlp._SidecarPKUSegSegmenter("/tmp/fake-python")
    assert seg.cut("法律扶助更生方案") == ["法律扶助", "更生方案"]
