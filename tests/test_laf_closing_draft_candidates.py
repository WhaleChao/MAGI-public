import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORCH_PATH = ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py"


spec = importlib.util.spec_from_file_location("laf_orchestrator_for_closing_candidates_test", ORCH_PATH)
laf_orchestrator = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(laf_orchestrator)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def fetch_all(self, *args, **kwargs):
        return list(self.rows)


def _orchestrator_with_rows(rows):
    orch = laf_orchestrator.LAFOrchestrator(dry_run=True)
    orch._db = FakeDB(rows)
    orch._was_closing_drafted_recently = lambda *args, **kwargs: False
    return orch


def test_auto_closing_skips_misfiled_consumer_debt_transfer_ruling(tmp_path):
    case_dir = tmp_path / "2025-0087-張慧敏-消費者債務清理-更生"
    judgment_dir = case_dir / "10_判決書"
    judgment_dir.mkdir(parents=True)
    transfer = judgment_dir / "20251124 基隆地方法院114年度司消債調字第207號民事裁定（張慧敏；主文：本件移送臺北地方法院）.pdf"
    transfer.write_bytes(b"%PDF-1.4\n")
    orch = _orchestrator_with_rows([
        {
            "case_number": "2025-0087",
            "client_name": "張慧敏",
            "legal_aid_number": "1140911-K-001",
            "folder_path": str(case_dir),
            "case_reason": "消費者債務清理",
        }
    ])

    assert orch._get_pending_closing_draft_cases(max_cases=10) == []


def test_auto_closing_accepts_terminal_consumer_debt_ruling(tmp_path):
    case_dir = tmp_path / "2025-0088-王小明-消費者債務清理-清算"
    judgment_dir = case_dir / "10_判決書"
    judgment_dir.mkdir(parents=True)
    terminal = judgment_dir / "20260601 臺灣花蓮地方法院114年度消債清字第12號免責裁定（王小明）.pdf"
    terminal.write_bytes(b"%PDF-1.4\n")
    orch = _orchestrator_with_rows([
        {
            "case_number": "2025-0088",
            "client_name": "王小明",
            "legal_aid_number": "1140101-W-001",
            "folder_path": str(case_dir),
            "case_reason": "消費者債務清理",
        }
    ])

    candidates = orch._get_pending_closing_draft_cases(max_cases=10)

    assert len(candidates) == 1
    assert candidates[0]["laf_case_number"] == "1140101-W-001"
    assert candidates[0]["closing_basis_files"] == [str(terminal)]
