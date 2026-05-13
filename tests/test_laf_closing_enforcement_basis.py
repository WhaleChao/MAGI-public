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
