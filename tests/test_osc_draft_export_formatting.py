from pathlib import Path


def test_clean_draft_output_keeps_fenced_pleading_text():
    from api.osc.drafts import _osc_clean_draft_output

    raw = """```text
# 民事準備書狀
**案號：**113年度訴字第100號

一、原告主張如下。
```"""

    cleaned = _osc_clean_draft_output(raw)

    assert "民事準備書狀" in cleaned
    assert "113年度訴字第100號" in cleaned
    assert "一、原告主張如下。" in cleaned
    assert "```" not in cleaned
    assert "#" not in cleaned
    assert "**" not in cleaned


def test_export_form_docx_uses_pleading_layout(tmp_path, monkeypatch):
    from docx import Document
    from api import startup

    monkeypatch.setattr(startup, "EXPORTS_DIR", str(tmp_path))
    text = """```text
# 民事準備書狀
案號：113年度訴字第100號　股別：義股

一、原告就本案爭點補充說明如下。
（一）被告仍未提出付款證明。

具狀人：測試律師
中華民國115年5月8日
```"""

    meta = startup._export_form_docx(text, "layout-test", title="民事準備書狀")

    assert meta["success"] is True
    path = Path(meta["path"])
    assert path.exists()

    doc = Document(path)
    paragraphs = [p for p in doc.paragraphs if p.text.strip()]
    assert paragraphs[0].text == "民事準備書狀"
    assert paragraphs[0].alignment == 1  # CENTER
    assert paragraphs[1].text.startswith("案號：")
    assert paragraphs[1].paragraph_format.first_line_indent is None
    assert paragraphs[2].text.startswith("一、")
    assert paragraphs[2].paragraph_format.first_line_indent is not None
    assert paragraphs[3].text.startswith("（一）")
    assert paragraphs[3].paragraph_format.first_line_indent is not None
    assert paragraphs[-1].text.startswith("中華民國")
    assert paragraphs[-1].alignment == 2  # RIGHT
    assert all("```" not in p.text and "#" not in p.text for p in paragraphs)


def test_export_html_strips_markdown_and_applies_body_classes():
    from api.startup import _render_form_text_to_html

    html = _render_form_text_to_html(
        "民事起訴狀",
        "# 民事起訴狀\n案號：113年度訴字第1號\n\n一、請求內容。\n具狀人：測試",
    )

    assert "<h1>民事起訴狀</h1>" in html
    assert "class='meta'>案號：" in html
    assert "class='body'>一、請求內容。" in html
    assert "class='signature'>具狀人：測試" in html
    assert "# 民事起訴狀" not in html


def test_export_osc_form_files_produces_docx_and_pdf(tmp_path, monkeypatch):
    from api import startup

    monkeypatch.setattr(startup, "EXPORTS_DIR", str(tmp_path))

    result = startup._export_osc_form_files(
        "民事準備書狀",
        "案號：113年度訴字第100號\n\n一、補充理由。\n具狀人：測試",
        "draft-export-smoke",
    )

    assert result["success"] is True
    assert result["export_docx"]["success"] is True
    assert Path(result["export_docx"]["path"]).exists()
    assert result["export_pdf"]["success"] is True
    assert Path(result["export_pdf"]["path"]).exists()
