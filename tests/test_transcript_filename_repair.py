import importlib.util
from pathlib import Path


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
    assert mod._record_parse_ready_for_filename({"date": "20260618", "type": "調解"}) is False
    assert (
        downloader._generate_record_filename(
            {"date": "N/A", "type": "N/A", "period": "N/A", "time": "N/A"},
            "raw-download.pdf",
        )
        == "raw-download.pdf"
    )


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
