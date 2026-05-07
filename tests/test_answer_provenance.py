"""
tests/test_answer_provenance.py
================================
answer_provenance 模組單元測試

涵蓋：
- build_provenance_footer：SIMPLE 不加頁尾 / COMPLEX 有記憶 / 有網路 / 無溯源警告
- _extract_web_titles：解析 (資料來源：...) 前綴
- _meaningful_memories：過濾對話記錄 / 信心門檻
- store_provenance + get_last_provenance：寫入讀取 / TTL 過期
- format_correction_context：格式化修正上下文
"""

import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from api.answer_provenance import (
    build_provenance_footer,
    store_provenance,
    get_last_provenance,
    format_correction_context,
    _extract_web_titles,
    _meaningful_memories,
    _label_source,
    _PROVENANCE_FILE,
)


# ─────────────────────────────────────────────────────────────────────────────
# _label_source
# ─────────────────────────────────────────────────────────────────────────────

def test_label_source_judgment():
    assert _label_source("judgment_archive") == "判決記錄"


def test_label_source_chatlog_hidden():
    """對話記錄來源應回傳 None（隱藏）"""
    assert _label_source("chatlog") is None


def test_label_source_assistant_hidden():
    assert _label_source("assistant_reply") is None


def test_label_source_obsidian():
    assert _label_source("obsidian_note") == "個人筆記"


def test_label_source_unknown_fallback():
    """未知來源 fallback 為「記憶庫」"""
    label = _label_source("some_unknown_source_xyz")
    assert label == "記憶庫"


def test_label_source_empty():
    assert _label_source("") is None


# ─────────────────────────────────────────────────────────────────────────────
# _extract_web_titles
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_web_titles_normal():
    ctx = "（資料來源：司法院, 中央氣象署）\n一些內容..."
    titles = _extract_web_titles(ctx)
    assert "司法院" in titles
    assert "中央氣象署" in titles


def test_extract_web_titles_empty():
    assert _extract_web_titles("無。") == []


def test_extract_web_titles_none():
    assert _extract_web_titles("") == []


def test_extract_web_titles_max_two():
    ctx = "（資料來源：A, B, C, D）\n..."
    titles = _extract_web_titles(ctx)
    assert len(titles) <= 2


def test_extract_web_titles_no_match():
    """沒有 (資料來源：...) 格式 → 空列表"""
    ctx = "這是沒有來源標記的網路內容"
    assert _extract_web_titles(ctx) == []


# ─────────────────────────────────────────────────────────────────────────────
# _meaningful_memories
# ─────────────────────────────────────────────────────────────────────────────

def test_meaningful_memories_filters_chatlog():
    mems = [
        {"source": "chatlog|", "content": "chat1", "confidence": 0.9},
        {"source": "judgment_archive", "content": "court1", "confidence": 0.8},
    ]
    result = _meaningful_memories(mems)
    assert len(result) == 1
    assert result[0]["source"] == "judgment_archive"


def test_meaningful_memories_filters_low_confidence():
    mems = [
        {"source": "judgment_archive", "content": "court1", "confidence": 0.05},
        {"source": "obsidian", "content": "note1", "confidence": 0.75},
    ]
    result = _meaningful_memories(mems)
    assert len(result) == 1
    assert result[0]["source"] == "obsidian"


def test_meaningful_memories_uses_score_fallback():
    """confidence=0 但 score 足夠高 → 應保留"""
    mems = [
        {"source": "legal_statute", "content": "law1", "confidence": 0, "score": 0.70},
    ]
    result = _meaningful_memories(mems)
    assert len(result) == 1


def test_meaningful_memories_empty():
    assert _meaningful_memories([]) == []


def test_meaningful_memories_max_three():
    mems = [
        {"source": "obsidian", "content": f"note{i}", "confidence": 0.8}
        for i in range(10)
    ]
    result = _meaningful_memories(mems)
    assert len(result) == 3


# ─────────────────────────────────────────────────────────────────────────────
# build_provenance_footer
# ─────────────────────────────────────────────────────────────────────────────

def test_footer_simple_tier_no_footer():
    """SIMPLE tier → 不加頁尾"""
    footer = build_provenance_footer(
        memories=[{"source": "judgment_archive", "content": "x", "confidence": 0.9}],
        web_context="無。",
        tier="SIMPLE",
        risk_level="HIGH",
    )
    assert footer == ""


def test_footer_greeting_tier_no_footer():
    footer = build_provenance_footer(
        memories=[{"source": "judgment_archive", "content": "x", "confidence": 0.9}],
        web_context="無。",
        tier="GREETING",
    )
    assert footer == ""


def test_footer_complex_with_memory():
    """COMPLEX + 有記憶庫 → 顯示來源頁尾"""
    mems = [{"source": "judgment_archive", "content": "判決內容", "confidence": 0.85}]
    footer = build_provenance_footer(
        memories=mems,
        web_context="無。",
        tier="COMPLEX",
    )
    assert footer != ""
    assert "記憶庫" in footer or "判決記錄" in footer
    assert "來源" in footer


def test_footer_complex_with_high_confidence_shows_score():
    """信心 ≥ 0.70 → 頁尾顯示信心值"""
    mems = [{"source": "verified_fact", "content": "fact", "confidence": 0.90}]
    footer = build_provenance_footer(
        memories=mems,
        web_context="無。",
        tier="COMPLEX",
    )
    assert "0.90" in footer


def test_footer_complex_with_web_source():
    """COMPLEX + 網路來源 → 顯示網路標籤"""
    footer = build_provenance_footer(
        memories=[],
        web_context="（資料來源：司法院判決資料庫）\n...",
        tier="COMPLEX",
    )
    assert "網路" in footer
    assert "司法院" in footer


def test_footer_complex_no_source_high_risk():
    """COMPLEX + 無溯源 + HIGH risk → 顯示 AI 訓練知識警告"""
    footer = build_provenance_footer(
        memories=[],
        web_context="無。",
        tier="COMPLEX",
        risk_level="HIGH",
    )
    assert "⚠️" in footer or "AI 訓練知識" in footer
    assert "法條" in footer


def test_footer_complex_no_source_safe_risk():
    """COMPLEX + 無溯源 + SAFE risk → 不加頁尾（閒聊不打擾）"""
    footer = build_provenance_footer(
        memories=[],
        web_context="無。",
        tier="COMPLEX",
        risk_level="SAFE",
    )
    assert footer == ""


def test_footer_has_correction_hint():
    """頁尾應包含修正引導提示"""
    mems = [{"source": "obsidian", "content": "note", "confidence": 0.8}]
    footer = build_provenance_footer(
        memories=mems,
        web_context="無。",
        tier="COMPLEX",
    )
    # Should hint at how to correct
    assert "不對" in footer or "有誤" in footer or "修正" in footer or "查證" in footer


def test_footer_hides_chatlog_source():
    """對話記錄 source 不應出現在頁尾"""
    mems = [
        {"source": "chatlog|", "content": "past chat", "confidence": 0.9},
    ]
    footer = build_provenance_footer(
        memories=mems,
        web_context="無。",
        tier="COMPLEX",
        risk_level="SAFE",
    )
    # No grounded source → empty footer (SAFE risk)
    assert footer == ""


# ─────────────────────────────────────────────────────────────────────────────
# store_provenance + get_last_provenance
# ─────────────────────────────────────────────────────────────────────────────

def test_store_and_retrieve_provenance(tmp_path, monkeypatch):
    """儲存後可讀回"""
    import api.answer_provenance as ap_mod
    tmp_file = str(tmp_path / "provenance.json")
    monkeypatch.setattr(ap_mod, "_PROVENANCE_FILE", tmp_file)

    mems = [
        {"source": "judgment_archive", "content": "判決摘要", "confidence": 0.8, "doc_id": "doc123"},
    ]
    web_ctx = "（資料來源：司法院）\n..."
    store_provenance("sess1", mems, web_ctx, "民法第184條侵權要件")

    prov = get_last_provenance("sess1")
    assert prov is not None
    assert "doc123" in prov.get("memory_doc_ids", [])
    assert prov.get("has_web") is True
    assert prov.get("has_grounded_memory") is True
    assert "民法第184條" in prov.get("query", "")


def test_get_provenance_returns_none_when_missing(tmp_path, monkeypatch):
    import api.answer_provenance as ap_mod
    monkeypatch.setattr(ap_mod, "_PROVENANCE_FILE", str(tmp_path / "missing.json"))
    assert get_last_provenance() is None


def test_get_provenance_stale_returns_none(tmp_path, monkeypatch):
    """過期記錄 → None"""
    import api.answer_provenance as ap_mod
    tmp_file = str(tmp_path / "provenance.json")
    monkeypatch.setattr(ap_mod, "_PROVENANCE_FILE", tmp_file)
    monkeypatch.setattr(ap_mod, "_PROVENANCE_TTL_SEC", 0)  # immediate expiry

    store_provenance("s", [], "無。", "test")
    time.sleep(0.01)
    assert get_last_provenance() is None


# ─────────────────────────────────────────────────────────────────────────────
# format_correction_context
# ─────────────────────────────────────────────────────────────────────────────

def test_format_correction_context_with_docs():
    prov = {
        "memory_doc_ids": ["doc1", "doc2"],
        "memory_sources": ["judgment_archive", "obsidian"],
        "memory_contents": ["判決摘要A", "筆記B"],
        "web_titles": ["司法院"],
    }
    text = format_correction_context(prov)
    assert "doc1" in text
    assert "判決記錄" in text
    assert "司法院" in text


def test_format_correction_context_empty():
    text = format_correction_context({})
    assert "無溯源" in text or text == ""


def test_format_correction_context_none():
    text = format_correction_context(None)
    assert text == ""
