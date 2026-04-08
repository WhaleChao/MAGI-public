# -*- coding: utf-8 -*-
"""Tests for skills/ops/spotlight_search.py — Spotlight/mdfind integration."""

import os
import subprocess
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest


# ── Module import ──
from skills.ops.spotlight_search import (
    is_exact_query,
    normalize_case_number,
    spotlight_search,
    spotlight_search_case,
    spotlight_search_person,
    check_spotlight_indexed,
)


# ── is_exact_query tests ──

class TestIsExactQuery:
    def test_full_case_number(self):
        assert is_exact_query("113年度勞訴字第19號") is True

    def test_short_case_number(self):
        assert is_exact_query("113勞訴19") is True

    def test_date_format_dash(self):
        assert is_exact_query("2026-04-08") is True

    def test_date_format_roc(self):
        assert is_exact_query("115.04.08") is True

    def test_chinese_name(self):
        assert is_exact_query("黃語玲") is True

    def test_chinese_name_4_chars(self):
        assert is_exact_query("歐陽修明") is True

    def test_general_query(self):
        assert is_exact_query("勞資爭議如何處理") is False

    def test_english_query(self):
        assert is_exact_query("how to appeal") is False

    def test_single_char(self):
        assert is_exact_query("法") is False

    def test_empty(self):
        assert is_exact_query("") is False


# ── normalize_case_number tests ──

class TestNormalizeCaseNumber:
    def test_full_format(self):
        result = normalize_case_number("113年度勞訴字第19號")
        assert '"113年度勞訴字第19號"' in result
        assert '"113勞訴19"' in result
        assert "OR" in result

    def test_short_format(self):
        result = normalize_case_number("113勞訴19")
        assert '"113年度勞訴字第19號"' in result
        assert '"113勞訴19"' in result

    def test_arbitrary_string(self):
        result = normalize_case_number("some random text")
        assert result == '"some random text"'


# ── spotlight_search tests (mocked subprocess) ──

class TestSpotlightSearch:
    @patch("skills.ops.spotlight_search.subprocess.run")
    @patch("skills.ops.spotlight_search.os.stat")
    def test_basic_search(self, mock_stat, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="/path/to/file1.pdf\n/path/to/file2.pdf\n",
        )
        mock_stat.return_value = MagicMock(st_mtime=1712556000, st_size=12345)

        results = spotlight_search("test query")
        assert len(results) == 2
        assert results[0]["name"] == "file1.pdf"
        assert results[0]["path"] == "/path/to/file1.pdf"
        assert results[0]["size"] == 12345

    @patch("skills.ops.spotlight_search.subprocess.run")
    def test_search_with_folder(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        with patch("skills.ops.spotlight_search.os.path.isdir", return_value=True):
            spotlight_search("test", folder="/Volumes/homes")
            cmd = mock_run.call_args[0][0]
            assert "-onlyin" in cmd
            assert "/Volumes/homes" in cmd

    @patch("skills.ops.spotlight_search.subprocess.run")
    def test_search_nonexistent_folder(self, mock_run):
        results = spotlight_search("test", folder="/nonexistent/path")
        assert results == []
        mock_run.assert_not_called()

    @patch("skills.ops.spotlight_search.subprocess.run")
    def test_search_with_file_type(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        spotlight_search("test", file_type="pdf")
        cmd_query = mock_run.call_args[0][0][-1]
        assert "*.pdf" in cmd_query

    @patch("skills.ops.spotlight_search.subprocess.run")
    def test_search_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="mdfind", timeout=10)
        results = spotlight_search("test")
        assert results == []

    @patch("skills.ops.spotlight_search.subprocess.run")
    def test_search_mdfind_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        results = spotlight_search("test")
        assert results == []

    @patch("skills.ops.spotlight_search.subprocess.run")
    def test_search_empty_results(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        results = spotlight_search("nonexistent-keyword-xyz")
        assert results == []

    @patch("skills.ops.spotlight_search.subprocess.run")
    @patch("skills.ops.spotlight_search.os.stat")
    def test_search_limit(self, mock_stat, mock_run):
        paths = "\n".join([f"/path/to/file{i}.pdf" for i in range(50)])
        mock_run.return_value = MagicMock(returncode=0, stdout=paths)
        mock_stat.return_value = MagicMock(st_mtime=1712556000, st_size=100)

        results = spotlight_search("test", limit=5)
        assert len(results) == 5


# ── spotlight_search_case tests ──

class TestSpotlightSearchCase:
    @patch("skills.ops.spotlight_search.spotlight_search")
    def test_case_search_calls_spotlight(self, mock_search):
        mock_search.return_value = []
        spotlight_search_case("113勞訴19", case_folder="/Volumes/homes")
        mock_search.assert_called_once()
        args = mock_search.call_args
        assert "113" in args[0][0]
        assert args[1]["folder"] == "/Volumes/homes"
        assert args[1]["file_type"] == "pdf"


# ── check_spotlight_indexed tests ──

class TestCheckSpotlightIndexed:
    @patch("skills.ops.spotlight_search.subprocess.run")
    def test_indexed(self, mock_run):
        mock_run.return_value = MagicMock(stdout="Indexing enabled.")
        assert check_spotlight_indexed("/Volumes/homes") is True

    @patch("skills.ops.spotlight_search.subprocess.run")
    def test_not_indexed(self, mock_run):
        mock_run.return_value = MagicMock(stdout="Indexing disabled.")
        assert check_spotlight_indexed("/Volumes/homes") is False
