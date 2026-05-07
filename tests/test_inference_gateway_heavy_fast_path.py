"""測試 _chat_inner 的 @heavy fast path（P1-2 修復，2026-04-19）

Bug 背景：
- oMLX 在高負載時會吐「忙碌中」類文字但 `success=True`，導致 _chat_inner 早於 NIM block
  就 return，NIM 永遠接不到手。
- 修法：在 _chat_inner 最前面加 heavy fast path — heavy_opt_in=True 時直接走 NIM，
  跳過 oMLX；NIM 失敗才退回 oMLX；nim_already_tried guard 防止重打第二次。
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from skills.bridge.inference_gateway import InferenceGateway


def _proper_result(success: bool, route: str, **kw) -> dict:
    """產生符合 InferenceGateway._result() 結構的 dict。"""
    base = {
        "success": success,
        "route": route,
        "degraded": not success,
        "response": kw.get("response", ""),
        "analysis": kw.get("analysis", ""),
        "summary": kw.get("summary", ""),
        "text": kw.get("response", "") or kw.get("analysis", ""),
        "error": kw.get("error", ""),
        "model": kw.get("model", ""),
        "task_type": kw.get("task_type", "general"),
    }
    return base


class TestHeavyFastPath:
    """@heavy 觸發 NIM fast path 的三個核心案例。"""

    def setup_method(self):
        # 確保 feature flag 開啟（測試環境）
        self._orig = os.environ.get("NVIDIA_NIM_ENABLE")
        os.environ["NVIDIA_NIM_ENABLE"] = "1"

    def teardown_method(self):
        if self._orig is None:
            os.environ.pop("NVIDIA_NIM_ENABLE", None)
        else:
            os.environ["NVIDIA_NIM_ENABLE"] = self._orig

    def test_heavy_true_triggers_nim_fast_path(self):
        """heavy=True → NIM fast path 直接觸發，跳過 oMLX。"""
        nim_ok = {
            "success": True,
            "response": "依民法第 184 條前段…",
            "model": "meta/llama-3.1-405b-instruct",
            "pii_scrubbed": False,
            "pii_counts": {},
        }

        with patch("skills.bridge.nim_heavy.run_nim_chat", return_value=nim_ok) as mock_nim, \
             patch.object(InferenceGateway, "_omlx_chat") as mock_omlx:
            gw = InferenceGateway()
            r = gw.chat(prompt="test", task_type="legal_analysis", timeout=30, heavy=True)

        assert r["route"] == "nvidia_nim"
        assert r["heavy_fast_path"] is True
        assert r["provider"] == "nvidia_nim"
        assert r["degraded"] is True  # NIM 雲端路徑固定 degraded=True
        assert mock_nim.called
        # ⚠️ 關鍵：oMLX 不該被呼叫（fast path 跳過它）
        assert not mock_omlx.called, "oMLX should be skipped on heavy fast path"

    def test_heavy_false_does_not_trigger_nim(self):
        """heavy=False（預設）→ 走 oMLX 主路徑，NIM 不被呼叫。"""
        omlx_ok = _proper_result(True, "omlx", response="local answer")
        nim_dummy = {"success": True, "response": "should not be called"}

        with patch("skills.bridge.nim_heavy.run_nim_chat", return_value=nim_dummy) as mock_nim, \
             patch.object(InferenceGateway, "_omlx_chat", return_value=omlx_ok):
            gw = InferenceGateway()
            r = gw.chat(prompt="test", task_type="general", timeout=30)

        assert r["route"] == "omlx"
        assert not mock_nim.called, "NIM must not be called when heavy opt-in is absent"

    def test_nim_failure_does_not_retry_second_time(self):
        """heavy=True + NIM 失敗 → nim_already_tried guard 防止後面的 NIM block 再打一次。"""
        nim_fail = {"success": False, "error": "rate_limit_429", "response": ""}
        call_count = [0]

        def counting_nim(**kw):
            call_count[0] += 1
            return nim_fail

        omlx_fail = _proper_result(False, "omlx", error="busy")
        local_fail = _proper_result(False, "local_ollama", error="down")

        with patch("skills.bridge.nim_heavy.run_nim_chat", side_effect=counting_nim), \
             patch.object(InferenceGateway, "_omlx_chat", return_value=omlx_fail), \
             patch.object(InferenceGateway, "_local_chat", return_value=local_fail):
            gw = InferenceGateway()
            gw.chat(
                prompt="test",
                task_type="legal_analysis",
                timeout=30,
                heavy=True,
                allow_synthetic_fallback=False,
            )

        assert call_count[0] == 1, (
            f"NIM called {call_count[0]} times; nim_already_tried guard failed "
            "(would double-burn daily budget)"
        )

    def test_heavy_fast_path_disabled_when_nim_env_off(self):
        """NVIDIA_NIM_ENABLE=0 時，即使 heavy=True 也不觸發 fast path。"""
        os.environ["NVIDIA_NIM_ENABLE"] = "0"
        omlx_ok = _proper_result(True, "omlx", response="local")

        with patch("skills.bridge.nim_heavy.run_nim_chat") as mock_nim, \
             patch.object(InferenceGateway, "_omlx_chat", return_value=omlx_ok):
            gw = InferenceGateway()
            r = gw.chat(prompt="test", task_type="legal_analysis", timeout=30, heavy=True)

        assert r["route"] == "omlx"
        assert not mock_nim.called, "NIM should be gated by NVIDIA_NIM_ENABLE=1"

    def test_heavy_fast_path_skipped_for_tc_review(self):
        """tc_review / captcha 任務不走 NIM fast path（保留本地路徑）。"""
        with patch("skills.bridge.nim_heavy.run_nim_chat") as mock_nim, \
             patch.object(InferenceGateway, "_omlx_chat", return_value=_proper_result(True, "omlx")):
            gw = InferenceGateway()
            gw.chat(prompt="校正這段", task_type="tc_review", timeout=30, heavy=True)

        assert not mock_nim.called, "NIM must skip tc_review"

    def test_heavy_prompt_prefix_detection(self):
        """終極防線：prompt 以 @heavy 開頭時自動偵測並剝除（跨 ThreadPoolExecutor 子 thread）。"""
        nim_ok = {
            "success": True,
            "response": "依民法第 184 條…",
            "model": "meta/llama-3.1-405b-instruct",
            "pii_scrubbed": False,
            "pii_counts": {},
        }

        captured_prompt = []

        def capture_nim(**kw):
            captured_prompt.append(kw.get("prompt"))
            return nim_ok

        with patch("skills.bridge.nim_heavy.run_nim_chat", side_effect=capture_nim) as mock_nim, \
             patch.object(InferenceGateway, "_omlx_chat") as mock_omlx:
            gw = InferenceGateway()
            # 不傳 heavy=True kwarg；依靠 prompt prefix 觸發
            r = gw.chat(prompt="@heavy 請解釋民法第 184 條", task_type="legal_analysis", timeout=30)

        assert r["route"] == "nvidia_nim", "prompt prefix detection should trigger NIM"
        assert r.get("heavy_fast_path") is True
        assert mock_nim.called
        assert not mock_omlx.called, "oMLX should be skipped"
        # prompt 前綴應該被剝除不傳給 NIM
        assert captured_prompt[0] == "請解釋民法第 184 條", (
            f"@heavy prefix should be stripped, got: {captured_prompt[0]!r}"
        )

    def test_heavy_prompt_prefix_chinese_variant(self):
        """@重型 前綴同樣可觸發。"""
        nim_ok = {
            "success": True,
            "response": "ok",
            "model": "meta/llama-3.1-405b-instruct",
            "pii_scrubbed": False,
            "pii_counts": {},
        }
        with patch("skills.bridge.nim_heavy.run_nim_chat", return_value=nim_ok) as mock_nim:
            gw = InferenceGateway()
            r = gw.chat(prompt="@重型 請重型回答", task_type="legal_analysis", timeout=30)
        assert r["route"] == "nvidia_nim"
        assert mock_nim.called
