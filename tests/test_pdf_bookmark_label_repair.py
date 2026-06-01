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
    assert any(issue["kind"] == "context_reference_label" for issue in item["issues"])


def test_audit_flags_judgment_body_reference_to_indictment(tmp_path: Path):
    mod = load_repair_module()
    pdf = tmp_path / "10_判決書" / "判決.pdf"
    pdf.parent.mkdir()
    make_text_pdf(
        pdf,
        [
            "理 由\n本院審酌檢察官起訴書所載犯罪事實，並參酌卷附鑑定報告及證人筆錄。",
        ],
    )
    set_toc(pdf, [[1, "起訴書", 1]])

    bookmarker = mod._load_bookmarker()
    item = mod.audit_pdf(pdf, bookmarker)

    assert item["needs_repair"] is True
    issue = item["issues"][0]
    assert issue["kind"] in {"context_reference_label", "unproven_document_label"}
    assert "body_reference_context" in issue.get("context_evidence", [])


def test_audit_flags_pleading_attachment_list_as_polluted_report(tmp_path: Path):
    mod = load_repair_module()
    pdf = tmp_path / "07_對方歷次書狀" / "聲請狀.pdf"
    pdf.parent.mkdir()
    make_text_pdf(
        pdf,
        [
            "刑事聲請狀\n附件清單\n一、法醫研究所解剖報告暨鑑定報告\n二、相驗屍體證明書",
        ],
    )
    set_toc(pdf, [[1, "法醫報告", 1]])

    bookmarker = mod._load_bookmarker()
    item = mod.audit_pdf(pdf, bookmarker)

    assert item["needs_repair"] is True
    assert item["issues"][0]["kind"] == "label_page_mismatch"


def test_audit_keeps_actual_report_cover(tmp_path: Path):
    mod = load_repair_module()
    pdf = tmp_path / "07_證據資料" / "法醫報告.pdf"
    pdf.parent.mkdir()
    make_text_pdf(
        pdf,
        [
            "法務部法醫研究所解剖報告書暨鑑定報告書\n受鑑定人 劉信義\n鑑定日期 中華民國114年6月5日",
        ],
    )
    set_toc(pdf, [[1, "鑑定報告", 1]])

    bookmarker = mod._load_bookmarker()
    item = mod.audit_pdf(pdf, bookmarker)

    assert item["needs_repair"] is False


def test_audit_skips_encrypted_pdf_without_error(tmp_path: Path):
    mod = load_repair_module()
    pdf = tmp_path / "06_閱卷資料" / "encrypted.pdf"
    pdf.parent.mkdir()
    make_text_pdf(pdf, ["加密卷宗"])
    encrypted = pdf.with_suffix(".encrypted.pdf")
    doc = fitz.open(pdf)
    try:
        doc.save(
            encrypted,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner",
            user_pw="user",
        )
    finally:
        doc.close()
    encrypted.replace(pdf)

    bookmarker = mod._load_bookmarker()
    item = mod.audit_pdf(pdf, bookmarker)

    assert item["classification"] == "encrypted_pdf_skipped"
    assert item["skipped"] is True
    assert item["needs_repair"] is False


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


def test_candidate_scan_can_focus_on_large_volume_pdfs(tmp_path: Path):
    mod = load_repair_module()
    target = tmp_path / "法扶案件" / "刑事" / "2026-0028-劉信義-一審-殺人" / "06_閱卷資料"
    target.mkdir(parents=True)

    small_pdf = target / "小型通知.pdf"
    large_pdf = target / "大型卷宗.pdf"
    make_text_pdf(small_pdf, ["法院通知"])
    make_text_pdf(large_pdf, ["卷宗"])
    with large_pdf.open("ab") as fh:
        fh.write(b"0" * (1024 * 1024 + 16))

    candidates, meta = mod.iter_pdf_candidates(
        [str(tmp_path)],
        min_file_mb=1,
        max_file_mb=2,
        max_dirs=50,
        max_files=5,
        max_seconds=30,
    )

    assert candidates == [large_pdf]
    assert meta["skipped_small_files"] == 1
    assert meta["skipped_large_files"] == 0
