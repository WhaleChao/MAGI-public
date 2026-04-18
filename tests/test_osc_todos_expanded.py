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


def test_bracket_extraction_none():
    r = _extract_todo_from_filename("20241015 委任狀（王大明）.pdf")
    assert r is None
