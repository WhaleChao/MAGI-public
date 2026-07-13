import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORCH_PATH = ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py"


spec = importlib.util.spec_from_file_location("laf_orchestrator_for_archive_fallback_test", ORCH_PATH)
laf_orchestrator = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(laf_orchestrator)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def fetch_all(self, *args, **kwargs):
        return list(self.rows)

    def fetch_one(self, *args, **kwargs):
        return None

    def execute_write(self, *args, **kwargs):
        return None


def test_closing_draft_uses_archived_case_folder_when_db_folder_is_stale(tmp_path, monkeypatch):
    active_root = tmp_path / "active" / "01_案件"
    empty_closed_root = tmp_path / "empty_closed" / "03_工作資料" / "10_結案"
    closed_root = tmp_path / "closed" / "03_工作資料" / "10_結案"
    (active_root / "法扶案件" / "消費者債務清理").mkdir(parents=True)
    (empty_closed_root / "法扶案件" / "消費者債務清理").mkdir(parents=True)

    archived_case = closed_root / "法扶案件" / "消費者債務清理" / "2025-0084-王台銘-消費者債務清理-清算"
    judgment_dir = archived_case / "10_判決書或終局裁定及處分"
    laf_dir = archived_case / "01_法扶資料"
    judgment_dir.mkdir(parents=True)
    laf_dir.mkdir(parents=True)
    basis = judgment_dir / "20260703 臺北地方法院115年度消債職聲免字第26號民事裁定（王台銘；主文：債務人王台銘不免責）.pdf"
    basis.write_bytes(b"%PDF-1.4\n")
    (laf_dir / "准予扶助證明書_1130919-T-057_1130923.pdf").write_bytes(b"%PDF-1.4\n")

    stale_folder = active_root / "法扶案件" / "消費者債務清理" / "2025-0084-王台銘-消費者債務清理-清算"
    monkeypatch.setattr(
        laf_orchestrator,
        "preferred_case_roots",
        lambda include_closed=False: [str(active_root)] + ([str(empty_closed_root)] if include_closed else []),
    )
    monkeypatch.setattr(
        laf_orchestrator,
        "default_case_roots",
        lambda include_closed=False: [str(active_root)] + ([str(empty_closed_root), str(closed_root)] if include_closed else []),
    )

    orch = laf_orchestrator.LAFOrchestrator(dry_run=True)
    orch._db = FakeDB([
        {
            "case_number": "2025-0084",
            "client_name": "王台銘",
            "legal_aid_number": "1130919-T-057",
            "folder_path": str(stale_folder),
            "legal_aid_status": "待報結",
        }
    ])
    orch._gather_case_counts = lambda *args, **kwargs: {
        "meeting_count": 1,
        "contact_count": 0,
        "inq_count": 0,
        "court_count": 1,
        "review_count": 0,
        "document_count": 1,
        "court_dates": [],
        "review_dates": [],
    }

    result = orch.execute_portal_action_draft(
        action="closing",
        laf_case_number="1130919-T-057",
        client_name="王台銘",
        suppress_notify=True,
    )

    assert result["ok"] is True
    assert result["identity"]["case_folder"] == str(archived_case)
    assert result["basis_files"] == [str(basis)]


def test_closing_draft_reports_terminal_ruling_misfiled_in_notice_folder(tmp_path, monkeypatch):
    case_dir = tmp_path / "2025-0068-劉亞箖-消費者債務清理-更生"
    notice_dir = case_dir / "09_法院通知或程序裁定"
    notice_dir.mkdir(parents=True)
    misfiled = notice_dir / "20260617 宜蘭地方法院114年度消債更字第83號民事裁定（劉亞箖；主文：更生之聲請駁回、聲請程序費用由聲請人負擔）.pdf"
    misfiled.write_bytes(b"%PDF-1.4\n")
    root = tmp_path / "active" / "01_案件"
    root.mkdir(parents=True)
    monkeypatch.setattr(laf_orchestrator, "preferred_case_roots", lambda include_closed=False: [str(root)])
    monkeypatch.setattr(laf_orchestrator, "default_case_roots", lambda include_closed=False: [str(root)])

    orch = laf_orchestrator.LAFOrchestrator(dry_run=True)
    orch._db = FakeDB([
        {
            "case_number": "2025-0068",
            "client_name": "劉亞箖",
            "legal_aid_number": "1140723-I-005",
            "folder_path": str(case_dir),
            "legal_aid_status": "待報結",
        }
    ])

    result = orch.execute_portal_action_draft(
        action="closing",
        laf_case_number="1140723-I-005",
        client_name="劉亞箖",
        suppress_notify=True,
    )

    assert result["ok"] is False
    assert result["error"] == "missing_required_docs"
    assert str(misfiled) in result["misfiled_closing_basis_files"]
    assert "10_判決書或終局裁定及處分" in result["hint"]
