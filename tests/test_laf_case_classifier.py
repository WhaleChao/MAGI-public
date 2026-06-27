from api.laf_case_classifier import clean_laf_case_reason, normalize_laf_case_fields


def test_clean_laf_case_reason_strips_suspected_prefix_only_at_start():
    assert clean_laf_case_reason("涉洗錢防制法、詐欺") == "洗錢防制法、詐欺"
    assert clean_laf_case_reason("涉嫌洗錢防制法、詐欺") == "洗錢防制法、詐欺"
    assert clean_laf_case_reason("涉及洗錢防制法、詐欺") == "洗錢防制法、詐欺"
    assert clean_laf_case_reason("洗錢防制法所涉詐欺") == "洗錢防制法所涉詐欺"
    assert clean_laf_case_reason("涉外民事法律適用法") == "涉外民事法律適用法"


def test_normalize_laf_case_fields_removes_suspected_prefix_before_storage():
    assert normalize_laf_case_fields("刑事", "一審", "涉詐欺、洗錢防制法", "") == (
        "刑事",
        "一審",
        "詐欺、洗錢防制法",
    )
