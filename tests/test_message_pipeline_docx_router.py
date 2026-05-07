"""
tests/test_message_pipeline_docx_router.py

Phase 3: message_pipeline docx chat edit router tests.
"""

import os
import pytest
from unittest.mock import patch, MagicMock

from api.pipelines.message_pipeline import _handle_docx_chat_edit_if_any


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_orch():
    """Minimal mock orchestrator."""
    orch = MagicMock()
    orch._append_history = MagicMock()
    return orch


def _make_docx_attachment(path="/tmp/test.docx"):
    return {
        "type": "docx",
        "path": path,
        "filename": "test.docx",
    }


# ── Test 1: no attachment → not handled ────────────────────────────────────────

def test_router_no_attachment_not_handled():
    orch = _make_orch()
    handled, reply = _handle_docx_chat_edit_if_any(
        orch, "user1", "discord", "@MAGI 編輯 把甲方改乙方", None
    )
    assert handled is False
    assert reply == ""


# ── Test 2: attachment is not docx → not handled ──────────────────────────────

def test_router_non_docx_attachment_not_handled():
    orch = _make_orch()
    attachment = {"type": "pdf", "path": "/tmp/x.pdf", "filename": "x.pdf"}
    handled, reply = _handle_docx_chat_edit_if_any(
        orch, "user1", "discord", "@MAGI 編輯 把甲方改乙方", attachment
    )
    assert handled is False


# ── Test 3: docx attachment but no trigger word → not handled ─────────────────

def test_router_docx_no_trigger_not_handled():
    orch = _make_orch()
    attachment = _make_docx_attachment()
    handled, reply = _handle_docx_chat_edit_if_any(
        orch, "user1", "discord", "幫我看看這份文件", attachment
    )
    assert handled is False


# ── Test 4: docx + trigger word but file doesn't exist → handled with error ───

def test_router_docx_trigger_no_file():
    orch = _make_orch()
    attachment = _make_docx_attachment(path="/tmp/nonexistent_xyz_12345.docx")
    handled, reply = _handle_docx_chat_edit_if_any(
        orch, "user1", "discord", "@MAGI 編輯 把甲方改原告", attachment
    )
    assert handled is True
    assert "無法讀取" in reply or "⚠️" in reply


# ── Test 5: valid trigger words all detected ──────────────────────────────────

def test_router_trigger_words():
    """All trigger phrases should be detected."""
    from api.pipelines.message_pipeline import _DOCX_EDIT_TRIGGER_RE

    triggers = [
        "@MAGI 編輯 instruction",
        "@magi 修改 instruction",
        "編輯這份文件",
        "修改這份草稿",
        "edit this document",
    ]
    for t in triggers:
        assert _DOCX_EDIT_TRIGGER_RE.search(t), f"Trigger not detected: {t!r}"


# ── Test 6: non-triggers not detected ────────────────────────────────────────

def test_router_non_trigger_words():
    """Non-trigger phrases should not match."""
    from api.pipelines.message_pipeline import _DOCX_EDIT_TRIGGER_RE

    non_triggers = [
        "幫我看看這份文件",
        "這份文件要怎麼用",
        "MAGI 查詢",
        "download this",
    ]
    for t in non_triggers:
        assert not _DOCX_EDIT_TRIGGER_RE.search(t), f"False positive: {t!r}"


# ── Test 7: docx attachment with MIME type detected ───────────────────────────

def test_router_docx_mime_type_detected():
    """Attachment with MIME type should be recognized as docx."""
    orch = _make_orch()
    attachment = {
        "type": "file",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "path": "/tmp/nonexistent.docx",
        "filename": "contract.docx",
    }
    handled, reply = _handle_docx_chat_edit_if_any(
        orch, "user1", "telegram", "@MAGI 編輯 把甲方改原告", attachment
    )
    assert handled is True  # should be handled (even if file not found → error reply)
