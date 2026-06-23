# -*- coding: utf-8 -*-
"""Tests for expanded OSC _TODO_PATTERNS covering 5 deadline categories."""
import sys
import os
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "osc-orchestrator"))

from osc_headless.todos import (
    extract_todos_from_filename,
    extract_base_year_from_filename,
    _extract_todo_from_filename,
    extract_document_date_from_filename,
)
import osc_headless.todos as todos_mod


def _extract(filename, file_path=""):
    todos = extract_todos_from_filename(filename, file_path)
    return todos


# ── 補正 ──
def test_補正_pattern1():
    todos = _extract("20240305 裁定（王大明；應於本裁定送達後20日內補正）.pdf")
    types = [t["type"] for t in todos]
    assert "補正" in types


def test_補正_pattern2():
    todos = _extract("20240305 函文（請於文到10日內補正）.pdf")
    assert any(t["type"] == "補正" for t in todos)


def test_補正_中文數字產生全天待辦():
    todos = _extract("20260326 花蓮地方法院函（宣愛華；請於文到十日內提出資料）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "提出資料"
    assert todos[0]["date"] == "2026-04-07"
    assert todos[0]["time"] == ""


def test_補正_日內後有標點仍建立待辦():
    todos = _extract("20260513 花蓮地方法院115年度訴字第109號民事裁定（張國賢；主文：原告應於本裁定送達後十日內，補正說明本件消極確認之訴的權利保護必要）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "補正"
    assert todos[0]["date"] == "2026-05-25"
    assert todos[0]["time"] == ""


def test_補正_文到後期限仍建立待辦():
    todos = _extract("20260505 臺北地方法院函（王台銘；主旨：請於文到後14日內補正如說明一所示事項）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "補正"
    assert todos[0]["date"] == "2026-05-19"
    assert todos[0]["time"] == ""


def test_原版osc簡單關鍵字搭配文到期限仍建立待辦():
    todos = extract_todos_from_filename(
        "20260501 函（王大明；請於文到10日內補提資料）.pdf",
        patterns={"補正": [{"pattern": "補提", "pattern_type": "relative", "days": None}]},
    )
    assert len(todos) == 1
    assert todos[0]["type"] == "補正"
    assert todos[0]["date"] == "2026-05-11"
    assert todos[0]["time"] == ""


def test_原版osc文到十日沒有內字也建立待辦():
    todos = _extract("20260501 函（王大明；請於文到10日補正）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "補正"
    assert todos[0]["date"] == "2026-05-11"
    assert todos[0]["time"] == ""


def test_原版osc文到數字缺日字仍用檔名前綴與假日順延():
    todos = _extract("20260618 花蓮地方法院115年度消債更字第71號函(高弘軒；主旨：請聲請人於文到30內補正，逾期未補正即駁回本件聲請).pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "補正"
    assert todos[0]["date"] == "2026-07-20"
    assert todos[0]["time"] == ""
    assert "06/18文到" in todos[0]["description"]


def test_原版osc自訂假日也會順延(monkeypatch, tmp_path):
    holiday_file = tmp_path / "holidays_config.json"
    holiday_file.write_text(
        '{"2026": {"連假": {"2026-07-20": "臨時假日"}, "臨時假日": {}}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OSC_HOLIDAYS_CONFIG", str(holiday_file))
    todos_mod._load_custom_holidays.cache_clear()
    try:
        todos = _extract("20260618 花蓮地方法院115年度消債更字第71號函(高弘軒；主旨：請聲請人於文到30內補正，逾期未補正即駁回本件聲請).pdf")
        assert len(todos) == 1
        assert todos[0]["type"] == "補正"
        assert todos[0]["date"] == "2026-07-21"
    finally:
        todos_mod._load_custom_holidays.cache_clear()


def test_原版osc民國收文日前綴作為文到基準日():
    assert extract_document_date_from_filename("1150528 函（王大明；請於文到10日補正）.pdf") == datetime(2026, 5, 28)
    todos = _extract("1150528 函（王大明；請於文到10日補正）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "補正"
    assert todos[0]["date"] == "2026-06-08"
    assert "05/28文到" in todos[0]["description"]


def test_windows_style_path_uses_pdf_basename_for_received_date():
    path = r"K:\SynologyDrive\01_案件\一般案件\2025-0030\20250515 花蓮地方法院114年度司補字第228號民事庭通知（謝廷延；主旨：請於本通知送達翌日起7日內補正後列事項）.pdf"

    assert extract_document_date_from_filename(path, path) == datetime(2025, 5, 15)
    todos = extract_todos_from_filename(path, path)

    assert len(todos) == 1
    assert todos[0]["type"] == "補正"
    assert todos[0]["date"] == "2025-05-22"
    assert "05/15文到" in todos[0]["description"]


def test_週內期限轉為全天待辦():
    todos = _extract("20260225 花蓮地方法院114年度訴字第83號函（主旨：請於文到2週內陳報有無調解意願到院）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "陳報"
    assert todos[0]["date"] == "2026-03-11"
    assert todos[0]["time"] == ""


def test_絕對日期前表示意見轉為待辦():
    todos = _extract("20250415 花蓮地方法院函（游秀鈴；請惠予於114年4月21日前，就聲請訴訟參與一事表示意見）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "陳報"
    assert todos[0]["date"] == "2025-04-21"
    assert todos[0]["time"] == ""


def test_絕對日期前陳報轉為待辦():
    todos = _extract("20260331 花蓮地方法院函（王小明；請於115年4月8日前陳報相關資料）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "陳報"
    assert todos[0]["date"] == "2026-04-08"
    assert todos[0]["time"] == ""


def test_純檢送狀紙不因資料關鍵字建立提出資料待辦():
    todos = _extract("20250821 高等法院114年度聲再字第157號刑事庭函（蕭仁俊；主旨：檢送刑事再審抗告理由續狀）.pdf")
    assert todos == []


def test_法院裁定主文無期限不建立確認或提出資料待辦():
    todos = _extract("20260602 花蓮地方法院115年度聲字第169號、115年度聲字第173號刑事裁定（劉信義；主文：准許聲請人A女之父、A女之母參與本案訴訟）.pdf")
    assert todos == []


# ── 上訴 ──
def test_上訴_pattern1():
    todos = _extract("20240305 判決（王大明；如不服本判決得於20日內提起上訴）.pdf")
    assert any(t["type"] == "上訴" for t in todos)


def test_上訴_pattern2():
    todos = _extract("20240305 判決（應於判決送達後14日內提起上訴）.pdf")
    assert any(t["type"] == "上訴" for t in todos)


# ── 陳述意見 ──
def test_陳述意見_pattern1():
    todos = _extract("20240305 函文（應於文到20日內陳述意見）.pdf")
    assert any(t["type"] == "陳述意見" for t in todos)


def test_陳述意見_pattern2():
    todos = _extract("20240305 函文（限於14日內陳述意見）.pdf")
    assert any(t["type"] == "陳述意見" for t in todos)


# ── 繳費 ──
def test_繳費_pattern1():
    todos = _extract("20241015 函文（應於文到30日內繳納規費）.pdf")
    assert any(t["type"] == "繳費" for t in todos)


def test_繳費_pattern2():
    todos = _extract("20241015 函文（限10日內繳納裁判費）.pdf")
    assert any(t["type"] == "繳費" for t in todos)


# ── 閱卷期限 ──
def test_閱卷期限_pattern1():
    todos = _extract("20241015 函文（應於20日內閱卷）.pdf")
    assert any(t["type"] == "閱卷期限" for t in todos)


def test_閱卷期限_pattern2():
    todos = _extract("20241015 函文（閱卷期限7日）.pdf")
    assert any(t["type"] == "閱卷期限" for t in todos)


def test_開庭民國年期日():
    todos = _extract("20260422 花蓮地方法院114年度花補字第502號花蓮簡易庭通知書（謝廷延；訂115年7月1日下午2時30分）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "開庭"
    assert todos[0]["date"] == "2026-07-01"
    assert todos[0]["time"] == "14:30"


def test_開庭民國年期日支援早上用語():
    todos = _extract("20260514 臺東地方檢察署115年度偵字第9號開庭通知（陳建華；訂115年5月27日早上10時40分開庭）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "開庭"
    assert todos[0]["date"] == "2026-05-27"
    assert todos[0]["time"] == "10:40"


def test_開庭民國年期日保留程序類型():
    todos = _extract("20260316 臺北地方法院114年度訴字第972號刑事庭通知書（游秀鈴；訂115年4月1日下午2時30分審理）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "審理"
    assert todos[0]["date"] == "2026-04-01"
    assert todos[0]["time"] == "14:30"


def test_開庭民國年期日可從尾段辨識調解程序():
    todos = _extract("20260513 花蓮地方法院115年度司消債調字第73號民事庭通知書（高弘軒；訂6月1日下午4時行調解程序）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "調解"
    assert todos[0]["date"] == "2026-06-01"
    assert todos[0]["time"] == "16:00"


def test_開庭民國年期日可辨識協商程序():
    todos = _extract("20260520 花蓮地方法院通知書（王大明；訂115年6月2日上午9時協商程序）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "協商程序"
    assert todos[0]["date"] == "2026-06-02"
    assert todos[0]["time"] == "09:00"


def test_開庭無年份期日使用收文年份而非案號年度():
    todos = _extract("20250211 花蓮地院113年度原易字第179號刑事庭通知書（余秋菊；訂3月4日下午3時整審理）.pdf")
    assert len(todos) == 1
    assert todos[0]["date"] == "2025-03-04"
    assert todos[0]["time"] == "15:00"


def test_開庭無收文日前綴時使用案號年度判斷年份():
    todos = _extract("花蓮地方法院115年度司消債調字第69號民事庭通知書（曾昌義；訂6月1日下午3時50分調解）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "調解"
    assert todos[0]["date"] == "2026-06-01"
    assert todos[0]["time"] == "15:50"


def test_extract_base_year_uses_case_folder_hint_without收文日期():
    path = "/tmp/01_案件/法扶案件/消費者債務清理/2025-0049-林洋宇-消費者債務清理-更生/09_法院通知與程序裁定/法院通知書（林洋宇；訂6月17日下午2時調解）.pdf"

    assert (
        extract_base_year_from_filename(
            "法院通知書（林洋宇；訂6月17日下午2時調解）.pdf",
            path,
        )
        == 2025
    )

    todos = _extract("法院通知書（林洋宇；訂6月17日下午2時調解）.pdf", path)
    assert len(todos) == 1
    assert todos[0]["type"] == "調解"
    assert todos[0]["date"] == "2025-06-17"
    assert todos[0]["time"] == "14:00"


def test_開庭無年份期日可跨隔年():
    todos = _extract("20251220 花蓮地院114年度原易字第179號刑事庭通知書（余秋菊；訂1月8日上午10時審理）.pdf")
    assert len(todos) == 1
    assert todos[0]["date"] == "2026-01-08"
    assert todos[0]["time"] == "10:00"


def test_同一法院通知可同時產生庭期與全天期限():
    todos = _extract("20241202 高雄地院通知書（武氏金仙；訂114年1月21日下午3時40分進行準備程序、送達翌日起14日內，提出原證5光碟影像之截圖到院）.pdf")
    assert [(t["type"], t["date"], t["time"]) for t in todos] == [
        ("提出資料", "2024-12-16", ""),
        ("準備程序", "2025-01-21", "15:40"),
    ]


def test_同日上下午庭期都要建立():
    todos = _extract("20251030 臺北地方法院114年度訴字第972號刑事庭通知書（游秀鈴；訂11月11日上午9時50分、11月11日下午2時50分進行審理程序）.pdf")
    assert sorted((t["type"], t["date"], t["time"]) for t in todos) == [
        ("審理", "2025-11-11", "09:50"),
        ("審理", "2025-11-11", "14:50"),
    ]


def test_explicit_roc_hearing_date_is_not_reparsed_as_yearless_next_year():
    todos = _extract("20250814 臺北地方法院114年度消債清字第84號民事裁定(王台銘；主文：債務人王台銘自民國114年8月12日下午4時起開始清算程序。命司法事務官進行本件清算程序）.pdf")

    assert [(t["type"], t["date"], t["time"]) for t in todos] == [("開庭", "2025-08-12", "16:00")]


def test_多日共用同一時間都要建立():
    todos = _extract("20251126 臺北地方法院114年度訴字第972號刑事庭通知書（游秀鈴；訂12月17日、12月18日上午9時30調解）.pdf")
    assert sorted((t["type"], t["date"], t["time"]) for t in todos) == [
        ("調解", "2025-12-17", "09:30"),
        ("調解", "2025-12-18", "09:30"),
    ]


def test_原版osc固定判決規則只在判決書資料夾產生上訴期限():
    todos = extract_todos_from_filename(
        "20260501 花蓮地方法院判決（王大明）.pdf",
        "/tmp/case/10_判決書或終局裁定及處分/20260501 花蓮地方法院判決（王大明）.pdf",
    )
    assert [(t["type"], t["date"], t["time"]) for t in todos] == [("上訴", "2026-05-21", "")]

    legacy_todos = extract_todos_from_filename(
        "20260501 花蓮地方法院判決（王大明）.pdf",
        "/tmp/case/10_判決書/20260501 花蓮地方法院判決（王大明）.pdf",
    )
    assert [(t["type"], t["date"], t["time"]) for t in legacy_todos] == [("上訴", "2026-05-21", "")]


def test_原版osc固定裁定規則非判決書資料夾不產生抗告期限():
    todos = extract_todos_from_filename(
        "20260501 花蓮地方法院裁定（王大明）.pdf",
        "/tmp/case/09_法院通知或程序裁定/20260501 花蓮地方法院裁定（王大明）.pdf",
    )
    assert todos == []


def test_原版osc固定消債字號需搭配裁定才產生異議期限():
    todos = extract_todos_from_filename(
        "20260501 花蓮地方法院115年度司消債更字第1號裁定（王大明）.pdf",
        "/tmp/case/10_判決書或終局裁定及處分/20260501 花蓮地方法院115年度司消債更字第1號裁定（王大明）.pdf",
    )
    assert [(t["type"], t["date"], t["time"]) for t in todos] == [("異議", "2026-05-11", "")]

    no_ruling = extract_todos_from_filename(
        "20260501 花蓮地方法院115年度司消債更字第1號通知（王大明）.pdf",
        "/tmp/case/10_判決書或終局裁定及處分/20260501 花蓮地方法院115年度司消債更字第1號通知（王大明）.pdf",
    )
    assert no_ruling == []


def test_原版osc不得抗告排除固定裁定期限():
    todos = extract_todos_from_filename(
        "20260501 花蓮地方法院裁定（王大明；不得抗告）.pdf",
        "/tmp/case/10_判決書或終局裁定及處分/20260501 花蓮地方法院裁定（王大明；不得抗告）.pdf",
    )
    assert todos == []


# ── deadline_type field ──
def test_deadline_type_in_result():
    todos = _extract("20240305 裁定（應於10日內補正）.pdf")
    for t in todos:
        if t["type"] == "補正":
            assert t.get("deadline_type") == "補正"
            break


# ── _extract_todo_from_filename helper ──
def test_bracket_extraction_補正():
    r = _extract_todo_from_filename("20241015 裁定（王大明；10日內補正）.pdf")
    assert r is not None
    assert r["deadline_type"] == "補正"
    assert r["days"] == 10


def test_bracket_extraction_上訴():
    r = _extract_todo_from_filename("20241015 判決（王大明；20日內上訴）.pdf")
    assert r is not None
    assert r["deadline_type"] == "上訴"


def test_bracket_extraction_週內陳報():
    r = _extract_todo_from_filename("20241015 函（王大明；2週內陳報）.pdf")
    assert r is not None
    assert r["deadline_type"] == "陳報"
    assert r["days"] == 14


def test_bracket_extraction_none():
    r = _extract_todo_from_filename("20241015 委任狀（王大明）.pdf")
    assert r is None
