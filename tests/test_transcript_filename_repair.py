import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "judicial_automation_v2.py"
REPAIR = ROOT / "scripts" / "ops" / "repair_transcript_filenames.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("judicial_automation_v2_for_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_repair_module():
    spec = importlib.util.spec_from_file_location("repair_transcript_filenames_for_test", REPAIR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_00000000_transcript_is_not_treated_as_final_name():
    mod = _load_module()
    downloader = mod.CourtRecordDownloader(username="", password="", headless=True)
    assert downloader._is_original_download_filename("00000000 言詞辯論筆錄(下午0400)_10.pdf") is True
    assert downloader._is_original_download_filename("20240618 言詞辯論筆錄(下午0400).pdf") is False


def test_transcript_filename_generation_rejects_unusable_parse_values(tmp_path):
    mod = _load_module()
    downloader = mod.CourtRecordDownloader(username="", password="", headless=True, download_folder=str(tmp_path))

    assert mod._record_parse_ready_for_filename({"date": "N/A", "type": "N/A", "period": "N/A"}) is False
    assert mod._record_parse_ready_for_filename({"date": "00000000", "type": "調解程序筆錄"}) is False
    assert mod._record_parse_ready_for_filename({"date": "20260230", "type": "調解程序筆錄"}) is False
    assert mod._record_parse_ready_for_filename({"date": "19700504", "type": "調解程序筆錄"}) is True
    assert mod._record_parse_ready_for_filename({"date": "19500101", "type": "調解程序筆錄"}) is True
    assert mod._record_parse_ready_for_filename({"date": "19000101", "type": "調解程序筆錄"}) is False
    assert mod._record_parse_ready_for_filename({"date": "20260618", "type": "調解"}) is False
    assert (
        downloader._generate_record_filename(
            {"date": "N/A", "type": "N/A", "period": "N/A", "time": "N/A"},
            "raw-download.pdf",
        )
        == "raw-download.pdf"
    )


def test_transcript_metadata_receipt_category_is_field_specific_and_privacy_safe():
    mod = _load_module()

    assert mod._transcript_filename_metadata_category({"date": None, "type": None}) == "metadata_date_and_type_unresolved"
    assert mod._transcript_filename_metadata_category({"date": "20260714", "type": None}) == "metadata_type_unresolved"
    assert mod._transcript_filename_metadata_category({"date": None, "type": "準備程序筆錄"}) == "metadata_date_unresolved"
    assert mod._transcript_filename_metadata_category({"date": "20260714", "type": "準備程序筆錄"}) == "metadata_unresolved"


def test_transcript_metadata_extracts_record_from_second_page_without_cross_page_mix():
    mod = _load_module()

    parsed = mod._extract_transcript_metadata_from_text_pages(
        [
            "電子卷證封面\n收文日期 2026-08-01",
            "臺灣臺北地方法院 \u6e96 備 程 序 筆 錄\n中 華 民 國 115 年 8 月 4 日 上 午 9 時 30 分",
        ]
    )

    assert parsed == {
        "date": "20260804",
        "type": "準備程序筆錄",
        "period": "上午",
        "time": "0930",
    }
    assert mod._extract_transcript_metadata_from_text_pages(
        ["中華民國115年8月4日", "準備程序筆錄"]
    )["date"] is None


def test_transcript_metadata_accepts_chinese_numeral_roc_date_on_proven_record_page():
    mod = _load_module()

    parsed = mod._extract_transcript_metadata_from_text_pages(
        ["臺灣某地方法院言詞辯論筆錄\n中華民國一百一十五年八月十日下午二時五分"]
    )

    assert parsed["date"] == "20260810"
    assert parsed["type"] == "言詞辯論筆錄"
    assert parsed["period"] == "下午"
    assert parsed["time"] == "0205"


def test_transcript_metadata_accepts_historical_roc_date_on_same_record_page():
    mod = _load_module()

    parsed = mod._extract_transcript_metadata_from_text_pages(
        ["臺灣某地方法院言詞辯論筆錄\n中 華 民 國 88 年 7 月 9 日"]
    )

    assert parsed["date"] == "19990709"
    assert parsed["type"] == "言詞辯論筆錄"


def test_transcript_parse_sanitizer_quarantines_poisoned_date_and_normalizes_time():
    mod = _load_module()

    cleaned = mod._sanitize_transcript_parse_result(
        {
            "date": "19000101",
            "type": "準備程序筆錄",
            "period": "上午0930",
            "time": "0930",
            "internal_note": "/private/runtime/trace-123",
        }
    )

    assert cleaned == {
        "date": None,
        "type": "準備程序筆錄",
        "period": "上午",
        "time": "0930",
    }
    assert mod._sanitize_transcript_parse_result(
        {"date": "20260714", "type": "準備程序筆錄", "period": "下午9960"}
    )["time"] == ""
    assert mod._sanitize_transcript_parse_result(
        {
            "date": "20260714",
            "type": "準備程序筆錄",
            "period": "上午0930",
            "time": "1030",
        }
    )["time"] == ""


def test_transcript_cached_parse_is_sanitized_and_rewritten(tmp_path):
    mod = _load_module()
    downloader = mod.CourtRecordDownloader.__new__(mod.CourtRecordDownloader)
    downloader.gemini_cache = {
        "cached": {
            "date": "19000101",
            "type": "調解程序筆錄",
            "period": "下午0230",
            "time": "0230",
        }
    }
    downloader._get_text_hash = lambda _text: "cached"
    downloader.log = lambda _message: None
    saved = []
    downloader._save_gemini_cache = lambda: saved.append(dict(downloader.gemini_cache))

    result = downloader._parse_with_gemini_cached("ignored")

    assert result["date"] is None
    assert result["type"] == "調解程序筆錄"
    assert result["period"] == "下午"
    assert result["time"] == "0230"
    assert downloader.gemini_cache["cached"] == result
    assert len(saved) == 1


def test_read_only_transcript_folder_probe_never_creates_empty_directory(tmp_path):
    mod = _load_module()
    downloader = mod.CourtRecordDownloader.__new__(mod.CourtRecordDownloader)
    scheduler = mod.TranscriptAutoDownloader.__new__(mod.TranscriptAutoDownloader)

    assert downloader._find_existing_transcript_folder(str(tmp_path)) is None
    assert scheduler._find_existing_transcript_folder(str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []


def test_transcript_pdf_inventory_excludes_appledouble_and_hidden_sidecars():
    mod = _load_module()

    assert mod._is_real_pdf_filename("20260714 準備程序筆錄.pdf") is True
    assert mod._is_real_pdf_filename("._20260714 準備程序筆錄.pdf") is False
    assert mod._is_real_pdf_filename(".hidden.pdf") is False
    assert mod._is_real_pdf_filename("20260714 準備程序筆錄.pdf.crdownload") is False


def test_downloader_rename_never_parses_appledouble_sidecar(tmp_path, monkeypatch):
    mod = _load_module()
    real_pdf = tmp_path / "raw-download.pdf"
    sidecar = tmp_path / "._raw-download.pdf"
    real_pdf.write_bytes(b"%PDF-real")
    sidecar.write_bytes(b"AppleDouble")
    parsed = []

    downloader = mod.CourtRecordDownloader.__new__(mod.CourtRecordDownloader)
    downloader.log = lambda _message: None
    downloader.get_cases_from_db = lambda: [
        SimpleNamespace(folder_path=str(tmp_path), court_name="臺北地院", court_case_number="115年度訴字第1號")
    ]
    downloader._find_existing_transcript_folder = lambda _path: str(tmp_path)
    downloader._parse_record_pdf = lambda path: (
        parsed.append(Path(path).name)
        or {"date": "20260714", "type": "準備程序筆錄", "period": "上午"}
    )
    downloader._generate_record_filename = lambda _result, name: name
    monkeypatch.setattr(mod, "translate_case_path_to_local", lambda path: path)
    monkeypatch.setattr(mod, "_global_transcript_operation_in_progress", False)

    downloader.rename_all_transcripts()

    assert parsed == ["raw-download.pdf"]
    assert sidecar.exists()


def test_downloader_rename_surfaces_unreadable_pdf_as_privacy_safe_retry(tmp_path, monkeypatch):
    mod = _load_module()
    raw_pdf = tmp_path / "private-client-raw-download.pdf"
    raw_pdf.write_bytes(b"not a readable pdf")

    downloader = mod.CourtRecordDownloader.__new__(mod.CourtRecordDownloader)
    downloader.log = lambda _message: None
    downloader.get_cases_from_db = lambda: [
        SimpleNamespace(
            folder_path=str(tmp_path),
            court_name="臺北地院",
            court_case_number="115年度訴字第1號",
        )
    ]
    downloader._find_existing_transcript_folder = lambda _path: str(tmp_path)

    def _unreadable(_path):
        downloader._last_record_parse_error = "pdf_unreadable"
        return {"date": None, "type": None, "period": None, "time": None}

    downloader._parse_record_pdf = _unreadable
    monkeypatch.setattr(mod, "translate_case_path_to_local", lambda path: path)
    monkeypatch.setattr(mod, "_global_transcript_operation_in_progress", False)

    result = downloader.rename_all_transcripts()

    assert result["success"] is True
    assert result["status"] == "partial_retry_pending"
    assert result["retryable"] is True
    assert result["parse_failed_count"] == 1
    assert result["retry_pending_count"] == 1
    assert result["failure_receipts"][0]["category"] == "pdf_unreadable"
    assert len(result["failure_receipts"][0]["file_token"]) == 16
    assert "private-client" not in str(result["failure_receipts"])


def test_rename_inventory_exception_does_not_log_private_path_or_traceback(monkeypatch):
    mod = _load_module()
    downloader = mod.CourtRecordDownloader.__new__(mod.CourtRecordDownloader)
    logs = []
    downloader.log = logs.append
    downloader.get_cases_from_db = lambda: (_ for _ in ()).throw(
        RuntimeError("/private/cases/secret-client.pdf")
    )
    monkeypatch.setattr(mod, "_global_transcript_operation_in_progress", False)

    result = downloader.rename_all_transcripts()

    assert result["reason"] == "rename_inventory_failed"
    assert result["exception_category"] == "rename_inventory_exception"
    assert result["retryable"] is True
    assert "/private/" not in "\n".join(logs)
    assert "secret-client" not in "\n".join(logs)


def test_downloader_rename_receipt_distinguishes_missing_filename_field(tmp_path, monkeypatch):
    mod = _load_module()
    raw_pdf = tmp_path / "private-client-raw-download.pdf"
    raw_pdf.write_bytes(b"not inspected by this fixture")

    downloader = mod.CourtRecordDownloader.__new__(mod.CourtRecordDownloader)
    downloader.log = lambda _message: None
    downloader.get_cases_from_db = lambda: [
        SimpleNamespace(folder_path=str(tmp_path), court_name="court", court_case_number="case")
    ]
    downloader._find_existing_transcript_folder = lambda _path: str(tmp_path)
    downloader._parse_record_pdf = lambda _path: {
        "date": "20260714", "type": None, "period": None, "time": None
    }
    monkeypatch.setattr(mod, "translate_case_path_to_local", lambda path: path)
    monkeypatch.setattr(mod, "_global_transcript_operation_in_progress", False)

    result = downloader.rename_all_transcripts()

    assert result["success"] is True
    assert result["metadata_pending_count"] == 1
    assert result["failure_receipts"][0]["category"] == "metadata_type_unresolved"
    assert len(result["failure_receipts"][0]["file_token"]) == 16
    assert "private-client" not in str(result["failure_receipts"])


def test_pdf_parser_records_stable_error_without_leaking_path(tmp_path, monkeypatch):
    mod = _load_module()
    downloader = mod.CourtRecordDownloader.__new__(mod.CourtRecordDownloader)
    logs = []
    downloader.log = logs.append

    class _BrokenFitz:
        @staticmethod
        def open(_path):
            raise RuntimeError("synthetic parser failure")

    monkeypatch.setitem(sys.modules, "fitz", _BrokenFitz)
    result = downloader._parse_record_pdf(str(tmp_path / "client-name.pdf"))

    assert result == {"date": None, "type": None, "period": None, "time": None}
    assert downloader._last_record_parse_error == "pdf_unreadable"


def test_pdf_parser_distinguishes_unavailable_file_provider_source(tmp_path, monkeypatch):
    mod = _load_module()
    downloader = mod.CourtRecordDownloader.__new__(mod.CourtRecordDownloader)
    logs = []
    downloader.log = logs.append

    real_open = open

    def _timeout_open(path, *args, **kwargs):
        if str(path).endswith("cloud-placeholder.pdf"):
            raise TimeoutError("synthetic file provider timeout")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _timeout_open)
    result = downloader._parse_record_pdf(str(tmp_path / "cloud-placeholder.pdf"))

    assert result == {"date": None, "type": None, "period": None, "time": None}
    assert downloader._last_record_parse_error == "pdf_source_unavailable"
    assert "cloud-placeholder" not in " ".join(logs)


def test_scheduler_rename_skips_sidecars_and_standard_names_before_parse(tmp_path, monkeypatch):
    mod = _load_module()
    (tmp_path / "raw-download.pdf").write_bytes(b"%PDF-real")
    (tmp_path / "._raw-download.pdf").write_bytes(b"AppleDouble")
    (tmp_path / "20260714 準備程序筆錄.pdf").write_bytes(b"%PDF-standard")
    parsed = []

    class FakeDownloader:
        def __init__(self, **_kwargs):
            pass

        def get_cases_from_db(self):
            return [SimpleNamespace(folder_path=str(tmp_path), court_name="臺北地院", court_case_number="115年度訴字第1號")]

        def _is_original_download_filename(self, name):
            return not name.startswith("20260714 ")

        def _parse_record_pdf(self, path):
            parsed.append(Path(path).name)
            return {"date": "20260714", "type": "準備程序筆錄", "period": "上午"}

        def _generate_record_filename(self, _result, name):
            return name

    scheduler = mod.TranscriptAutoDownloader.__new__(mod.TranscriptAutoDownloader)
    scheduler.username = ""
    scheduler.password = ""
    scheduler.db_manager = None
    scheduler.headless = True
    scheduler.log_callback = None
    scheduler.log = lambda _message: None
    scheduler.translate_path_to_local = lambda path: path
    scheduler._find_existing_transcript_folder = lambda _path: str(tmp_path)
    monkeypatch.setattr(mod, "CourtRecordDownloader", FakeDownloader)

    scheduler.rename_all_transcripts()

    assert parsed == ["raw-download.pdf"]


def test_transcript_filename_generation_accepts_valid_parse_result(tmp_path):
    mod = _load_module()
    downloader = mod.CourtRecordDownloader(username="", password="", headless=True, download_folder=str(tmp_path))

    assert mod._record_parse_ready_for_filename({"date": "20260618", "type": "調解程序筆錄"}) is True
    assert (
        downloader._generate_record_filename(
            {"date": "20260618", "type": "調解程序筆錄", "period": "上午", "time": "0930"},
            "raw-download.pdf",
        )
        == "20260618 調解程序筆錄(上午0930).pdf"
    )


def test_scheduler_filename_generation_uses_normalized_period_and_time():
    mod = _load_module()
    scheduler = mod.TranscriptAutoDownloader.__new__(mod.TranscriptAutoDownloader)
    scheduler.log = lambda _message: None

    assert (
        scheduler._generate_record_filename(
            {
                "date": "20260618",
                "type": "調解程序筆錄",
                "period": "上午",
                "time": "0930",
            },
            "raw-download.pdf",
        )
        == "20260618 調解程序筆錄(上午0930).pdf"
    )


def test_repair_standard_collision_does_not_parse_pdf(tmp_path):
    repair = _load_repair_module()

    class DummyDownloader:
        def _calculate_file_md5(self, path):
            import hashlib

            return hashlib.md5(Path(path).read_bytes()).hexdigest()

        def _parse_record_pdf(self, path):
            raise AssertionError("standard duplicate repair should not parse PDFs")

    base = tmp_path / "20240618 言詞辯論筆錄(下午0400).pdf"
    dup = tmp_path / "20240618 言詞辯論筆錄(下午0400)_2.pdf"
    base.write_bytes(b"same transcript")
    dup.write_bytes(b"same transcript")

    result = repair.repair_folder(tmp_path, DummyDownloader(), apply=False)
    actions = result["actions"]
    assert [a["action"] for a in actions] == ["quarantine_duplicate"]
    assert actions[0]["from"].endswith("_2.pdf")


def test_transcript_ai_assist_uses_profile_aware_gateway(monkeypatch):
    mod = _load_module()
    calls = []

    class FakeGateway:
        def chat(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {
                "success": True,
                "response": '{"date":"20260714","type":"準備程序筆錄","period":"上午","time":"0930"}',
                "route": "omlx",
            }

    from skills.bridge import inference_gateway

    monkeypatch.setattr(inference_gateway, "InferenceGateway", FakeGateway)
    downloader = mod.CourtRecordDownloader(username="", password="", headless=True)

    result = downloader._parse_with_gemini("民國115年7月14日上午9時30分準備程序筆錄")

    assert result["date"] == "20260714"
    assert result["type"] == "準備程序筆錄"
    assert calls[0][1]["task_type"] == "transcribe"
    assert calls[0][1]["allow_synthetic_fallback"] is False


def test_transcript_ai_assist_accepts_json_wrapped_with_model_text(monkeypatch):
    mod = _load_module()

    class FakeGateway:
        def chat(self, _prompt, **_kwargs):
            return {
                "success": True,
                "response": (
                    "以下是結果：\n```json\n"
                    '{"date":"20260714","type":"準備程序筆錄","period":"上午","time":"0930"}'
                    "\n```\n請查收"
                ),
            }

    from skills.bridge import inference_gateway

    monkeypatch.setattr(inference_gateway, "InferenceGateway", FakeGateway)
    downloader = mod.CourtRecordDownloader(username="", password="", headless=True)

    result = downloader._parse_with_gemini("掃描筆錄")

    assert result["date"] == "20260714"
    assert result["type"] == "準備程序筆錄"


def test_transcript_vision_renders_png_for_gateway_without_8082_fallback(monkeypatch):
    mod = _load_module()
    rendered_paths = []

    class FakePixmap:
        def tobytes(self, kind):
            assert kind == "png"
            return b"rendered-png"

    class FakePage:
        rect = SimpleNamespace(x0=0, y0=0, x1=100, height=200)

        def get_pixmap(self, **kwargs):
            assert kwargs["dpi"] == 150
            return FakePixmap()

    class FakeDocument:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return FakePage()

        def close(self):
            return None

    fake_fitz = SimpleNamespace(
        open=lambda _path: FakeDocument(),
        Rect=lambda *args: args,
    )

    class FakeGateway:
        def vision(self, *, image_path, prompt, **kwargs):
            rendered_paths.append(image_path)
            assert Path(image_path).suffix == ".png"
            assert Path(image_path).read_bytes() == b"rendered-png"
            assert kwargs["task_type"] == "ocr"
            return {
                "success": True,
                "analysis": '{"date":"20260714","type":"準備程序筆錄","period":"上午","time":"0930"}',
                "route": "omlx",
                "model": "active-profile-model",
            }

    from skills.bridge import inference_gateway

    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)
    monkeypatch.setattr(inference_gateway, "InferenceGateway", FakeGateway)
    downloader = mod.CourtRecordDownloader(username="", password="", headless=True)

    result = downloader._parse_with_vision("/tmp/fake-transcript.pdf")

    assert result["date"] == "20260714"
    assert rendered_paths and not os.path.exists(rendered_paths[0])
    source = MODULE.read_text(encoding="utf-8")
    assert "casper_tools_client" not in source
    assert "http://127.0.0.1:8082" not in source
