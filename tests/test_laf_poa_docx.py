from __future__ import annotations

import base64
import zipfile
from pathlib import Path
import sys
import types

from api.laf_branch_profiles import LawFirmProfile


def _docx_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def _docx_text(path: Path) -> str:
    import re

    return re.sub(r"<[^>]+>", "", _docx_xml(path))


def _make_pdf(path: Path, text: str = "法律扶助基金會委任狀") -> None:
    try:
        import fitz  # type: ignore
    except Exception:
        path.write_bytes(b"%PDF-1.4")
        return

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(fitz.Rect(72, 72, 540, 800), text, fontsize=12)
    doc.save(str(path))
    doc.close()


def _fake_docx(path: Path, values: dict[str, str] | None = None) -> None:
    values = values or {}
    body = "".join(
        "<w:p><w:r><w:rPr>"
        '<w:rFonts w:ascii="標楷體" w:eastAsia="標楷體" w:hAnsi="標楷體" w:cs="標楷體"/>'
        f"<w:t>{value}</w:t></w:r></w:p>"
        for value in values.values()
        if value
    )
    xml = f"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">{body}</w:document>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0' encoding='UTF-8'?>")
        zf.writestr("word/document.xml", xml)


def _fake_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4")


def _with_fake_fitz(monkeypatch) -> None:
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5Vx6sAAAAASUVORK5CYII="
    )

    class _FakePixmap:
        def save(self, path: str) -> None:
            Path(path).write_bytes(png_bytes)

    class _FakeRect(types.SimpleNamespace):
        width: float
        height: float

    class _FakePage:
        rect = _FakeRect(width=612, height=792)

        def get_pixmap(self, matrix, alpha=False) -> _FakePixmap:
            return _FakePixmap()

    class _FakePdf:
        def __init__(self, *_args, **_kwargs) -> None:
            self._pages = [_FakePage()]

        def __iter__(self):
            return iter(self._pages)

        def __len__(self) -> int:
            return len(self._pages)

        def close(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            self.close()

    class _FakeMatrix:
        def __init__(self, x: float, y: float) -> None:
            self.x = x
            self.y = y

    module = types.SimpleNamespace(open=lambda *_args, **_kwargs: _FakePdf(), Matrix=_FakeMatrix)
    monkeypatch.setitem(sys.modules, "fitz", module)


def test_laf_poa_quality_validator_accepts_complete_fields(tmp_path):
    from api.laf_poa_docx import validate_laf_poa_docx_quality

    docx_path = tmp_path / "委任狀_1150529-E-005（可填寫版）.docx"
    _fake_docx(
        docx_path,
        {
            "client_name": "王惠薰",
            "case_reason": "更生",
            "lawyer_name": "喬政翔律師",
            "lawyer_address": "花蓮縣花蓮市明禮路18之6號1樓",
            "lawyer_phone": "03-835-7186",
            "court": "臺灣花蓮地方法院",
        },
    )

    result = validate_laf_poa_docx_quality(
        docx_path,
        expected_fields={
            "client_name": "王惠薰",
            "case_reason": "更生",
            "lawyer_name": "喬政翔律師",
            "lawyer_address": "花蓮縣花蓮市明禮路18之6號1樓",
            "lawyer_phone": "03-835-7186",
            "court": "臺灣花蓮地方法院",
        },
    )
    assert result["ok"] is True
    assert result["issues"] == []


def test_laf_poa_quality_validator_flags_missing_lawyer_and_contact(tmp_path):
    from api.laf_poa_docx import validate_laf_poa_docx_quality

    docx_path = tmp_path / "委任狀_1150529-E-005（可填寫版）.docx"
    _fake_docx(
        docx_path,
        {
            "client_name": "王惠薰",
            "case_reason": "更生",
            "court": "臺灣花蓮地方法院",
            "lawyer_name": "受任律師",
            "lawyer_address": "事務所地址",
            "lawyer_phone": "事務所電話",
        },
    )

    result = validate_laf_poa_docx_quality(
        docx_path,
        expected_fields={
            "client_name": "王惠薰",
            "case_reason": "更生",
            "lawyer_name": "受任律師",
            "lawyer_address": "事務所地址",
            "lawyer_phone": "事務所電話",
            "court": "臺灣花蓮地方法院",
        },
    )
    assert result["ok"] is False
    issue_codes = {issue["code"] for issue in result["issues"]}
    assert "missing_field" in issue_codes
    assert "placeholder_leftover" in issue_codes or "unreplaced_placeholder" in issue_codes


def test_laf_poa_curated_templates_are_valid_docx_files():
    from docx import Document  # type: ignore

    from api.laf_poa_docx import TEMPLATE_DIR, TEMPLATE_FILENAMES

    for filename in TEMPLATE_FILENAMES.values():
        path = TEMPLATE_DIR / filename
        Document(str(path))
        xml = _docx_xml(path)
        assert "{{LAF_CASE_NUMBER}}" in xml
        assert "{{CLIENT_NAME}}" in xml
        assert "{{LAWYER_NAME}}" in xml
        assert "{{LAW_FIRM_ADDRESS_LINE}}" in xml
        assert "{{LAW_FIRM_PHONE}}" in xml


def test_laf_poa_pdf_creates_fillable_word_companion(tmp_path):
    from api.laf_poa_docx import (
        ensure_laf_poa_docx_companion,
        laf_poa_docx_path,
        laf_poa_template_docx_path,
    )

    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _make_pdf(pdf)

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "client_name": "王惠薰",
            "client_birthday": "70年1月1日",
            "client_id": "A123456789",
            "client_address_phone": "花蓮市測試路1號；03-0000000",
            "laf_case_number": "1150529-E-005",
            "branch": "花蓮",
            "case_type": "消費者債務清理",
            "case_reason": "更生",
            "court_name": "臺灣花蓮地方法院",
            "court_case_number": "115年度消債更字第005號",
        },
    )
    docx_path = laf_poa_docx_path(pdf)
    template_path = laf_poa_template_docx_path(pdf)

    assert result["ok"] is True
    assert result["status"] == "created"
    assert result["pages"] == 1
    assert result["template_key"] == "general"
    assert result["filled_fields"]["laf_case_number"] == "1150529-E-005"
    assert not template_path.exists(), "exact PDF layout 不應在案件資料夾留下委任狀（範本）"
    assert docx_path.exists()
    xml = _docx_xml(docx_path)
    assert "<wp:anchor" in xml and 'behindDoc="1"' in xml, "預設必須以 PDF 作底圖，讓可填寫版貼近原 PDF"
    assert "magi_laf_poa_overlay_" in xml, "PDF 底圖上必須有可編輯欄位 overlay"
    assert "<w:sdt>" in xml, "可填寫項目必須是 Word content controls，不只是不可追蹤的普通文字"
    assert "magi_laf_poa.client_name" in xml
    assert "magi_laf_poa.case_reason" in xml
    assert "w14:checkbox" in xml, "表單勾選項目必須提供 Word checkbox controls"
    assert "magi_laf_poa.role_defender" in xml
    assert "王惠薰" in xml
    assert "70年1月1日" in xml
    assert "A123456789" in xml
    assert "花蓮市測試路1號" in xml
    assert "1150529-E-005" in xml
    assert "更生" in xml
    assert result["template_values"]["LAWYER_NAME"] in xml
    assert result["template_values"]["LAW_FIRM_ADDRESS_LINE"] in xml
    if result["template_values"].get("LAW_FIRM_PHONE"):
        assert result["template_values"]["LAW_FIRM_PHONE"] in xml
    assert "<w:br/>" in xml
    assert "臺灣花蓮地方法院" in xml
    assert "115" in xml
    assert "消債更" in xml
    assert "005" in xml
    assert "標楷體" in xml
    assert "Times New Roman" not in xml
    assert "{{" not in xml

    second = ensure_laf_poa_docx_companion(pdf)
    assert second["ok"] is True
    assert second["status"] == "exists"


def test_laf_poa_existing_static_companion_is_rebuilt_for_exact_layout(tmp_path):
    import os

    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path, laf_poa_template_docx_path

    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _make_pdf(pdf)
    docx_path = laf_poa_docx_path(pdf)
    template_path = laf_poa_template_docx_path(pdf)
    _fake_docx(docx_path, {"old": "舊版靜態範本"})
    _fake_docx(template_path, {"old": "舊版範本"})
    future = pdf.stat().st_mtime + 60
    os.utime(docx_path, (future, future))
    os.utime(template_path, (future, future))

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={"client_name": "王惠薰", "laf_case_number": "1150529-E-005"},
    )
    xml = _docx_xml(docx_path)

    assert result["ok"] is True
    assert result["status"] == "created"
    assert "stale_non_exact_docx_rebuilt" in result["warnings"]
    assert not template_path.exists(), "重建 exact 可填寫版時也要清掉舊範本檔"
    assert "<wp:anchor" in xml and 'behindDoc="1"' in xml
    assert "magi_laf_poa_overlay_" in xml
    assert "舊版靜態範本" not in xml
    assert "王惠薰" in xml


def test_criminal_poa_pdf_creates_usable_content_controls_without_template(tmp_path):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path, laf_poa_template_docx_path

    pdf = tmp_path / "刑事委任狀_測試.pdf"
    _make_pdf(pdf, "刑事委任狀\n案號：\n股別：\n為委任辯護人事")

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "poa_layout": "criminal",
            "client_name": "林稚芳",
            "client_address_phone": "花蓮市測試路1號",
            "poa_lawyer_name": "喬政翔律師",
            "law_firm_office_name": "偵理法律事務所",
            "law_firm_address_line": "970花蓮縣花蓮市明禮路18-6號1樓",
            "law_firm_phone": "03-8357-186",
            "law_firm_fax": "03-8357-135",
            "case_reason": "過失傷害",
            "court_name": "臺灣花蓮地方檢察署",
            "court_case_number": "115年度偵字第123號",
            "roc_year": "115",
            "roc_month": "6",
            "roc_day": "26",
        },
    )
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert result["status"] == "created"
    assert not laf_poa_template_docx_path(pdf).exists()
    assert "<wp:anchor" in xml and 'behindDoc="1"' in xml
    for field in (
        "criminal_case_number",
        "criminal_client_name",
        "criminal_client_address",
        "criminal_lawyer_name",
        "criminal_law_firm_contact",
        "criminal_case_reason",
        "criminal_court_name",
        "criminal_client_signature_name",
        "criminal_lawyer_signature_name",
        "criminal_roc_year",
    ):
        assert f"magi_laf_poa.{field}" in xml
    assert "<w:sdt>" in xml
    assert "林稚芳" in xml
    assert "喬政翔律師" in xml
    assert "過失傷害" in xml


def test_laf_poa_generation_records_quality_validation_in_result(tmp_path, monkeypatch):
    import api.laf_poa_docx as poa
    from api.laf_poa_docx import ensure_laf_poa_docx_companion

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _fake_pdf(pdf)

    template_path = tmp_path / "fake_template.docx"
    _fake_docx(template_path, {"seed": "模板"})

    def fake_select_laf_poa_template(_metadata):
        return "general", template_path

    def fake_create_from_template(template_key, template_path_arg, template_target, target, metadata):
        values = {
            "LAWYER_NAME": metadata.get("lawyer_name", "喬政翔律師"),
            "LAW_FIRM_ADDRESS_LINE": "花蓮縣花蓮市明禮路18之6號1樓",
            "LAW_FIRM_PHONE": "03-835-7186",
            "CLIENT_NAME": metadata.get("client_name", "王惠薰"),
            "CASE_REASON": metadata.get("case_reason", "更生"),
            "COURT_NAME": metadata.get("court_name", "臺灣花蓮地方法院"),
            "COURT_LINE": metadata.get("court_line", "臺灣花蓮地方法院"),
            "template_key": template_key,
        }
        _fake_docx(template_target, {"seed": "template"})
        _fake_docx(
            target,
            {
                "client_name": values["CLIENT_NAME"],
                "case_reason": values["CASE_REASON"],
                "lawyer_name": values["LAWYER_NAME"],
                "lawyer_address": values["LAW_FIRM_ADDRESS_LINE"],
                "lawyer_phone": values["LAW_FIRM_PHONE"],
                "court": values["COURT_NAME"],
            },
        )
        return values

    monkeypatch.setattr(poa, "select_laf_poa_template", fake_select_laf_poa_template)
    monkeypatch.setattr(poa, "_create_from_laf_poa_template", fake_create_from_template)

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "client_name": "王惠薰",
            "case_reason": "更生",
            "branch": "花蓮",
            "court_name": "臺灣花蓮地方法院",
            "poa_lawyer_name": "喬政翔律師",
        },
    )
    assert result["quality"]["ok"] is True
    assert "quality_validation_failed" not in result["warnings"]


def test_laf_poa_generation_reports_quality_failure_on_missing_lawyer_contact(tmp_path, monkeypatch):
    import api.laf_poa_docx as poa
    from api.laf_poa_docx import ensure_laf_poa_docx_companion

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    monkeypatch.setattr(
        poa,
        "get_law_firm_profile",
        lambda: LawFirmProfile(
            lawyer_name="受任律師",
            office_name="事務所名稱",
            address_line="事務所地址",
            phone="事務所電話",
            fax="",
            mobile="",
        ),
    )
    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _fake_pdf(pdf)
    template_path = tmp_path / "fake_template.docx"
    _fake_docx(template_path, {"seed": "模板"})

    def fake_select_laf_poa_template(_metadata):
        return "general", template_path

    def fake_create_from_template(template_key, template_path_arg, template_target, target, metadata):
        values = {
            "LAWYER_NAME": metadata.get("lawyer_name", ""),
            "LAW_FIRM_ADDRESS_LINE": "事務所地址",
            "LAW_FIRM_PHONE": "事務所電話",
            "CLIENT_NAME": metadata.get("client_name", "王惠薰"),
            "CASE_REASON": metadata.get("case_reason", ""),
            "COURT_NAME": metadata.get("court_name", ""),
            "COURT_LINE": metadata.get("court_name", ""),
            "template_key": template_key,
        }
        _fake_docx(template_target, {"seed": "template"})
        _fake_docx(target, values)
        return values

    monkeypatch.setattr(poa, "select_laf_poa_template", fake_select_laf_poa_template)
    monkeypatch.setattr(poa, "_create_from_laf_poa_template", fake_create_from_template)

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "client_name": "王惠薰",
            "case_reason": "更生",
            "branch": "花蓮",
            "court_name": "臺灣花蓮地方法院",
            "lawyer_name": "受任律師",
        },
    )
    assert result["quality"]["ok"] is False
    assert "quality_validation_failed" in result["warnings"]
    fields = {issue["field"] for issue in result["quality"]["issues"]}
    assert {"lawyer_name", "lawyer_address", "lawyer_phone"} <= fields


def test_laf_poa_ignores_mail_receipt_pdfs(tmp_path):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    receipts = [
        tmp_path / "20260204 1150107-J-003(陳偉傑)東分委任狀掛號郵件收件回執.pdf",
        tmp_path / "臺北刑事_114國審訴1卷1_P1-168_委任狀卷_OCR.pdf",
    ]

    for pdf in receipts:
        _make_pdf(pdf)

        result = ensure_laf_poa_docx_companion(pdf, overwrite=True)

        assert result["ok"] is True
        assert result["status"] == "not_poa_pdf"
        assert not laf_poa_docx_path(pdf).exists()


def test_laf_poa_static_template_can_fill_when_exact_layout_disabled(tmp_path, monkeypatch):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _fake_pdf(pdf)

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "client_name": "王惠薰",
            "laf_case_number": "1150529-E-005",
            "branch": "花蓮",
            "case_type": "消費者債務清理",
            "case_reason": "更生",
        },
    )
    xml = _docx_xml(laf_poa_docx_path(pdf))
    template_values = result["template_values"]

    assert result["ok"] is True
    assert result["template_key"] == "general"
    assert "<wp:anchor" not in xml
    assert "王惠薰" in xml
    assert "1150529-E-005" in xml
    assert "更生" in xml
    assert "花蓮分會" in xml
    assert "待確認" in xml
    assert template_values["LAWYER_NAME"] in xml
    assert template_values["LAW_FIRM_ADDRESS_LINE"] in xml
    assert template_values["LAW_FIRM_PHONE"] in xml
    assert "案號：年度字第號股" in _docx_text(laf_poa_docx_path(pdf))
    assert "請填法院" not in _docx_text(laf_poa_docx_path(pdf))
    assert "請填法院案號" not in _docx_text(laf_poa_docx_path(pdf))
    assert "{{" not in xml


def test_laf_portal_zip_poa_generates_word_companion(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import _move_downloaded_to_case_folder

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    zip_path = download_dir / "1150529-E-005.zip"
    pdf_name = "委任狀_1150529-E-005_1150601.pdf"
    pdf_source = tmp_path / pdf_name
    _make_pdf(pdf_source)

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(pdf_source, pdf_name)

    case_root = tmp_path / "case"
    moved, failed = _move_downloaded_to_case_folder([str(zip_path)], str(case_root))

    archived_pdf = case_root / "01_法扶資料" / pdf_name
    archived_docx = case_root / "01_法扶資料" / "委任狀_1150529-E-005_1150601（可填寫版）.docx"
    assert failed == []
    assert pdf_name in moved
    assert archived_pdf.exists()
    assert archived_docx.exists()


def test_laf_poa_indigenous_template_uses_center_phone(tmp_path, monkeypatch):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    pdf = tmp_path / "委任狀_1150421-W-004_1150529.pdf"
    _make_pdf(pdf)

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "client_name": "李秀英",
            "client_birthday": "60年12月9日",
            "client_id": "U220557046",
            "laf_case_number": "1150421-W-004",
            "branch": "原住民族法律服務中心",
            "case_type": "行政",
            "case_reason": "勞工保險爭議",
            "lawyer_name": "喬政翔",
        },
    )

    xml = _docx_xml(laf_poa_docx_path(pdf))
    assert result["ok"] is True
    assert result["template_key"] == "indigenous_center"
    text = _docx_text(laf_poa_docx_path(pdf))
    assert "受原住民族委員會委託辦理原住民法律扶助專用委任狀" in text
    assert "李秀英" in xml
    assert "1150421-W-004" in xml
    assert "U220557046" in xml
    assert "原住民族法律服務中心" in xml
    assert "03-8509917" in xml
    assert "{{" not in xml


def test_laf_poa_exact_layout_is_used_even_when_static_template_exists(tmp_path, monkeypatch):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "1")
    monkeypatch.setenv("MAGI_LAF_POA_ALLOW_PDF_RENDER_FALLBACK", "1")
    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _make_pdf(pdf, "更生\n法院：臺灣花蓮地方法院")

    result = ensure_laf_poa_docx_companion(pdf, case_metadata={"client_name": "王惠薰"})
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert result["status"] == "created"
    assert result["template_key"] == "general"
    assert "magi_laf_poa_overlay_" in xml, "exact layout 開啟時必須進入 PDF 背景重建模式"
    assert "<wp:anchor" in xml and 'behindDoc="1"' in xml
    assert result["filled_fields"]["client_name"] == "王惠薰"


def test_laf_poa_pdf_background_fallback_requires_explicit_opt_in(tmp_path, monkeypatch):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "1")
    monkeypatch.setenv("MAGI_LAF_POA_DOCX_TEMPLATES", "0")
    monkeypatch.setenv("MAGI_LAF_POA_ALLOW_PDF_RENDER_FALLBACK", "1")
    _with_fake_fitz(monkeypatch)
    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _fake_pdf(pdf)

    result = ensure_laf_poa_docx_companion(pdf, case_metadata={"client_name": "王惠薰"})
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert result["status"] == "created"
    assert result["template_key"] == "general"
    assert "<wp:anchor" in xml and 'behindDoc="1"' in xml, "需明確開啟 fallback 才可使用 PDF 重建版"
    assert result["filled_fields"]["client_name"] == "王惠薰"


def test_laf_poa_pdf_background_fallback_is_opt_in_when_exact_layout_disabled(tmp_path, monkeypatch):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    monkeypatch.setenv("MAGI_LAF_POA_DOCX_TEMPLATES", "0")
    monkeypatch.delenv("MAGI_LAF_POA_ALLOW_PDF_RENDER_FALLBACK", raising=False)
    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _make_pdf(pdf)

    result = ensure_laf_poa_docx_companion(pdf, case_metadata={"client_name": "王惠薰"})

    assert result["ok"] is False
    assert result["status"] == "template_unavailable"
    assert result["error"] == "laf_poa_docx_template_required"
    assert not laf_poa_docx_path(pdf).exists()


def test_laf_poa_pdf_itself_is_authoritative_metadata_source(tmp_path, monkeypatch):
    import api.laf_poa_docx as poa
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    pdf = tmp_path / "委任狀_1150421-W-004_1150529.pdf"
    _make_pdf(pdf)

    def fake_pdf_extract(_path):
        return {
            "laf_case_number": "1150421-W-004",
            "client_name": "李秀英",
            "client_birthday": "60年12月9日",
            "client_id": "U220557046",
            "lawyer_name": "喬政翔律師",
            "branch": "原住民族法律服務中心",
            "branch_phone": "03-8509917",
            "roc_year": "115",
            "roc_month": "5",
            "roc_day": "29",
        }

    monkeypatch.setattr(poa, "_extract_laf_poa_pdf_metadata", fake_pdf_extract)

    result = ensure_laf_poa_docx_companion(pdf)
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert result["template_key"] == "indigenous_center"
    assert result["pdf_extracted_fields"]["client_name"] == "李秀英"
    assert result["pdf_extracted_fields"]["branch_phone"] == "03-8509917"
    assert "1150421-W-004" in xml
    assert "李秀英" in xml
    assert "60年12月9日" in xml
    assert "U220557046" in xml
    assert "原住民族法律服務中心" in xml
    assert "03-8509917" in xml
    assert "115 年 5 月 29 日" in _docx_text(laf_poa_docx_path(pdf))


def test_laf_poa_reason_falls_back_to_case_folder(tmp_path, monkeypatch):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    case_dir = tmp_path / "法扶案件" / "消費者債務清理" / "2026-0058-王惠薰-消費者債務清理-更生" / "01_法扶資料"
    case_dir.mkdir(parents=True)
    pdf = case_dir / "委任狀_1150529-E-005_1150601.pdf"
    _make_pdf(
        pdf,
        "財團法人法律扶助基金會專用委任狀\n"
        "本會申請編號：1150529-E-005\n"
        "姓名\n王惠薰\n"
        "本事件經本會 花蓮分會 審核准予扶助，請逕致電分會(03-8222128)。",
    )

    result = ensure_laf_poa_docx_companion(pdf)
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert result["filled_fields"]["case_type"] == "消費者債務清理"
    assert result["filled_fields"]["case_reason"] == "更生"
    assert "更生" in xml
    assert "王惠薰" in xml
    assert "1150529-E-005" in xml


def test_laf_poa_laf_number_falls_back_to_filename(tmp_path, monkeypatch):
    import api.laf_poa_docx as poa
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    pdf = tmp_path / "委任狀_1150521-A-044_1150525.pdf"
    _make_pdf(pdf)

    monkeypatch.setattr(
        poa,
        "_extract_laf_poa_pdf_metadata",
        lambda path: {"client_name": "游秀鈴", "branch": "台北分會", "branch_phone": "02-23225151"},
    )

    result = ensure_laf_poa_docx_companion(pdf)
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert result["filled_fields"]["laf_case_number"] == "1150521-A-044"
    assert "1150521-A-044" in xml


def test_laf_poa_lawyer_is_not_overridden_by_assigned_lawyer(tmp_path, monkeypatch):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_EXACT_PDF_LAYOUT", "0")
    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _make_pdf(pdf)

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "client_name": "王惠薰",
            "laf_case_number": "1150529-E-005",
            "branch": "花蓮",
            "case_type": "消費者債務清理",
            "case_reason": "更生",
            "lawyer_name": "林稚芳律師",
            "assigned_lawyer": "林稚芳",
        },
    )
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert "喬政翔律師" in xml
    assert "林稚芳" not in xml


def test_laf_poa_roc_date_uses_file_tail_not_laf_number():
    from api.laf_poa_docx import _roc_date_from_filename

    assert _roc_date_from_filename(Path("委任狀_1150421-W-004_1150529.pdf")) == {
        "roc_year": "115",
        "roc_month": "5",
        "roc_day": "29",
    }
