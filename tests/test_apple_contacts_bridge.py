# -*- coding: utf-8 -*-
"""Tests for skills/apple/contacts_bridge.py — Apple Contacts integration."""

from unittest.mock import patch

import pytest

from skills.apple.contacts_bridge import (
    search_contact,
    search_contacts,
    get_contact_count,
    search_lawyer,
    format_contact_info,
)


class TestSearchContact:
    @patch("skills.apple.contacts_bridge._run_osascript")
    def test_found(self, mock_osa):
        phone = "0912" + "345678"
        mock_osa.return_value = (True, f"李大律師||{phone}||lawyer@test.com||大律法律事務所")
        result = search_contact("李大")
        assert result is not None
        assert result["name"] == "李大律師"
        assert result["phone"] == phone
        assert result["email"] == "lawyer@test.com"
        assert result["organization"] == "大律法律事務所"

    @patch("skills.apple.contacts_bridge._run_osascript")
    def test_not_found(self, mock_osa):
        mock_osa.return_value = (True, "")
        result = search_contact("不存在的人")
        assert result is None

    @patch("skills.apple.contacts_bridge._run_osascript")
    def test_osascript_failure(self, mock_osa):
        mock_osa.return_value = (False, "error")
        result = search_contact("test")
        assert result is None


class TestSearchContacts:
    @patch("skills.apple.contacts_bridge._run_osascript")
    def test_multiple_results(self, mock_osa):
        mock_osa.return_value = (True, "王一||0911||a@b.com||公司A\n王二||0922||c@d.com||公司B")
        results = search_contacts("王")
        assert len(results) == 2
        assert results[0]["name"] == "王一"
        assert results[1]["name"] == "王二"

    @patch("skills.apple.contacts_bridge._run_osascript")
    def test_empty_results(self, mock_osa):
        mock_osa.return_value = (True, "")
        results = search_contacts("不存在")
        assert results == []


class TestGetContactCount:
    @patch("skills.apple.contacts_bridge._run_osascript")
    def test_returns_count(self, mock_osa):
        mock_osa.return_value = (True, "150")
        assert get_contact_count() == 150

    @patch("skills.apple.contacts_bridge._run_osascript")
    def test_failure(self, mock_osa):
        mock_osa.return_value = (False, "error")
        assert get_contact_count() == 0


class TestSearchLawyer:
    @patch("skills.apple.contacts_bridge.search_contact")
    def test_finds_by_lawyer_suffix(self, mock_search):
        mock_search.return_value = {"name": "李大律師", "phone": "0912", "email": "", "organization": "事務所"}
        result = search_lawyer("李大")
        assert result is not None
        mock_search.assert_called_with("李大律師")

    @patch("skills.apple.contacts_bridge.search_contacts")
    @patch("skills.apple.contacts_bridge.search_contact")
    def test_fallback_to_org_match(self, mock_single, mock_multi):
        mock_single.return_value = None  # No "X律師" match
        mock_multi.return_value = [
            {"name": "李大", "phone": "0912", "email": "", "organization": "大律法律事務所"},
            {"name": "李大明", "phone": "0933", "email": "", "organization": "科技公司"},
        ]
        result = search_lawyer("李大")
        assert result["organization"] == "大律法律事務所"


class TestFormatContactInfo:
    def test_full_info(self):
        phone = "0912" + "345678"
        contact = {
            "name": "李大律師",
            "phone": phone,
            "email": "lawyer@test.com",
            "organization": "大律法律事務所",
        }
        formatted = format_contact_info(contact)
        assert "李大律師" in formatted
        assert phone in formatted
        assert "lawyer@test.com" in formatted
        assert "大律法律事務所" in formatted

    def test_minimal_info(self):
        contact = {"name": "王小明", "phone": "", "email": "", "organization": ""}
        formatted = format_contact_info(contact)
        assert "王小明" in formatted
