"""
tests/test_citation_format.py

Phase 5: citation_format module tests.
"""

import pytest
from skills.bridge.citation_format import (
    Citation,
    ParsedAnswer,
    parse_citations,
    render_citations_for_telegram,
    build_citation_system_prompt,
    CITATION_BLOCK_RE,
)


# ── Test 1: happy path ─────────────────────────────────────────────────────────

def test_parse_citations_happy_path():
    answer = """依據[1]，被告應負損害賠償責任。另依[2]，消滅時效為兩年。

<CITATIONS>
[
  {"ref": 1, "doc_id": "doc-0", "page": "3", "quote": "被告應負損害賠償責任"},
  {"ref": 2, "doc_id": "doc-1", "page": "41-42", "quote": "消滅時效為兩年"}
]
</CITATIONS>"""

    result = parse_citations(answer)
    assert len(result.citations) == 2
    assert result.citations[0].ref == 1
    assert result.citations[0].doc_id == "doc-0"
    assert result.citations[0].page == "3"
    assert result.citations[0].quote == "被告應負損害賠償責任"
    assert result.citations[1].ref == 2
    assert result.citations[1].page == "41-42"
    assert result.parse_warnings == []
    # prose should not contain <CITATIONS> block
    assert "<CITATIONS>" not in result.prose
    assert "依據[1]" in result.prose


# ── Test 2: no <CITATIONS> block ───────────────────────────────────────────────

def test_parse_citations_no_block():
    answer = "本案當事人應自行協商。"
    result = parse_citations(answer)
    assert result.citations == []
    assert result.prose == answer
    assert result.parse_warnings == []


# ── Test 3: malformed JSON → warnings but no raise ────────────────────────────

def test_parse_citations_malformed_json():
    answer = """正文。

<CITATIONS>
[{"ref": 1, "doc_id": INVALID_JSON}]
</CITATIONS>"""

    result = parse_citations(answer)
    assert result.citations == []
    assert len(result.parse_warnings) == 1
    assert "JSON parse" in result.parse_warnings[0]
    # prose still returned
    assert "正文" in result.prose


# ── Test 4: missing fields → individual citation skipped ──────────────────────

def test_parse_citations_missing_fields():
    answer = """正文[1]。

<CITATIONS>
[
  {"ref": 1, "doc_id": "doc-0"},
  {"ref": 2, "doc_id": "doc-1", "page": "5", "quote": "完整引用"}
]
</CITATIONS>"""

    result = parse_citations(answer)
    # First citation missing page + quote → skipped
    assert len(result.citations) == 1
    assert result.citations[0].ref == 2
    assert len(result.parse_warnings) >= 1
    assert any("缺欄位" in w for w in result.parse_warnings)


# ── Test 5: quote > 25 words → warning but citation kept ──────────────────────

def test_parse_citations_long_quote_warns():
    long_quote = " ".join(["word"] * 30)  # 30 words
    answer = f"""正文[1]。

<CITATIONS>
[{{"ref": 1, "doc_id": "doc-0", "page": "1", "quote": "{long_quote}"}}]
</CITATIONS>"""

    result = parse_citations(answer)
    assert len(result.citations) == 1
    assert result.citations[0].ref == 1
    # Should have a warning about long quote
    assert any("quote 超過" in w for w in result.parse_warnings)


# ── Test 6: unmatched inline [N] → warning ────────────────────────────────────

def test_parse_citations_unmatched_inline_ref():
    answer = """正文[1]和[3]都有引用。

<CITATIONS>
[{"ref": 1, "doc_id": "doc-0", "page": "1", "quote": "某段文字"}]
</CITATIONS>"""

    result = parse_citations(answer)
    assert len(result.citations) == 1
    assert any("3" in w for w in result.parse_warnings)


# ── Test 7: render_citations_for_telegram format ──────────────────────────────

def test_render_citations_for_telegram():
    parsed = ParsedAnswer(
        prose="依據[1]，被告應負責。",
        citations=[
            Citation(ref=1, doc_id="doc-0", page="3", quote="被告應負責"),
        ],
        parse_warnings=[],
    )
    rendered = render_citations_for_telegram(parsed)
    assert "📄 引用：" in rendered
    assert "[1] doc-0 p.3：「被告應負責」" in rendered
    assert "依據[1]" in rendered


# ── Test 8: render with no citations → just prose ─────────────────────────────

def test_render_citations_no_citations():
    parsed = ParsedAnswer(prose="純文字答覆。", citations=[], parse_warnings=[])
    rendered = render_citations_for_telegram(parsed)
    assert rendered == "純文字答覆。"
    assert "📄" not in rendered


# ── Test 9: build_citation_system_prompt returns non-empty string ──────────────

def test_build_citation_system_prompt():
    prompt = build_citation_system_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 100
    assert "<CITATIONS>" in prompt
    assert "ref" in prompt
    assert "doc_id" in prompt


# ── Test 10: CITATION_BLOCK_RE regex ──────────────────────────────────────────

def test_citation_block_re_multiline():
    text = "<CITATIONS>\n[\n{\"ref\": 1}\n]\n</CITATIONS>"
    m = CITATION_BLOCK_RE.search(text)
    assert m is not None
    assert "ref" in m.group(1)
