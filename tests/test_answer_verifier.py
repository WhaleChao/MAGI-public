"""Regression tests for answer verification and false-memory blocking."""

from api.verification import verify_answer


def test_verify_answer_blocks_false_memory_claim_without_support():
    result = verify_answer(
        query="那篇文章是什麼？",
        answer="你之前給過我一篇文章，我現在可以直接幫你整理。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="無。",
        conversation_history="",
    )

    assert result.passed is False
    assert result.reason == "false_memory_claim_without_support"
    assert "可驗證證據" in result.safe_reply


def test_verify_answer_allows_false_memory_style_phrase_when_chatlog_exists():
    result = verify_answer(
        query="你還記得我之前提過什麼嗎？",
        answer="你之前說過想整理一份文章清單。",
        memories=[
            {
                "content": "我想整理一份文章清單",
                "source": "chatlog|platform=Discord|role=user|conf=0.82",
            }
        ],
        memory_context="有相關記憶。",
        web_context="無。",
        conversation_history="",
    )

    assert result.passed is True
    assert result.reason == "verified"


def test_verify_answer_blocks_overclaim_without_evidence():
    result = verify_answer(
        query="最新狀況如何？",
        answer="我可以確定目前已經完全修復。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="無。",
        conversation_history="",
    )

    assert result.passed is False
    assert result.reason == "overclaim_without_evidence"
    assert "不應該把推測說成確定事實" in result.safe_reply


def test_verify_answer_allows_overclaim_when_web_evidence_exists():
    result = verify_answer(
        query="最新狀況如何？",
        answer="我可以確定目前官方公告已上線。",
        memories=[],
        memory_context="無相關記憶。",
        web_context="來源 A：官方公告已上線",
        conversation_history="",
    )

    assert result.passed is True
