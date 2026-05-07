# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-namer")))

import action as mod


def test_infer_party_from_case_folder_path():
    path = (
        "/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件/一般案件/民事/"
        "2025-0136-聯鋒國際有限公司-一審-支付命令/07_法院通知或程序裁定/"
        "20260320 臺灣士林地方法院115年度司促字第1781號支付命令（聯鋒國際有限公司）.pdf"
    )
    assert mod._infer_party_from_case_folder_path(path) == "聯鋒國際有限公司"


def test_our_statement_subtype_liudi_normalizes_to_cundi_once():
    result = mod._build_name_result(
        found_date="20260403",
        found_type="陳報狀",
        found_party="林洋宇",
        doc_subtype="消費者債務清理更生陳報狀留底",
    )
    assert result["filename"] == "20260403 消費者債務清理更生陳報狀存底（林洋宇）.pdf"


def test_our_statement_existing_cundi_not_duplicated():
    result = mod._build_name_result(
        found_date="20260403",
        found_type="陳報狀",
        found_party="林洋宇",
        doc_subtype="消費者債務清理更生陳報狀存底",
    )
    assert result["filename"] == "20260403 消費者債務清理更生陳報狀存底（林洋宇）.pdf"
