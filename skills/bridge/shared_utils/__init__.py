# skills/bridge/shared_utils — 共用文字處理 / 法院 / 案號工具
# 從各 skill 抽取的重複邏輯，統一維護於此。

from skills.bridge.shared_utils.text_utils import (
    normalize_spaces,
    normalize_segment_fragment,
    clean_text,
    strip_zero_width,
    normalize_court_char,
)
from skills.bridge.shared_utils.court_utils import (
    COURT_OPTIONS,
    SIMPLE_COURT_MAPPING,
    normalize_court_name,
    get_court_code,
    extract_court_name,
)
from skills.bridge.shared_utils.case_number_utils import (
    extract_case_number,
    parse_case_number_flexible,
    extract_laf_case_number,
)

__all__ = [
    "normalize_spaces",
    "normalize_segment_fragment",
    "clean_text",
    "strip_zero_width",
    "normalize_court_char",
    "COURT_OPTIONS",
    "SIMPLE_COURT_MAPPING",
    "normalize_court_name",
    "get_court_code",
    "extract_court_name",
    "extract_case_number",
    "parse_case_number_flexible",
    "extract_laf_case_number",
]
