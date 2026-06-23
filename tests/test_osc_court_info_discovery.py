import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = ROOT / "skills" / "osc-orchestrator" / "action.py"

spec = importlib.util.spec_from_file_location("osc_orchestrator_action_for_test", ACTION_PATH)
action = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(action)


def test_discover_case_court_info_prefers_latest_non_debt_case_number(tmp_path):
    case_dir = tmp_path / "2025-0114-黃淨雅-一審-侵權行為"
    notice_dir = case_dir / "07_法院通知或程序裁定"
    notice_dir.mkdir(parents=True)
    (notice_dir / "20251209 臺北地方法院114年度北司小調字第2918號臺北簡易庭通知書.pdf").write_bytes(b"%PDF")
    (notice_dir / "20260414 臺北地方法院臺北簡易庭115年度北小字第1213號通知書.pdf").write_bytes(b"%PDF")

    info = action._discover_case_court_info(str(case_dir))

    assert info["court_name"] == "臺灣臺北地方法院"
    assert info["court_case_number"] == "115年度北小字第001213號"


def test_discover_case_court_info_excludes_criminal_detention_rulings(tmp_path):
    case_dir = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    notice_dir = case_dir / "09_法院通知或程序裁定"
    notice_dir.mkdir(parents=True)
    (notice_dir / "20260402 臺北地方法院115年度聲羈字第1號刑事裁定（停止羈押）.pdf").write_bytes(b"%PDF")
    (notice_dir / "20260320 臺北地方法院114年度訴字第972號刑事庭通知書.pdf").write_bytes(b"%PDF")

    info = action._discover_case_court_info(str(case_dir))

    assert info["court_case_number"] == "114年度訴字第000972號"


def test_discover_case_court_info_prefers_judgment_over_later_procedural_number(tmp_path):
    case_dir = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    notice_dir = case_dir / "09_法院通知或程序裁定"
    judgment_dir = case_dir / "10_判決書或終局裁定及處分"
    notice_dir.mkdir(parents=True)
    judgment_dir.mkdir(parents=True)
    (notice_dir / "20260711 臺北地方法院114年度國審強處字第1號國民法官庭通知書(游秀鈴；訂調查).pdf").write_bytes(b"%PDF")
    (notice_dir / "20260507 臺北地方法院114年度國審訴字第1號國民法官庭通知書(游秀鈴).pdf").write_bytes(b"%PDF")
    (judgment_dir / "20260518 臺北地方法院114年度訴字第972號刑事判決(游秀鈴；主文：共同犯傷害致人於死罪).pdf").write_bytes(b"%PDF")

    info = action._discover_case_court_info(str(case_dir))

    assert info["court_case_number"] == "114年度訴字第000972號"


def test_discover_case_court_info_uses_local_alias_for_canonical_windows_path(tmp_path, monkeypatch):
    case_dir = tmp_path / "2025-0002-游秀鈴-一審-傷害致死"
    judgment_dir = case_dir / "10_判決書或終局裁定及處分"
    judgment_dir.mkdir(parents=True)
    (judgment_dir / "20260518 臺北地方法院114年度訴字第972號刑事判決(游秀鈴).pdf").write_bytes(b"%PDF")

    canonical = r"Z:\lumi63181107\01_案件\法扶案件\刑事\2025-0002-游秀鈴-一審-傷害致死"
    monkeypatch.setattr(action, "local_case_path_candidates", lambda path: [str(case_dir)] if path == canonical else [])

    info = action._discover_case_court_info(canonical)

    assert info["court_name"] == "臺灣臺北地方法院"
    assert info["court_case_number"] == "114年度訴字第000972號"
