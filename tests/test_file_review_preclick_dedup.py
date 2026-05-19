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
    review_root.mkdir(parents=True)
    existing = review_root / "113年度訴字第123號_卷宗.pdf"
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
    review_root.mkdir(parents=True)
    existing = review_root / "刑事卷證資料.pdf"
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
    review_root.mkdir(parents=True)
    (review_root / "既有卷.pdf").write_text("already archived", encoding="utf-8")

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
