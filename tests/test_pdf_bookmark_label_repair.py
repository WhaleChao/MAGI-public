from __future__ import annotations

import importlib.util
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "ops" / "repair_pdf_bookmark_labels.py"


def load_repair_module():
    spec = importlib.util.spec_from_file_location("repair_pdf_bookmark_labels_for_test", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def make_text_pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12, fontname="china-t")
    doc.save(path)
    doc.close()


def set_toc(path: Path, toc: list[list]) -> None:
    doc = fitz.open(path)
    try:
        doc.set_toc(toc)
        tmp = path.with_suffix(".tmp.pdf")
        doc.save(tmp, garbage=4, deflate=True)
    finally:
        doc.close()
    tmp.replace(path)


def test_audit_flags_transcript_internal_evidence_reference(tmp_path: Path):
    mod = load_repair_module()
    pdf = tmp_path / "08_筆錄" / "20260513 準備程序筆錄.pdf"
    pdf.parent.mkdir()
    make_text_pdf(
        pdf,
        [
            "準 備 程 序 筆 錄\n被 告 劉信義\n上列被告因案件行準備程序",
            "審判長問：關於鑑定報告書是否請求調查？\n被告答：沒有意見。",
        ],
    )
    set_toc(pdf, [[1, "準備程序筆錄 劉信義", 1], [1, "鑑定報告", 2]])

    bookmarker = mod._load_bookmarker()
    item = mod.audit_pdf(pdf, bookmarker)

    assert item["needs_repair"] is True
    assert item["repairable"] is True
    assert item["issues"][0]["kind"] == "standalone_transcript_multi_bookmark"


def test_audit_flags_evidence_table_reference_as_polluted_label(tmp_path: Path):
    mod = load_repair_module()
    pdf = tmp_path / "06_閱卷資料" / "卷宗.pdf"
    pdf.parent.mkdir()
    make_text_pdf(
        pdf,
        [
            "臺灣花蓮地方法院刑事卷宗\n被告 劉信義",
            "檢證編號 證據名稱 待證事實\n43 解剖報告書暨鑑定報告書\n44 相驗屍體證明書",
        ],
    )
    set_toc(pdf, [[1, "卷宗封面", 1], [1, "鑑定報告", 2]])

    bookmarker = mod._load_bookmarker()
    item = mod.audit_pdf(pdf, bookmarker)

    assert item["needs_repair"] is True
    assert any(issue["kind"] == "embedded_reference_label" for issue in item["issues"])


def test_repair_rebuilds_polluted_transcript_toc(tmp_path: Path):
    mod = load_repair_module()
    pdf = tmp_path / "08_筆錄" / "20260513 準備程序筆錄.pdf"
    pdf.parent.mkdir()
    make_text_pdf(
        pdf,
        [
            "準 備 程 序 筆 錄\n被 告 劉信義\n上列被告因案件行準備程序",
            "審判長問：關於鑑定報告書是否請求調查？\n被告答：沒有意見。",
        ],
    )
    set_toc(pdf, [[1, "準備程序筆錄 劉信義", 1], [1, "鑑定報告", 2]])

    bookmarker = mod._load_bookmarker()
    result = mod.repair_pdf(pdf, bookmarker)

    assert result["success"] is True
    doc = fitz.open(pdf)
    try:
        toc = doc.get_toc()
    finally:
        doc.close()
    assert len(toc) == 1
    assert "準備程序筆錄" in toc[0][1]


def test_case_number_scan_discovers_case_before_deep_document_walk(tmp_path: Path):
    mod = load_repair_module()
    # A deep unrelated folder should not consume the scan before the matching
    # case folder is found.
    unrelated = tmp_path / "一般案件" / "刑事" / "2026-0001-測試" / "07_證據資料"
    unrelated.mkdir(parents=True)
    for idx in range(20):
        (unrelated / f"dummy-{idx}.txt").write_text("x", encoding="utf-8")

    target = tmp_path / "法扶案件" / "刑事" / "2026-0028-劉信義-一審-殺人" / "08_筆錄"
    target.mkdir(parents=True)
    pdf = target / "20260513 準備程序筆錄.pdf"
    make_text_pdf(pdf, ["準 備 程 序 筆 錄\n被 告 劉信義"])

    candidates, meta = mod.iter_pdf_candidates(
        [str(tmp_path)],
        case_number="2026-0028",
        max_dirs=50,
        max_files=5,
        max_seconds=30,
    )

    assert candidates == [pdf]
    assert meta["case_discovery"]["case_root_count"] == 1
