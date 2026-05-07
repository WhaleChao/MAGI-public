"""
Tests for skills/docx-editor/lib/run_splitter.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "docx-editor"))

from lxml import etree
from lib.run_splitter import flatten_paragraph, collapse_diff, FlatParagraph

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_para_xml(runs_data):
    """
    Helper: build a <w:p> element from list of (text, bold) tuples.
    Each tuple becomes one <w:r> with optional <w:rPr><w:b/></w:rPr>.
    """
    p = etree.Element(f"{{{W}}}p")
    for text, bold in runs_data:
        r = etree.SubElement(p, f"{{{W}}}r")
        if bold:
            rpr = etree.SubElement(r, f"{{{W}}}rPr")
            etree.SubElement(rpr, f"{{{W}}}b")
        t = etree.SubElement(r, f"{{{W}}}t")
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = text
    return p


class TestFlattenParagraph:
    def test_single_run(self):
        """單個 run 平面化"""
        p = make_para_xml([("Hello World", False)])
        flat = flatten_paragraph(p)
        assert flat.para_text == "Hello World"
        assert len(flat.runs) == 1
        assert len(flat.char_run) == 11

    def test_multiple_runs(self):
        """多個 run 平面化"""
        p = make_para_xml([("The quick ", False), ("brown fox", True), (" jumps", False)])
        flat = flatten_paragraph(p)
        assert flat.para_text == "The quick brown fox jumps"
        assert len(flat.runs) == 3
        assert flat.char_run[0] == 0   # 'T' belongs to run 0
        assert flat.char_run[10] == 1  # 'b' in 'brown' belongs to run 1
        assert flat.char_run[19] == 2  # ' jumps' belongs to run 2

    def test_run_boundaries(self):
        """run 邊界正確記錄"""
        p = make_para_xml([("AB", False), ("CD", True)])
        flat = flatten_paragraph(p)
        assert flat.runs[0].para_start == 0
        assert flat.runs[0].para_end == 2
        assert flat.runs[1].para_start == 2
        assert flat.runs[1].para_end == 4

    def test_rpr_preserved(self):
        """bold run 的 rPr 被記錄"""
        p = make_para_xml([("normal", False), ("bold", True)])
        flat = flatten_paragraph(p)
        assert flat.runs[0].rpr is None
        assert flat.runs[1].rpr is not None
        # rPr should have w:b child
        children_tags = [c.tag for c in flat.runs[1].rpr]
        assert f"{{{W}}}b" in children_tags

    def test_del_text_skipped(self):
        """<w:del> 內的文字在 accepted view 中跳過"""
        p = etree.Element(f"{{{W}}}p")
        # Normal run
        r1 = etree.SubElement(p, f"{{{W}}}r")
        t1 = etree.SubElement(r1, f"{{{W}}}t")
        t1.text = "visible "
        # Del element (should be skipped)
        del_el = etree.SubElement(p, f"{{{W}}}del")
        del_el.set(f"{{{W}}}id", "1")
        del_el.set(f"{{{W}}}author", "test")
        del_el.set(f"{{{W}}}date", "2026-01-01T00:00:00Z")
        r2 = etree.SubElement(del_el, f"{{{W}}}r")
        dt = etree.SubElement(r2, f"{{{W}}}delText")
        dt.text = "deleted"
        # Normal run after del
        r3 = etree.SubElement(p, f"{{{W}}}r")
        t3 = etree.SubElement(r3, f"{{{W}}}t")
        t3.text = "text"

        flat = flatten_paragraph(p)
        assert flat.para_text == "visible text"
        assert "deleted" not in flat.para_text

    def test_ins_included_in_accepted_view(self):
        """<w:ins> 內的文字在 accepted view 中包含"""
        p = etree.Element(f"{{{W}}}p")
        r1 = etree.SubElement(p, f"{{{W}}}r")
        t1 = etree.SubElement(r1, f"{{{W}}}t")
        t1.text = "before "
        ins_el = etree.SubElement(p, f"{{{W}}}ins")
        ins_el.set(f"{{{W}}}id", "2")
        ins_el.set(f"{{{W}}}author", "test")
        ins_el.set(f"{{{W}}}date", "2026-01-01T00:00:00Z")
        r2 = etree.SubElement(ins_el, f"{{{W}}}r")
        t2 = etree.SubElement(r2, f"{{{W}}}t")
        t2.text = "inserted"
        r3 = etree.SubElement(p, f"{{{W}}}r")
        t3 = etree.SubElement(r3, f"{{{W}}}t")
        t3.text = " after"

        flat = flatten_paragraph(p)
        assert flat.para_text == "before inserted after"

    def test_empty_paragraph(self):
        """空段落"""
        p = etree.Element(f"{{{W}}}p")
        flat = flatten_paragraph(p)
        assert flat.para_text == ""
        assert len(flat.runs) == 0


class TestCollapseDiff:
    def test_full_replacement(self):
        """完全替換（無公共前後綴）"""
        deleted, inserted, leading, trailing = collapse_diff("abc", "xyz")
        assert deleted == "abc"
        assert inserted == "xyz"
        assert leading == 0
        assert trailing == 0

    def test_common_prefix(self):
        """有公共前綴"""
        deleted, inserted, leading, trailing = collapse_diff("Hello World", "Hello MAGI")
        assert deleted == "World"
        assert inserted == "MAGI"
        assert leading == 6

    def test_common_suffix(self):
        """有公共後綴"""
        deleted, inserted, leading, trailing = collapse_diff("old text", "new text")
        assert deleted == "old"
        assert inserted == "new"
        assert trailing == 5  # " text" is 5 chars

    def test_identical(self):
        """完全相同（不應發生但要 handle）"""
        deleted, inserted, leading, trailing = collapse_diff("same", "same")
        assert deleted == ""
        assert inserted == ""

    def test_pure_deletion(self):
        """純刪除"""
        deleted, inserted, leading, trailing = collapse_diff("delete this", "")
        assert deleted == "delete this"
        assert inserted == ""

    def test_pure_insertion(self):
        """純插入（find 為空字串）"""
        deleted, inserted, leading, trailing = collapse_diff("", "new content")
        assert deleted == ""
        assert inserted == "new content"
