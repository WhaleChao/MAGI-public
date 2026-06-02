import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASE_NO_SYNC_PATH = ROOT / "api" / "osc" / "case_no_sync.py"

spec = importlib.util.spec_from_file_location("case_no_sync_for_test", CASE_NO_SYNC_PATH)
case_no_sync = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(case_no_sync)


def test_sync_case_no_prefers_judgment_over_later_procedural_notice(monkeypatch):
    monkeypatch.setattr(case_no_sync, "extract_division_from_notice", lambda _path: ("", "none"))

    record = {
        "id": 1,
        "client_name": "游秀鈴",
        "case_type": "刑事",
        "current_court_case_no": "114年度國審強處字第1號",
        "current_institution": "臺灣臺北地方法院",
    }
    notices = [
        {
            "path": "/case/09_法院通知或程序裁定/20260711 臺北地方法院114年度國審強處字第1號國民法官庭通知書(游秀鈴).pdf",
            "filename": "20260711 臺北地方法院114年度國審強處字第1號國民法官庭通知書(游秀鈴).pdf",
            "mtime": 200,
        },
        {
            "path": "/case/10_判決書/20260518 臺北地方法院114年度訴字第972號刑事判決(游秀鈴).pdf",
            "filename": "20260518 臺北地方法院114年度訴字第972號刑事判決(游秀鈴).pdf",
            "mtime": 100,
        },
    ]

    out = case_no_sync.sync_case_no_from_notices(record, notices, dry_run=True)

    assert out["new_case_no"] == "114年度訴字第972號"
    assert out["source_pdf"].endswith("114年度訴字第972號刑事判決(游秀鈴).pdf")


def test_sync_case_no_still_prefers_later_same_quality_notice(monkeypatch):
    monkeypatch.setattr(case_no_sync, "extract_division_from_notice", lambda _path: ("", "none"))

    record = {
        "id": 2,
        "client_name": "黃淨雅",
        "case_type": None,
        "current_court_case_no": "114年度北司小調字第2918號",
        "current_institution": "臺灣臺北地方法院",
    }
    notices = [
        {
            "path": "/case/09_法院通知或程序裁定/20251209 臺北地方法院114年度北司小調字第2918號黃淨雅通知書.pdf",
            "filename": "20251209 臺北地方法院114年度北司小調字第2918號黃淨雅通知書.pdf",
            "mtime": 200,
        },
        {
            "path": "/case/09_法院通知或程序裁定/20260414 臺北地方法院115年度北小字第1213號黃淨雅通知書.pdf",
            "filename": "20260414 臺北地方法院115年度北小字第1213號黃淨雅通知書.pdf",
            "mtime": 100,
        },
    ]

    out = case_no_sync.sync_case_no_from_notices(record, notices, dry_run=True)

    assert out["new_case_no"] == "115年度北小字第1213號"
