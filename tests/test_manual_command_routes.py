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
