from api.tools.policies import classify_tool_requirement


def test_illustrated_manual_command_routes():
    cases = [
        ("今天有什麼行程？", "calendar_query"),
        ("列出本週 OSC 建立待辦。", "todo_query"),
        ("查 2026-0001 的案件狀態。", "case_query"),
        ("從這份法院通知建立待辦。", "document_processing"),
        ("@heavy 翻譯這份 PDF，專有名詞後保留原文。", "document_processing"),
        ("檢查這件是否有新閱卷資料。", "file_review_query"),
        ("下載這件的新筆錄。", "transcript_query"),
        ("用最高法院與通譯抓判決並分類。", "judgment_query"),
        ("查 1150421-W-004 法扶狀態。", "laf_query"),
        ("匯入這個月帳務，排除非本人項目。", "accounting_query"),
        ("MAGI 系統狀態。", "system_health"),
        ("跑完整 smoke62 與 commercial readiness。", "system_health"),
    ]
    for prompt, expected_tool in cases:
        req = classify_tool_requirement(prompt)
        assert req.level == "required", prompt
        assert req.tool_hint == expected_tool, prompt


def test_tool_first_gate_respects_chat_cancel_and_correction_boundaries():
    chat = classify_tool_requirement("我只是想跟你聊聊天，不要查資料庫")
    assert chat.level == "none"

    capability = classify_tool_requirement("你可以查天氣嗎？")
    assert capability.level == "none"

    cancel = classify_tool_requirement("取消")
    assert cancel.level == "none"

    correction = classify_tool_requirement("更正：正確是臺灣新北地方法院")
    assert correction.level == "none"

    explicit_tool = classify_tool_requirement("查 2026-0001 的案件狀態。")
    assert explicit_tool.level == "required"
    assert explicit_tool.tool_hint == "case_query"
