"""Tests for api.handlers.text_processing_handler."""


def test_sanitize_removes_metadata():
    from api.handlers.text_processing_handler import sanitize_incoming_message
    raw = "Conversation info (untrusted metadata):\n{\"foo\": 1}\n翻譯這篇文章"
    assert "untrusted" not in sanitize_incoming_message(raw)
    assert "翻譯這篇文章" in sanitize_incoming_message(raw)


def test_sanitize_empty():
    from api.handlers.text_processing_handler import sanitize_incoming_message
    assert sanitize_incoming_message("") == ""
    assert sanitize_incoming_message(None) == ""


def test_strip_intent_prefixes():
    from api.handlers.text_processing_handler import strip_intent_prefixes
    assert strip_intent_prefixes("@MAGI 翻譯這段", []) == "翻譯這段"
    assert strip_intent_prefixes("翻譯這段", [r"^翻譯"]) == "這段"


def test_redact_secrets():
    from api.handlers.text_processing_handler import redact_secrets
    assert redact_secrets("") == ""
    long_token = "A" * 80
    assert "[REDACTED_TOKEN]" in redact_secrets(f"token={long_token}")


def test_output_guard_issues_returns_list():
    from api.handlers.text_processing_handler import output_guard_issues
    result = output_guard_issues("正常的回覆文字")
    assert isinstance(result, list)


def test_translation_guard_allows_contact_info_in_source_text():
    from api.handlers.text_processing_handler import output_guard_issues

    translated_appendix = (
        "財團法人臺北市賽珍珠基金會\n"
        "電話：(零二)二三六九-捌八八零\n"
        "電子郵件：hsinchi@example.org\n"
        "地址：臺北市中山區龍江路二百六十四號三樓"
    )

    assert output_guard_issues(translated_appendix, mode="translation") == []
