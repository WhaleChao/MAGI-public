# -*- coding: utf-8 -*-
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-namer")))

import action as mod


def test_fallback_date_from_filename_prefix():
    date_value, method = mod._fallback_date_from_filename_or_mtime("/tmp/20260306 無償委任證明書.pdf")
    assert date_value == "20260306"
    assert method == "filename_prefix_fallback"


def test_build_name_result_judgment_adds_placeholder_brackets():
    result = mod._build_name_result(
        found_date="20250718",
        found_court="臺灣花蓮地方法院",
        found_case_no="113年度原易字第179號",
        found_type="判決",
        found_party="",
        summary="",
    )
    assert "（待補摘要）" in result["filename"]
