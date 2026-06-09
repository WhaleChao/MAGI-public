import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCMIXIN_PATH = ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator_docmixins.py"


spec = importlib.util.spec_from_file_location("laf_orchestrator_docmixins_for_test", DOCMIXIN_PATH)
docmixins = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(docmixins)


def test_enforcement_order_counts_as_closing_basis(tmp_path):
    case_dir = tmp_path / "2026-0004-測試當事人-執行-強制執行"
    judgment_dir = case_dir / "10_判決書"
    judgment_dir.mkdir(parents=True)
    basis = judgment_dir / "20260512 花蓮地方法院115年度司執字第000001號執行命令（測試當事人；檢附債權憑證）.pdf"
    basis.write_bytes(b"%PDF-1.4\n")

    scanner = docmixins.LAFOrchestratorDocumentMixin()
    docs = scanner._scan_case_folder_docs(str(case_dir))

    assert str(basis) in docs["closing_basis_files"]
    meta = scanner._infer_closing_metadata_from_docs(docs["closing_basis_files"], client_name="測試當事人", folder_path=str(case_dir))
    assert meta["closing_doc_type"] == "執行命令"


def test_random_execution_notice_is_not_closing_basis(tmp_path):
    case_dir = tmp_path / "2026-0005-測試-執行-強制執行"
    notice_dir = case_dir / "09_法院通知或程序裁定"
    notice_dir.mkdir(parents=True)
    notice = notice_dir / "20260512 花蓮地方法院115年度司執字第1088號執行命令.pdf"
    notice.write_bytes(b"%PDF-1.4\n")

    scanner = docmixins.LAFOrchestratorDocumentMixin()
    docs = scanner._scan_case_folder_docs(str(case_dir))

    assert str(notice) not in docs["closing_basis_files"]


def test_closing_scan_skips_review_folder_for_large_cases(tmp_path):
    case_dir = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    review_dir = case_dir / "06_閱卷資料" / "20260520"
    judgment_dir = case_dir / "10_判決書"
    review_dir.mkdir(parents=True)
    judgment_dir.mkdir(parents=True)
    noisy_review_file = review_dir / "114年度國審強處字第000001號裁定.pdf"
    closing_basis = judgment_dir / "20260520 臺灣臺北地方法院114年度國審強處字第000001號裁定.pdf"
    noisy_review_file.write_bytes(b"%PDF-1.4\n")
    closing_basis.write_bytes(b"%PDF-1.4\n")

    scanner = docmixins.LAFOrchestratorDocumentMixin()
    docs = scanner._scan_case_folder_docs(str(case_dir), action="closing")

    assert str(closing_basis) in docs["closing_basis_files"]
    assert str(noisy_review_file) not in docs["closing_basis_files"]


def test_closing_metadata_extracts_short_district_court_name(tmp_path):
    case_dir = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    judgment_dir = case_dir / "10_判決書"
    judgment_dir.mkdir(parents=True)
    basis = judgment_dir / "20260518 臺北地方法院114年度訴字第972號刑事判決(游秀鈴).pdf"
    basis.write_bytes(b"%PDF-1.4\n")

    scanner = docmixins.LAFOrchestratorDocumentMixin()
    meta = scanner._infer_closing_metadata_from_docs([str(basis)], client_name="游秀鈴", folder_path=str(case_dir))

    assert meta["court_kind"] == "法院"
    assert meta["court_name"] == "臺北地方法院"
    assert meta["court_case_year"] == "114"
    assert meta["court_case_code"] == "訴"
    assert meta["court_case_no"] == "972"


def test_consumer_debt_transfer_ruling_does_not_trigger_closing_basis(tmp_path):
    case_dir = tmp_path / "2025-0087-張慧敏-消費者債務清理-更生"
    judgment_dir = case_dir / "10_判決書"
    judgment_dir.mkdir(parents=True)
    transfer = judgment_dir / "20251124 基隆地方法院114年度司消債調字第207號民事裁定（張慧敏；主文：本件移送臺北地方法院）.pdf"
    transfer.write_bytes(b"%PDF-1.4\n")

    scanner = docmixins.LAFOrchestratorDocumentMixin()
    docs = scanner._scan_case_folder_docs(str(case_dir), action="closing")

    assert str(transfer) not in docs["closing_basis_files"]


def test_consumer_debt_procedure_end_is_not_auto_closing_basis(tmp_path):
    case_dir = tmp_path / "2025-0045-郭麗卿-消費者債務清理-清算"
    judgment_dir = case_dir / "10_判決書"
    judgment_dir.mkdir(parents=True)
    intermediate = judgment_dir / "20260601 臺灣花蓮地方法院113年度消債清字第1號裁定（主文：本件清算程序終結）.pdf"
    intermediate.write_bytes(b"%PDF-1.4\n")

    scanner = docmixins.LAFOrchestratorDocumentMixin()
    docs = scanner._scan_case_folder_docs(str(case_dir), action="closing")

    assert str(intermediate) not in docs["closing_basis_files"]
    assert not scanner._is_auto_closing_basis_candidate(
        str(intermediate),
        case_reason="消費者債務清理",
        folder_path=str(case_dir),
    )


def test_pleading_docx_with_ruling_words_is_not_closing_basis(tmp_path):
    case_dir = tmp_path / "2025-0003-蕭仁俊-非常上訴-強盜殺人"
    pleading_dir = case_dir / "04_我方歷次書狀"
    pleading_dir.mkdir(parents=True)
    pleading = pleading_dir / "10蕭仁俊_補充判決暨暫時處分裁定聲請二狀.docx"
    pleading.write_bytes(b"fake-docx")

    scanner = docmixins.LAFOrchestratorDocumentMixin()
    docs = scanner._scan_case_folder_docs(str(case_dir), action="closing")

    assert str(pleading) not in docs["closing_basis_files"]
    assert not scanner._is_auto_closing_basis_candidate(
        str(pleading),
        case_reason="刑事",
        folder_path=str(case_dir),
    )


def test_ruling_that_mentions_judgment_request_is_not_auto_closing_basis(tmp_path):
    case_dir = tmp_path / "2025-0003-蕭仁俊-非常上訴-強盜殺人"
    judgment_dir = case_dir / "10_判決書"
    judgment_dir.mkdir(parents=True)
    ruling = judgment_dir / "20260317 憲法法庭115年審裁字第578號裁定(蕭仁俊；主文：一、補充判決之聲請不受理；二、暫時處分之聲請駁回).pdf"
    ruling.write_bytes(b"%PDF-1.4\n")

    scanner = docmixins.LAFOrchestratorDocumentMixin()

    assert not scanner._is_auto_closing_basis_candidate(
        str(ruling),
        case_reason="刑事",
        folder_path=str(case_dir),
    )


def test_consumer_debt_terminal_rulings_are_closing_basis(tmp_path):
    case_dir = tmp_path / "2025-0088-王小明-消費者債務清理-清算"
    judgment_dir = case_dir / "10_判決書"
    judgment_dir.mkdir(parents=True)
    terminal = judgment_dir / "20260601 臺灣花蓮地方法院114年度消債清字第12號免責裁定（王小明）.pdf"
    terminal.write_bytes(b"%PDF-1.4\n")

    scanner = docmixins.LAFOrchestratorDocumentMixin()
    docs = scanner._scan_case_folder_docs(str(case_dir), action="closing")

    assert str(terminal) in docs["closing_basis_files"]
