# -*- coding: utf-8 -*-
import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path

import fitz

from api.osc.document_reuse import index_pleading_docx
from api.osc.case_folder_schema import JUDGMENT_FOLDER_LABEL, path_has_judgment_folder
from scripts.ops import document_golden_regression as golden


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tests" / "golden" / "document_regression_manifest.json"
REQUIRED_CATEGORIES = {
    "payment_ocr_naming",
    "transcript_naming",
    "judgment_or_final_ruling",
    "pleading_template_reuse",
}
CJK_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/System/Library/Fonts/CJKSymbolsFallback.ttc"),
)


def _load_pdf_namer():
    module_path = ROOT / "skills" / "pdf-namer" / "action.py"
    spec = importlib.util.spec_from_file_location("pdf_namer_document_golden", module_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _write_cjk_pdf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    font_path = next((candidate for candidate in CJK_FONT_CANDIDATES if candidate.exists()), None)
    fontname = "helv"
    if font_path is not None:
        fontname = "MAGICJK"
        page.insert_font(fontfile=str(font_path), fontname=fontname)
    page.insert_text((72, 72), text, fontsize=12, fontname=fontname)
    doc.save(path)
    doc.close()


@contextmanager
def _deterministic_pdf_namer(monkeypatch):
    pdf_namer = _load_pdf_namer()
    monkeypatch.setenv("MAGI_PDF_AI_NAME_ENABLED", "0")
    monkeypatch.setattr(pdf_namer, "HAS_OCR", False)
    monkeypatch.setattr(pdf_namer, "_macos_vision_ocr_page", lambda *args, **kwargs: "")
    monkeypatch.setattr(pdf_namer, "_vision_analyze_for_naming", lambda *args, **kwargs: {})
    monkeypatch.setattr(pdf_namer, "_prefer_opendataloader_if_better", lambda current_text, *args, **kwargs: current_text)
    yield pdf_namer


def test_document_golden_manifest_covers_required_case_types():
    manifest = golden.load_manifest(MANIFEST)

    assert golden.validate_manifest(manifest) == []
    categories = {case["category"] for case in manifest["cases"]}
    assert REQUIRED_CATEGORIES <= categories
    assert all(case["status"] == "automated" for case in manifest["cases"])


def test_document_golden_runner_can_select_automated_cases_without_running_pytest():
    report = golden.run_manifest(
        MANIFEST,
        categories={"pleading_template_reuse"},
        automated_only=True,
        dry_run=True,
    )

    assert report["ok"] is True
    assert report["selected"] == 1
    assert report["automated"] == 1
    assert report["results"][0]["id"] == "osc_own_pleading_word_excludes_poa"
    assert report["results"][0]["nodeid"] == (
        "tests/test_document_golden_regression.py::"
        "test_golden_pleading_index_only_indexes_own_pleading_word"
    )


def test_golden_payment_notice_pdf_naming_extracts_fee_deadline(tmp_path, monkeypatch):
    with _deterministic_pdf_namer(monkeypatch) as pdf_namer:
        pdf_path = (
            tmp_path
            / "2026-0502-喬翔-一審-消債"
            / "09_法院通知或程序裁定"
            / "繳費單查詢清單.pdf"
        )
        _write_cjk_pdf(
            pdf_path,
            "\n".join(
                [
                    "臺灣花蓮地方法院花蓮簡易庭通知書",
                    "中華民國115年6月29日",
                    "案號：114年度花補字第502號",
                    "當事人 喬翔",
                    "主旨：應於文到10日內繳納裁判費新臺幣100元。",
                    "銷帳編號：0031001561821271",
                    "繳費期限：1150701",
                    "本件係案件繳費狀況查詢清單轉成法院通知。",
                ]
            ),
        )

        result = pdf_namer.generate_name_proposal(str(pdf_path), return_structured=True)

    assert result["doc_type"] == "法院_通知"
    assert result["deadline"] == 10
    assert result["deadline_type"] == "繳費"
    assert "114年度花補字第502號" in result["filename"]
    assert "喬翔" in result["filename"]
    assert "10日內繳納" in result["filename"]
    assert "繳納裁判" not in result["filename"]
    assert "文件" not in result["filename"]
    assert "收據" not in result["filename"]


def test_golden_transcript_pdf_naming_keeps_transcript_kind(tmp_path, monkeypatch):
    with _deterministic_pdf_namer(monkeypatch) as pdf_namer:
        pdf_path = tmp_path / "2026-0503-王大明-一審-詐欺" / "08_筆錄" / "raw.pdf"
        _write_cjk_pdf(
            pdf_path,
            "\n".join(
                [
                    "臺灣花蓮地方法院準備程序筆錄",
                    "中華民國115年6月18日",
                    "案號：115年度訴字第21號",
                    "被告 王大明",
                    "準備程序筆錄",
                    "本日訊問事項及陳述如下。",
                ]
            ),
        )

        result = pdf_namer.generate_name_proposal(str(pdf_path), return_structured=True)

    assert result["doc_type"] == "準備程序筆錄"
    assert result["party"] == "王大明"
    assert result["date"] == "20260618"
    assert "準備程序筆錄" in result["filename"]
    assert "文件" not in result["filename"]


def test_golden_judgment_final_ruling_folder_pdf_naming(tmp_path, monkeypatch):
    with _deterministic_pdf_namer(monkeypatch) as pdf_namer:
        folder = tmp_path / "2026-0504-林小美-一審-確認債權" / f"10_{JUDGMENT_FOLDER_LABEL}"
        pdf_path = folder / "raw.pdf"
        _write_cjk_pdf(
            pdf_path,
            "\n".join(
                [
                    "臺灣花蓮地方法院民事裁定",
                    "中華民國115年5月1日",
                    "案號：115年度訴字第123號",
                    "聲請人 林小美",
                    "主文",
                    "聲請人之更生程序終結。",
                    "理由",
                    "略。",
                ]
            ),
        )

        result = pdf_namer.generate_name_proposal(str(pdf_path), return_structured=True)

    assert path_has_judgment_folder(str(pdf_path))
    assert result["doc_type"] == "裁定"
    assert result["party"] == "林小美"
    assert result["case_number"] == "115年度訴字第123號"
    assert "臺灣花蓮地方法院115年度訴字第123號裁定" in result["filename"]
    assert "文件" not in result["filename"]


def test_golden_pleading_index_only_indexes_own_pleading_word(tmp_path):
    case_root = tmp_path / "case"
    pleading_dir = case_root / "04_我方歷次書狀"
    poa_dir = case_root / "01_委任狀"
    opponent_dir = case_root / "03_對造歷次書狀"
    pleading_dir.mkdir(parents=True)
    poa_dir.mkdir(parents=True)
    opponent_dir.mkdir(parents=True)

    (pleading_dir / "民事準備書狀.docx").write_bytes(b"PK\x03\x04")
    (pleading_dir / "舊案民事聲請狀.doc").write_bytes(b"legacy word")
    (pleading_dir / "民事準備書狀.pdf").write_bytes(b"%PDF-1.4\n")
    (pleading_dir / "~$暫存民事準備書狀.docx").write_bytes(b"temp")
    (pleading_dir / "附件明細.docx").write_bytes(b"not a pleading")
    (poa_dir / "委任狀（可填寫版）.docx").write_bytes(b"PK\x03\x04")
    (poa_dir / "民事準備書狀委任狀.docx").write_bytes(b"PK\x03\x04")
    (opponent_dir / "對造民事準備書狀.docx").write_bytes(b"PK\x03\x04")

    indexed = index_pleading_docx(case_root)
    names = {item["file_name"] for item in indexed}

    assert names == {"民事準備書狀.docx", "舊案民事聲請狀.doc", "附件明細.docx"}
    assert all("我方歷次書狀" in item["path"] for item in indexed)
