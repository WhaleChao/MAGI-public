import os
from pathlib import Path

from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager


class FakeElement:
    def __init__(self, text="", attrs=None, children=None):
        self.text = text
        self._attrs = attrs or {}
        self._children = children or []

    def get_attribute(self, name):
        return self._attrs.get(name, "")

    def find_elements(self, *args, **kwargs):
        return self._children


def _manager(tmp_path):
    return FileReviewManager(
        username="",
        password="",
        download_folder=str(tmp_path / "downloads"),
        db_manager=None,
        headless=True,
    )


def test_popup_download_skip_matches_existing_file_with_chrome_suffix(tmp_path):
    review_root = tmp_path / "case" / "06_閱卷資料"
    dated_folder = review_root / "20260709"
    dated_folder.mkdir(parents=True)
    existing = dated_folder / "113年度訴字第123號_卷宗.pdf"
    existing.write_text("already archived", encoding="utf-8")

    mgr = _manager(tmp_path)
    index = mgr._build_existing_review_file_index(str(review_root))

    row = FakeElement(text="113年度訴字第123號_卷宗 (1).pdf 下載")
    button = FakeElement(attrs={"title": "下載"})

    matched = mgr._popup_download_already_exists(
        row=row,
        button=button,
        existing_index=index,
        review_root_folder=str(review_root),
    )

    assert matched == ("113年度訴字第123號_卷宗 (1).pdf 下載", str(existing))


def test_popup_download_skip_extracts_filename_from_row_children(tmp_path):
    review_root = tmp_path / "case" / "06_閱卷資料"
    dated_folder = review_root / "20260709"
    dated_folder.mkdir(parents=True)
    existing = dated_folder / "刑事卷證資料.pdf"
    existing.write_text("already archived", encoding="utf-8")

    mgr = _manager(tmp_path)
    index = mgr._build_existing_review_file_index(str(review_root))

    row = FakeElement(
        text="",
        children=[
            FakeElement(text="序號 1"),
            FakeElement(text="刑事卷證資料.pdf"),
            FakeElement(text="下載"),
        ],
    )
    button = FakeElement(attrs={"title": "下載"})

    matched = mgr._popup_download_already_exists(
        row=row,
        button=button,
        existing_index=index,
        review_root_folder=str(review_root),
    )

    assert matched == ("刑事卷證資料.pdf", str(existing))


def test_popup_download_does_not_skip_when_no_filename_candidate(tmp_path):
    review_root = tmp_path / "case" / "06_閱卷資料"
    dated_folder = review_root / "20260709"
    dated_folder.mkdir(parents=True)
    (dated_folder / "既有卷.pdf").write_text("already archived", encoding="utf-8")

    mgr = _manager(tmp_path)
    index = mgr._build_existing_review_file_index(str(review_root))

    row = FakeElement(text="下載")
    button = FakeElement(attrs={"title": "下載"})

    assert (
        mgr._popup_download_already_exists(
            row=row,
            button=button,
            existing_index=index,
            review_root_folder=str(review_root),
        )
        is None
    )


def test_existing_review_index_normalizes_loose_root_imports(tmp_path):
    review_root = tmp_path / "case" / "06_閱卷資料"
    review_root.mkdir(parents=True)
    loose = review_root / "20260703 人工匯入卷宗.pdf"
    loose.write_bytes(b"%PDF loose root import")

    mgr = _manager(tmp_path)
    index = mgr._build_existing_review_file_index(str(review_root))

    moved = review_root / "20260703" / loose.name
    assert moved.exists()
    assert not loose.exists()
    assert any(key in index for key in mgr._review_filename_keys(loose.name))


def test_existing_review_index_stages_duplicate_loose_root_import(tmp_path):
    review_root = tmp_path / "case" / "06_閱卷資料"
    dated_folder = review_root / "20260709"
    dated_folder.mkdir(parents=True)
    existing = dated_folder / "20260709 人工匯入卷宗.pdf"
    existing.write_bytes(b"%PDF same payload")
    loose = review_root / "20260709 人工匯入卷宗.pdf"
    loose.write_bytes(b"%PDF same payload")

    mgr = _manager(tmp_path)
    index = mgr._build_existing_review_file_index(str(review_root))

    assert existing.exists()
    assert not loose.exists()
    quarantined = list(Path(mgr.download_folder).glob("_duplicate_downloads/*/20260709 人工匯入卷宗.loose_root_duplicate.pdf"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"%PDF same payload"
    assert any(key in index for key in mgr._review_filename_keys(existing.name))


def test_download_snapshot_tracks_same_filename_by_full_path(tmp_path):
    mgr = _manager(tmp_path)
    download_root = Path(mgr.download_folder)
    today_folder = download_root / "20260702"
    today_folder.mkdir(parents=True)
    download_root.mkdir(exist_ok=True)

    existing = download_root / "卷宗.pdf"
    existing.write_bytes(b"%PDF already in root")

    snapshot = mgr._snapshot_download_file_mtimes([str(today_folder), str(download_root)])

    same_name_in_today = today_folder / "卷宗.pdf"
    same_name_in_today.write_bytes(b"%PDF newly downloaded")

    assert mgr._download_file_changed_since_snapshot(str(existing), snapshot) is False
    assert mgr._download_file_changed_since_snapshot(str(same_name_in_today), snapshot) is True


def test_download_snapshot_detects_modified_file_by_full_path(tmp_path):
    mgr = _manager(tmp_path)
    download_root = Path(mgr.download_folder)
    download_root.mkdir(parents=True, exist_ok=True)
    existing = download_root / "卷宗.pdf"
    existing.write_bytes(b"%PDF v1")

    snapshot = mgr._snapshot_download_file_mtimes([str(download_root)])
    old_mtime = snapshot[str(existing)]
    os.utime(existing, (old_mtime + 2, old_mtime + 2))

    assert mgr._download_file_changed_since_snapshot(str(existing), snapshot) is True


def test_payment_notification_seen_matches_case_party_key(tmp_path):
    mgr = _manager(tmp_path)
    mgr.notified_cases.add("web_payment:case:115原交易21:林建豐")

    assert mgr._payment_notification_already_seen(
        {
            "yyidno": "115.原交易.000021",
            "clnm": "林建豐",
            "rowid": "1075000",
        }
    ) is True


def test_archive_duplicate_review_content_isolates_staging_copy(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    case_folder = tmp_path / "case" / "2026-0071-李滿金-一審-詐欺、洗錢防制法"
    review_root = case_folder / "06_閱卷資料"
    review_root.mkdir(parents=True)
    existing = review_root / "人工匯入卷宗.pdf"
    existing.write_bytes(b"%PDF-1.4\nsame-review-payload")

    download_src = Path(mgr.download_folder) / "20260709" / "法院下載不同檔名.pdf"
    download_src.parent.mkdir(parents=True)
    download_src.write_bytes(b"%PDF-1.4\nsame-review-payload")

    meta = {
        "showyyidno": "115年度原金訴字第000088號",
        "case_number": "115.原金訴.000088",
        "party": "李滿金",
    }
    mgr._last_download_meta_by_file = {str(download_src): meta, download_src.name: meta}
    monkeypatch.setattr(mgr, "_resolve_case_folder", lambda _info: str(case_folder))

    mgr._archive_to_case_folders([str(download_src)], [meta])

    assert existing.exists()
    assert not download_src.exists()
    quarantined = list(Path(mgr.download_folder).glob("_duplicate_downloads/*/*.pdf"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"%PDF-1.4\nsame-review-payload"
    items = mgr._last_archive_report["items"]
    assert items[0]["action"] == "exists_skip"
    assert items[0]["dst"] == str(existing)
