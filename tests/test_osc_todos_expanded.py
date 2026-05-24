# -*- coding: utf-8 -*-
"""Tests for expanded OSC _TODO_PATTERNS covering 5 deadline categories."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "osc-orchestrator"))

from osc_headless.todos import extract_todos_from_filename, _extract_todo_from_filename


def _extract(filename):
    todos = extract_todos_from_filename(filename)
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


def test_週內期限轉為全天待辦():
    todos = _extract("20260225 花蓮地方法院114年度訴字第83號函（主旨：請於文到2週內陳報有無調解意願到院）.pdf")
    assert len(todos) == 1
    assert todos[0]["type"] == "陳報"
    assert todos[0]["date"] == "2026-03-11"
    assert todos[0]["time"] == ""


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


def test_開庭無年份期日使用收文年份而非案號年度():
    todos = _extract("20250211 花蓮地院113年度原易字第179號刑事庭通知書（余秋菊；訂3月4日下午3時整審理）.pdf")
    assert len(todos) == 1
    assert todos[0]["date"] == "2025-03-04"
    assert todos[0]["time"] == "15:00"


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


def test_多日共用同一時間都要建立():
    todos = _extract("20251126 臺北地方法院114年度訴字第972號刑事庭通知書（游秀鈴；訂12月17日、12月18日上午9時30調解）.pdf")
    assert sorted((t["type"], t["date"], t["time"]) for t in todos) == [
        ("調解", "2025-12-17", "09:30"),
        ("調解", "2025-12-18", "09:30"),
    ]


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
