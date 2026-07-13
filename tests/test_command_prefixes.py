from api.routing.command_prefixes import split_heavy_prefix, strip_heavy_prefix


def test_split_heavy_prefix_accepts_fullwidth_and_no_space_variants():
    cases = [
        ("@HEAVY 請分析", "請分析"),
        ("＠HEAVY請分析", "請分析"),
        ("@重型：請分析", "請分析"),
        ("＠重型　請分析", "請分析"),
        (" @heavy\n請分析", "請分析"),
        ("@HEAVY @MAGI 請分析", "@MAGI 請分析"),
        ("@MAGI @HEAVY 請分析", "@MAGI 請分析"),
        ("＠MAGI 重型：請分析", "@MAGI 請分析"),
        ("@MAGI heavy 請分析", "@MAGI 請分析"),
    ]
    for raw, expected in cases:
        has_prefix, cleaned = split_heavy_prefix(raw)
        assert has_prefix is True
        assert cleaned == expected


def test_split_heavy_prefix_does_not_match_unrelated_words():
    has_prefix, cleaned = split_heavy_prefix("@heavyweight report")
    assert has_prefix is False
    assert cleaned == "@heavyweight report"
    assert strip_heavy_prefix("普通聊天") == "普通聊天"
