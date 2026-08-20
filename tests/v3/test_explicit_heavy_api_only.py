from __future__ import annotations

from unittest.mock import patch

from skills.bridge.inference_gateway import InferenceGateway
from skills.bridge import nim_heavy
from skills.bridge import ensemble_inference
from skills.engine.react_engine import ReActEngine


class _FakeResponse:
    status_code = 200
    text = '{"ok":true}'

    def json(self):
        return {"choices": [{"message": {"content": "完成"}}]}


def test_explicit_heavy_never_falls_back_to_local(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.setenv("MAGI_HEAVY_STRICT_NIM", "0")
    gateway = InferenceGateway()

    with patch.object(
        nim_heavy,
        "run_nim_chat",
        return_value={"success": False, "error": "forced", "response": ""},
    ), patch.object(gateway, "_local_chat") as local_chat:
        result = gateway.chat(
            "@heavy 測試",
            task_type="legal_analysis",
            allow_synthetic_fallback=False,
        )

    assert result["success"] is False
    assert result["route"] == "nvidia_nim_heavy_failed"
    local_chat.assert_not_called()


def test_explicit_heavy_fails_closed_when_nim_disabled(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "0")
    gateway = InferenceGateway()

    with patch.object(gateway, "_local_chat") as local_chat:
        result = gateway.chat(
            "@重型 測試",
            task_type="legal_analysis",
            allow_synthetic_fallback=False,
        )

    assert result["success"] is False
    assert result["route"] == "nvidia_nim_required"
    local_chat.assert_not_called()


def test_explicit_heavy_daily_budget_exhaustion_stops_without_retry(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.setenv("MAGI_HEAVY_STRICT_NIM", "1")
    monkeypatch.setenv("MAGI_HEAVY_STRICT_NIM_RETRIES", "6")
    gateway = InferenceGateway()

    with patch.object(
        nim_heavy,
        "run_nim_chat",
        return_value={
            "success": False,
            "error": "nim_daily_budget_exceeded:500/500",
            "response": "",
        },
    ) as nim_chat, patch("time.sleep") as sleep, patch.object(gateway, "_local_chat") as local_chat:
        result = gateway.chat(
            "@heavy 測試",
            task_type="legal_analysis",
            allow_synthetic_fallback=False,
        )

    assert result["success"] is False
    assert result["route"] == "nvidia_nim_strict_failed"
    assert "budget_exceeded" in result["error"]
    nim_chat.assert_called_once()
    sleep.assert_not_called()
    local_chat.assert_not_called()


def test_explicit_heavy_authorizes_verbatim_personal_data_for_this_call(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    gateway = InferenceGateway()
    observed = {}

    def fake_nim(**kwargs):
        observed.update(kwargs)
        return {"success": True, "response": "完成", "model": "nim"}

    with patch.object(nim_heavy, "run_nim_chat", side_effect=fake_nim):
        result = gateway.chat("＠HEAVY 翻譯王大明（案號 115 年度訴字第 1 號）", task_type="translate")

    assert result["success"] is True
    assert observed["user_heavy_authorized"] is True
    assert "王大明" in observed["prompt"]


def test_unmarked_follow_up_never_inherits_heavy_authorization(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    gateway = InferenceGateway()
    with patch.object(nim_heavy, "run_nim_chat") as nim_chat, patch.object(
        gateway, "_omlx_chat", return_value={"success": True, "response": "本機回覆"}
    ):
        result = gateway.chat("請繼續翻譯", task_type="translate")

    assert result["success"] is True
    nim_chat.assert_not_called()


def test_explicit_heavy_blocks_credentials_before_network(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "test-key")
    with patch.object(nim_heavy.requests, "post") as post:
        result = nim_heavy.run_nim_chat(
            prompt="@heavy 請整理 API_KEY=super-secret-value-123456",
            heavy=True,
            user_heavy_authorized=True,
        )

    assert result["success"] is False
    assert result["error"] == "credential_blocked"
    post.assert_not_called()


def test_heavy_ensemble_preserves_current_message_pii_and_never_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.setenv("MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK", "1")
    observed = {}

    def fake_nim(**kwargs):
        observed.update(kwargs)
        return {"success": False, "error": "forced", "response": ""}

    monkeypatch.setattr(nim_heavy, "run_nim_chat", fake_nim)
    with patch.object(ensemble_inference, "_call_omlx_chat") as local_chat:
        result = ensemble_inference.ensemble_chat_verified(
            prompt="翻譯王大明的資料",
            task_type="translate",
            heavy=True,
        )

    assert result.result is None
    assert observed["user_heavy_authorized"] is True
    assert observed["prompt"] == "翻譯王大明的資料"
    local_chat.assert_not_called()


def test_heavy_react_preserves_current_message_pii_and_never_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK", "1")
    observed = {}

    def fake_nim(**kwargs):
        observed.update(kwargs)
        return {"success": False, "error": "forced", "response": ""}

    monkeypatch.setattr(nim_heavy, "run_nim_chat", fake_nim)
    with patch("skills.bridge.ensemble_inference._call_omlx_chat_multiturn") as local_chat:
        engine = ReActEngine.for_omlx(tools={}, user_query="王大明", heavy=True)
        answer = engine._llm([{"role": "user", "content": "查王大明的案件"}])

    assert answer == "LLM ERROR: nvidia_nim:forced"
    assert observed["user_heavy_authorized"] is True
    assert "王大明" in observed["prompt"]
    local_chat.assert_not_called()
