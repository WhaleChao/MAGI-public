"""
Tests for skills/docx-editor/lib/anchor_matcher.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "docx-editor"))

from lib.anchor_matcher import (
    find_unique_anchor,
    find_anchor_in_paragraphs,
    normalize_ws,
)


class TestFindUniqueAnchor:
    def test_unique_anchor_simple(self):
        """純文字唯一找到"""
        text = "This is a simple test document."
        offset, status = find_unique_anchor(text, "This is ", "a simple", " test")
        assert status == "ok"
        assert offset == 8  # 'a simple' starts at index 8

    def test_anchor_not_found(self):
        """find 不在 full_text → not_found"""
        text = "Hello World"
        offset, status = find_unique_anchor(text, "", "不存在的文字", "")
        assert status == "not_found"
        assert offset is None

    def test_anchor_ambiguous_find_twice_but_anchor_unique(self):
        """find 出現 2+ 次但 full anchor 仍唯一 → ok"""
        text = "foo bar baz foo qux"
        # "foo" appears twice, but with context "baz foo qux" it's unique
        offset, status = find_unique_anchor(text, "baz ", "foo", " qux")
        assert status == "ok"
        assert text[offset:offset + 3] == "foo"

    def test_anchor_ambiguous_with_anchor(self):
        """full anchor 仍出現 2 次 → ambiguous（大小寫完全相同）"""
        text = "the cat sat on the mat. the cat sat on the mat."
        offset, status = find_unique_anchor(text, "the ", "cat", " sat")
        assert status == "ambiguous"
        assert offset is None

    def test_empty_context_before(self):
        """find 在文檔開頭（context_before == ""）"""
        text = "Hello World, this is a test."
        offset, status = find_unique_anchor(text, "", "Hello", " World")
        assert status == "ok"
        assert offset == 0

    def test_empty_context_after(self):
        """find 在文檔結尾（context_after == ""）"""
        text = "This is a test document."
        offset, status = find_unique_anchor(text, "test ", "document.", "")
        assert status == "ok"
        assert text[offset:] == "document."

    def test_pure_insert_with_empty_find(self):
        """find='' 純插入，只用 context 定位插入點"""
        text = "before_text after_text"
        offset, status = find_unique_anchor(text, "before_text", "", " after_text")
        assert status == "ok"
        assert offset == len("before_text")

    def test_whitespace_normalization(self):
        """連續空白視為等價"""
        text = "Section  4.2 is about normalization."
        # The text has double space between Section and 4.2
        offset, status = find_unique_anchor(text, "Section ", "4.2", " is about")
        assert status == "ok"
        assert "4.2" in text[offset:offset + 3]

    def test_smart_quotes_normalization(self):
        """智慧引號應被正規化"""
        text = 'He said "hello" to her.'
        # Query uses smart quotes that should normalize to regular quotes
        offset, status = find_unique_anchor(text, "said ", '"hello"', " to")
        assert status == "ok"

    def test_chinese_text(self):
        """中文書狀文字"""
        text = "被告於民國一一四年三月一日，在臺北市中正區犯罪。"
        offset, status = find_unique_anchor(text, "民國", "一一四年三月一日", "，在")
        assert status == "ok"
        assert text[offset:offset + 8] == "一一四年三月一日"

    def test_find_not_in_text_at_all(self):
        """find 完全不在文字中"""
        offset, status = find_unique_anchor("hello world", "", "xyz", "")
        assert status == "not_found"

    def test_multi_paragraph_unique(self):
        """find_anchor_in_paragraphs: 跨段搜尋，唯一匹配"""
        paras = [
            "First paragraph with some text.",
            "Second paragraph has the target word: unique_token here.",
            "Third paragraph is different.",
        ]
        pi, start, end, status = find_anchor_in_paragraphs(
            paras, "target word: ", "unique_token", " here."
        )
        assert status == "ok"
        assert pi == 1
        assert paras[pi][start:end] == "unique_token"

    def test_multi_paragraph_not_found(self):
        """find_anchor_in_paragraphs: 找不到"""
        paras = ["Line one.", "Line two.", "Line three."]
        pi, start, end, status = find_anchor_in_paragraphs(paras, "", "不存在", "")
        assert status == "not_found"
        assert pi is None
