from api.verification import run_tri_agent_verification


def test_tri_agent_verification_accepts_grounded_answer():
    report = run_tri_agent_verification(
        query="最新狀況如何？",
        draft_answer="目前官方公告已上線。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="來源 A：官方公告已上線",
        conversation_history="",
    )

    assert report.passed is True
    assert report.final_answer == "目前官方公告已上線。"
    assert report.metadata["agent_count"] == 3


def test_tri_agent_verification_falls_back_to_safe_reply_without_generator():
    report = run_tri_agent_verification(
        query="那篇文章是什麼？",
        draft_answer="你之前給過我一篇文章，我現在可以直接幫你整理。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="無。",
        conversation_history="",
    )

    assert report.passed is False
    assert "可驗證證據" in report.final_answer


def test_tri_agent_verification_uses_repair_generator_when_available():
    report = run_tri_agent_verification(
        query="那篇文章是什麼？",
        draft_answer="你之前給過我一篇文章，我現在可以直接幫你整理。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="無。",
        conversation_history="",
        generate=lambda _prompt: "我目前沒有可驗證證據能證明你之前提供過那份文章。",
    )

    assert report.revision_count == 1
    assert "可驗證證據" in report.final_answer
