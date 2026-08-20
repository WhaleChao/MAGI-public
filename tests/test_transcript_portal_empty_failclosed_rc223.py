from types import SimpleNamespace

import casper_ecosystem.law_firm_orchestrators.judicial_automation_v2 as module


class _Body:
    def __init__(self, text: str):
        self.text = text


class _Driver:
    def __init__(self, body_text: str):
        self._body_text = body_text
        self.page_source = f"<html><body>{body_text}</body></html>"

    def find_element(self, by, value):
        return _Body(self._body_text)

    def find_elements(self, by, value):
        return []

    def quit(self):
        return None


def _downloader(tmp_path, text: str):
    module.By = SimpleNamespace(TAG_NAME="tag name", XPATH="xpath")
    obj = module.CourtRecordDownloader.__new__(module.CourtRecordDownloader)
    obj.driver = _Driver(text)
    obj.download_folder = str(tmp_path)
    obj._last_download_error = ""
    obj._last_no_new_files_reason = ""
    obj._last_pdf_fetch_count = 0
    obj._last_pdf_known_duplicate_count = 0
    obj.log = lambda *_args, **_kwargs: None
    return obj


def test_explicit_portal_empty_is_a_verified_noop(tmp_path):
    downloader = _downloader(tmp_path, "查無符合條件之筆錄資料")

    files, clicked, total = downloader._download_pdfs_from_page(set())

    assert files == []
    assert clicked == 0
    assert total == 0
    assert downloader._last_no_new_files_reason == "portal_confirmed_empty"
    assert downloader._last_download_error == ""


def test_missing_pdf_links_without_empty_evidence_fails_closed(tmp_path):
    downloader = _downloader(tmp_path, "電子筆錄調閱查詢結果")

    files, clicked, total = downloader._download_pdfs_from_page(set())

    assert files == []
    assert clicked == 0
    assert total == 0
    assert downloader._last_no_new_files_reason == ""
    assert downloader._last_download_error.startswith("unverified_no_pdf_links:")


def test_archived_transcript_returns_relative_hash_bound_receipt(monkeypatch, tmp_path):
    case_root = tmp_path / "case"
    transcript_folder = case_root / "06_筆錄"
    transcript_folder.mkdir(parents=True)
    source = tmp_path / "downloaded.pdf"
    source.write_bytes(b"synthetic transcript bytes")

    downloader = module.CourtRecordDownloader.__new__(module.CourtRecordDownloader)
    downloader.log = lambda *_args, **_kwargs: None
    downloader._load_md5_records = lambda: {}
    saved = {}
    downloader._save_md5_records = lambda rows: saved.update(rows)
    downloader.find_transcript_folder = lambda _root: str(transcript_folder)
    downloader._all_existing_transcript_folders = lambda _case, preferred_folder=None: [preferred_folder]
    downloader._transcript_pdf_matches_case = lambda _path, _case: True
    downloader._calculate_file_md5 = lambda _path: "m" * 32
    downloader._parse_record_pdf = lambda _path: {}
    monkeypatch.setattr(module, "translate_case_path_to_local", lambda _path: str(case_root))

    receipts = downloader.move_to_case_folder(
        SimpleNamespace(
            folder_path="synthetic/case",
            case_number="2026-0001",
            court_case_number="115年度訴字第1號",
            client_name="測試",
        ),
        [str(source)],
    )

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["status"] == "archived"
    assert receipt["archive_reference"] == "06_筆錄/downloaded.pdf"
    assert len(receipt["sha256"]) == 64
    assert receipt["case_identity_match"] is True
    assert receipt["readable"] is True
    assert not receipt["archive_reference"].startswith("/")
    assert saved


def test_hash_duplicate_repairs_legacy_placeholder_only_with_pdf_metadata(monkeypatch, tmp_path):
    case_root = tmp_path / "case"
    transcript_folder = case_root / "06_筆錄"
    transcript_folder.mkdir(parents=True)
    existing = transcript_folder / "00000000 言詞辯論筆錄(下午0400)_15.pdf"
    existing.write_bytes(b"same transcript")
    source = tmp_path / "portal-download.pdf"
    source.write_bytes(b"same transcript")

    downloader = module.CourtRecordDownloader.__new__(module.CourtRecordDownloader)
    downloader.log = lambda *_args, **_kwargs: None
    downloader._load_md5_records = lambda: {}
    downloader._save_md5_records = lambda _rows: None
    downloader.find_transcript_folder = lambda _root: str(transcript_folder)
    downloader._all_existing_transcript_folders = lambda _case, preferred_folder=None: [preferred_folder]
    downloader._transcript_pdf_matches_case = lambda _path, _case: True
    downloader._calculate_file_md5 = lambda _path: "m" * 32
    downloader._parse_record_pdf = lambda _path: {
        "date": "20260714", "type": "言詞辯論筆錄", "period": "下午", "time": "0400"
    }
    monkeypatch.setattr(module, "translate_case_path_to_local", lambda _path: str(case_root))

    receipts = downloader.move_to_case_folder(
        SimpleNamespace(
            folder_path="synthetic/case", case_number="2026-0001", court_case_number="case", client_name=""
        ),
        [str(source)],
    )

    assert receipts[0]["status"] == "duplicate_existing"
    assert not existing.exists()
    assert (transcript_folder / "20260714 言詞辯論筆錄(下午0400).pdf").exists()
    assert receipts[0]["archive_reference"] == "06_筆錄/20260714 言詞辯論筆錄(下午0400).pdf"


def test_hash_duplicate_keeps_placeholder_when_pdf_metadata_is_not_trustworthy(monkeypatch, tmp_path):
    case_root = tmp_path / "case"
    transcript_folder = case_root / "06_筆錄"
    transcript_folder.mkdir(parents=True)
    existing = transcript_folder / "00000000 筆錄.pdf"
    existing.write_bytes(b"same transcript")
    source = tmp_path / "portal-download.pdf"
    source.write_bytes(b"same transcript")

    downloader = module.CourtRecordDownloader.__new__(module.CourtRecordDownloader)
    downloader.log = lambda *_args, **_kwargs: None
    downloader._load_md5_records = lambda: {}
    downloader._save_md5_records = lambda _rows: None
    downloader.find_transcript_folder = lambda _root: str(transcript_folder)
    downloader._all_existing_transcript_folders = lambda _case, preferred_folder=None: [preferred_folder]
    downloader._transcript_pdf_matches_case = lambda _path, _case: True
    downloader._calculate_file_md5 = lambda _path: "m" * 32
    downloader._parse_record_pdf = lambda _path: {"date": None, "type": "筆錄"}
    monkeypatch.setattr(module, "translate_case_path_to_local", lambda _path: str(case_root))

    downloader.move_to_case_folder(
        SimpleNamespace(
            folder_path="synthetic/case", case_number="2026-0001", court_case_number="case", client_name=""
        ),
        [str(source)],
    )

    assert existing.exists()
    assert not (transcript_folder / "00000000 筆錄_2.pdf").exists()


def test_transcript_archive_reference_falls_back_without_leaking_absolute_path(monkeypatch, tmp_path):
    source = tmp_path / "outside" / "private-transcript.pdf"

    def _raise_on_resolve(_self):
        raise OSError("synthetic disconnected share")

    monkeypatch.setattr(module.Path, "resolve", _raise_on_resolve)

    reference = module.CourtRecordDownloader._transcript_archive_reference(
        str(source), str(tmp_path / "case")
    )

    assert reference == "private-transcript.pdf"
    assert not reference.startswith("/")


def test_visible_text_controls_empty_state_not_hidden_script(tmp_path):
    downloader = _downloader(tmp_path, "電子筆錄調閱查詢結果")
    downloader.driver.page_source = (
        "<html><script>const emptyMessage='查無資料';</script>"
        "<body>電子筆錄調閱查詢結果</body></html>"
    )

    assert downloader._portal_has_explicit_empty_state() is False


def test_alert_confirmed_empty_short_circuits_download(tmp_path):
    downloader = _downloader(tmp_path, "")
    downloader.logged_in = True

    def _execute(_case):
        downloader._last_no_new_files_reason = "portal_confirmed_empty"
        return True

    downloader._execute_search_query = _execute
    downloader._process_search_results = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("confirmed empty must not inspect download links")
    )
    case = SimpleNamespace(court_name="臺灣臺東地方法院", court_case_number="114年度東原簡字第18號")

    assert downloader.download_record(case) == []
    assert downloader._last_download_error == ""


def test_expired_mid_batch_session_is_rebuilt_once_and_same_case_retried(tmp_path):
    downloader = _downloader(tmp_path, "")
    downloader.logged_in = True
    downloader._last_download_error = ""
    calls = {"query": 0, "close": 0, "login": 0}

    def _execute(_case):
        calls["query"] += 1
        if calls["query"] == 1:
            downloader._last_download_error = (
                "transcript_search_page_unavailable: session expired"
            )
            return False
        downloader._last_no_new_files_reason = "portal_confirmed_empty"
        return True

    def _close():
        calls["close"] += 1
        downloader.driver = None
        downloader.logged_in = False

    def _login(max_retries=0):
        calls["login"] += 1
        assert max_retries == 2
        downloader.driver = _Driver("")
        downloader.logged_in = True
        return True

    downloader._execute_search_query = _execute
    downloader.close = _close
    downloader.login = _login
    downloader._process_search_results = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("confirmed empty must not inspect download links")
    )
    case = SimpleNamespace(
        court_name="臺灣臺東地方法院",
        court_case_number="114年度東原簡字第18號",
    )

    assert downloader.download_record(case) == []
    assert calls == {"query": 2, "close": 1, "login": 1}
    assert downloader._last_download_error == ""


def test_close_clears_in_memory_login_state(tmp_path):
    downloader = _downloader(tmp_path, "")
    downloader.logged_in = True

    downloader.close()

    assert downloader.driver is None
    assert downloader.logged_in is False


def test_batch_inventory_excludes_blank_court_metadata(tmp_path):
    class _ReadOnlyDb:
        def __init__(self):
            self.query = ""

        def execute(self, query, fetch=None):
            self.query = str(query)
            assert fetch == "all"
            return []

    db = _ReadOnlyDb()
    downloader = _downloader(tmp_path, "")
    downloader.db = db

    assert downloader.get_cases_from_db() == []
    assert "TRIM(court_name) != ''" in db.query


def test_portal_docket_match_normalizes_table_format_and_zero_padding():
    row = "臺灣臺東地方法院\n刑事 114.東原簡.000018\n當事人"

    assert module._portal_row_matches_case_text(row, "114年度東原簡字第000018號") is True
    assert module._portal_row_matches_case_text(row, "114年度附民字第001289號") is False


def test_pdf_text_with_another_docket_cannot_match_by_party_name():
    text = "臺灣士林地方法院 114年度附民字第1289號\n當事人 測試人"

    assert module._transcript_text_matches_case(
        text,
        "114年度東原簡字第18號",
        "測試人",
    ) is False


def test_pdf_text_without_docket_is_quarantined_even_when_party_matches():
    assert module._transcript_text_matches_case(
        "準備程序筆錄\n當事人：測試人",
        "114年度東原簡字第18號",
        "測試人",
    ) is False


def test_multi_case_result_controls_are_scoped_to_exact_docket():
    class _Element:
        def __init__(self, identity):
            self.id = identity

    class _Row:
        def __init__(self, text, controls):
            self.text = text
            self.controls = controls

        def find_elements(self, _by, _xpath):
            return self.controls

    target = _Element("target")
    unrelated = _Element("unrelated")
    rows = [
        _Row("刑事 114.東原簡.000018", [target]),
        _Row("刑事 114.附民.001289", [unrelated]),
    ]

    class _ScopedDriver:
        def find_elements(self, _by, xpath):
            return rows if xpath == "//tr" else [target, unrelated]

    module.By = SimpleNamespace(TAG_NAME="tag name", XPATH="xpath")
    downloader = module.CourtRecordDownloader.__new__(module.CourtRecordDownloader)
    downloader.driver = _ScopedDriver()
    downloader._last_download_error = ""
    downloader.log = lambda *_args, **_kwargs: None
    case = SimpleNamespace(court_case_number="114年度東原簡字第18號")

    controls = downloader._case_scoped_result_elements(
        "//input[@type='button' and @value='立即調閱']",
        case,
    )

    assert controls == [target]
    assert downloader._last_download_error == ""


def test_exact_docket_expired_row_is_verified_unavailable():
    class _Row:
        def __init__(self, text):
            self.text = text

    rows = [
        _Row("刑事 114.東原簡.000018 已於115/08/20超過下載期限"),
        _Row("刑事 114.附民.001289 線上下載"),
    ]

    class _ScopedDriver:
        def find_elements(self, _by, xpath):
            return rows if xpath == "//tr" else []

    module.By = SimpleNamespace(TAG_NAME="tag name", XPATH="xpath")
    downloader = module.CourtRecordDownloader.__new__(module.CourtRecordDownloader)
    downloader.driver = _ScopedDriver()
    case = SimpleNamespace(court_case_number="114年度東原簡字第18號")

    assert downloader._portal_has_explicit_unavailable_state(case) is True


def test_other_docket_expired_row_cannot_close_target_fail_closed():
    class _Row:
        def __init__(self, text):
            self.text = text

    rows = [
        _Row("刑事 114.東原簡.000018 法院回覆同意"),
        _Row("刑事 114.附民.001289 已於115/08/20超過下載期限"),
    ]

    class _ScopedDriver:
        def find_elements(self, _by, xpath):
            return rows if xpath == "//tr" else []

    module.By = SimpleNamespace(TAG_NAME="tag name", XPATH="xpath")
    downloader = module.CourtRecordDownloader.__new__(module.CourtRecordDownloader)
    downloader.driver = _ScopedDriver()
    case = SimpleNamespace(court_case_number="114年度東原簡字第18號")

    assert downloader._portal_has_explicit_unavailable_state(case) is False
