"""Tests for Orchestrator audio transcription post-processing pipeline."""

from unittest.mock import MagicMock, patch


def _make_orchestrator():
    with patch("api.orchestrator.ThreadPoolExecutor"), \
         patch("api.orchestrator.switch_brain_mode"), \
         patch("api.orchestrator.get_brain_status"):
        from api.orchestrator import Orchestrator

        orc = object.__new__(Orchestrator)
        orc._history = {}
        orc._profile_facts = {}
        orc._callbacks = []
        orc._bg_task_pool = MagicMock()
        orc._route_traces = {}
        import threading
        orc._heavy_task_lock = threading.Lock()
        orc._heavy_tasks = {}
        orc._progress_callback = None
        orc._detect_summary_length = lambda prompt: "medium"
        orc._detect_summary_target_pref = lambda prompt_lower: "source"
        orc._is_file_protocol_user = lambda user_id: False
        return orc


@patch("skills.bridge.openclaw_codex_bridge.feature_enabled", return_value=False)
@patch("skills.bridge.balthasar_bridge.transcribe")
def test_audio_parallel_branch_uses_resilient_wrappers(mock_transcribe, mock_feature_enabled):
    mock_transcribe.return_value = {
        "text": "This is a long enough transcript for testing the audio pipeline behavior." * 4,
        "segments": [],
    }

    orc = _make_orchestrator()
    orc._translate_text_complete = MagicMock(return_value={
        "success": True,
        "text": "這是翻譯後的逐字稿。",
    })
    orc._summarize_text_resilient = MagicMock(return_value={
        "success": True,
        "text": "1. 逐字稿重點摘要",
    })

    result = orc._handle_multimedia(
        "user-1",
        "translate and summary no txt no timestamp",
        {"type": "audio", "path": "/tmp/fake-audio.wav"},
    )

    assert "逐字稿重點摘要" in result
    assert "這是翻譯後的逐字稿" in result
    orc._translate_text_complete.assert_called_once()
    orc._summarize_text_resilient.assert_called_once()


@patch("skills.bridge.openclaw_codex_bridge.feature_enabled", return_value=False)
@patch("skills.bridge.balthasar_bridge.transcribe")
def test_audio_translated_summary_uses_translated_text(mock_transcribe, mock_feature_enabled):
    mock_transcribe.return_value = {
        "text": "This is another long transcript for testing translated summary flow." * 4,
        "segments": [],
    }

    orc = _make_orchestrator()
    orc._detect_summary_target_pref = lambda prompt_lower: "translated"
    orc._translate_text_complete = MagicMock(return_value={
        "success": True,
        "text": "這是完整翻譯稿。",
    })
    orc._summarize_text_resilient = MagicMock(return_value={
        "success": True,
        "text": "1. 翻譯結果摘要",
    })

    result = orc._handle_multimedia(
        "user-2",
        "請翻譯後摘要，不要txt 不要時間戳",
        {"type": "audio", "path": "/tmp/fake-audio.wav"},
    )

    assert "翻譯結果摘要" in result
    assert "完整翻譯稿" in result
    orc._translate_text_complete.assert_called_once()
    summary_call = orc._summarize_text_resilient.call_args
    assert summary_call.args[0] == "這是完整翻譯稿。"
