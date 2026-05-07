"""
ReAct 推理引擎 — Reason → Act → Observe → Reason → ... → Final Answer
=======================================================================
讓 MAGI 從「一問一答」升級為「多步推理 + 工具呼叫」。

核心原則：
1. LLM 決定下一步，不是 if/elif
2. 技能是 LLM 的「工具」，由 LLM 決定何時呼叫
3. 有觀察回饋，可中途調整策略
4. 設定最大步數防止無限迴圈
5. Iron Dome 安全檢查

使用：
    from skills.engine.react_engine import ReActEngine
    from skills.engine.tool_registry import get_tools

    engine = ReActEngine(tools=get_tools())
    result = engine.run("幫我查案號 112 年訴字第 123 號的判決摘要")
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("ReActEngine")

MAX_STEPS = int(os.environ.get("MAGI_REACT_MAX_STEPS", "8"))
STEP_TIMEOUT = int(os.environ.get("MAGI_REACT_STEP_TIMEOUT", "60"))
TOTAL_TIMEOUT = int(os.environ.get("MAGI_REACT_TOTAL_TIMEOUT", "180"))
MAX_OBS_CHARS = int(os.environ.get("MAGI_REACT_MAX_OBS_CHARS", "2000"))

REACT_SYSTEM_PROMPT = """你是 CASPER，MAGI 系統的推理引擎。使用繁體中文。

你有以下工具可用：
{tool_list}

回覆格式（嚴格遵守）：

要使用工具時：
ACTION: 工具名稱
PARAMS: {{"參數名": "值"}}

已有答案時：
FINAL: 最終回答

範例 1（使用工具）：
使用者：現在幾點？
ACTION: current_time
PARAMS: {{}}

範例 2（計算）：
使用者：100 乘以 3 加 50 等於多少？
ACTION: calculate
PARAMS: {{"expression": "100*3+50"}}

範例 3（不需工具，直接回答）：
使用者：什麼是正當防衛？
FINAL: 正當防衛是指對於現在不法之侵害，而出於防衛自己或他人權利之行為，不罰。

規則：
- 每次只用一個工具。觀察結果後決定下一步。最多 {max_steps} 步
- 不可刪除檔案或資料
- read_file 只能讀本機檔案路徑（如 /Users/...），不能讀 URL
- 工具選擇必須精準：
  - 問「行程、開庭、會議、排程、今天/明天有沒有事」→ 用 get_schedule，不要用 web_search
  - 問「天氣、新聞、最新、匯率、股價」→ 用 web_search
  - 問「事務所案件、案號、當事人案件狀態」→ 用 query_cases
  - 問「判決、裁判、實務見解」→ 用 search_judgments
  - 問「法條、第幾條、法律條文」→ 用 search_statutes
  - 問「摘要」且已提供文字 → 用 summarize；未提供文字 → FINAL: 請使用者提供文字
  - 問「翻譯」且已提供文字 → 用 translate；未提供文字 → FINAL: 請使用者提供文字
  - 問「記住、記下來、備忘」→ 用 remember
  - 問「讀取本機檔案」且提供安全路徑 → 用 read_file
  - 問白名單技能（例如合約審閱、PDF 頁籤、股市晨報、勞動計算）→ 用 run_skill
- 如果你已經知道答案，直接用 FINAL: 回答，不要多此一舉用工具
- 回覆中不要重複範例的內容
"""

# 安全黑名單 — 這些參數值不可出現在工具呼叫中
_IRON_DOME_BLOCKED = {
    "rm -rf", "drop table", "delete from", "truncate",
    "format", "mkfs", "dd if=", "shutdown", "reboot",
}


class ReActEngine:
    """ReAct 推理引擎。"""

    def __init__(
        self,
        tools: dict[str, dict[str, Any]],
        llm_fn: Optional[Callable] = None,
        max_steps: int = MAX_STEPS,
        total_timeout: int = TOTAL_TIMEOUT,
    ):
        """
        Args:
            tools: {"tool_name": {"fn": callable, "desc": str, "params": str}}
            llm_fn: 呼叫 LLM 的函數 (messages: list[dict]) -> str
                    如果不提供，使用 llm_direct.chat
            max_steps: 最大推理步數
            total_timeout: 整體超時秒數
        """
        self.tools = tools
        self.max_steps = max_steps
        self.total_timeout = total_timeout

        if llm_fn:
            self._llm = llm_fn
        else:
            self._llm = self._default_llm

    def _default_llm(self, messages: list[dict]) -> str:
        """預設 LLM — 使用 llm_direct.chat，feature='react'。"""
        from skills.bridge.llm_direct import chat
        result = chat(
            prompt="",
            feature="react",
            messages=messages,
            timeout=STEP_TIMEOUT,
            max_tokens=512,  # 控制回覆長度，加速推理
        )
        return result.get("text", "") if result.get("success") else f"LLM ERROR: {result.get('error', 'unknown')}"

    def _format_tool_list(self) -> str:
        """格式化工具清單給 LLM 看。"""
        lines = []
        for name, info in self.tools.items():
            desc = info.get("desc", "")
            params = info.get("params", "")
            lines.append(f"- {name}: {desc}")
            if params:
                lines.append(f"  參數: {params}")
        return "\n".join(lines)

    def _build_system_prompt(self, soul_text: str = "") -> str:
        """建構含工具清單的 system prompt。可選 soul 注入。"""
        react_part = REACT_SYSTEM_PROMPT.format(
            tool_list=self._format_tool_list(),
            max_steps=self.max_steps,
        )
        if soul_text:
            return "{}\n\n---\n{}".format(soul_text[:800].strip(), react_part)
        return react_part

    @classmethod
    def for_omlx(cls, tools=None, user_query="", max_steps=5, total_timeout=60, soul_text=""):
        # type: (Optional[dict], str, int, int, str) -> ReActEngine
        """建立走 oMLX E4B 的 ReAct 引擎。

        Args:
            tools: 工具 dict，若為 None 則自動用 get_compact_tools
            user_query: 使用者原文（用於 remember 閘門判斷）
            max_steps: 最大步數（E4B 較慢，預設 5）
            total_timeout: 整體超時秒數（預設 60，含 summarize/translate 等較慢工具）
            soul_text: SOUL 身份文字（注入 system prompt 前段）
        """
        if tools is None:
            from skills.engine.tool_registry import get_compact_tools
            tools = get_compact_tools(user_query)

        def _omlx_llm(messages):
            from skills.bridge.ensemble_inference import (
                _call_omlx_chat_multiturn, OMLX_E4B_BASE,
            )
            result = _call_omlx_chat_multiturn(
                OMLX_E4B_BASE, "e4b", messages,
                timeout_sec=45, max_tokens=512,
            )
            if result.get("success"):
                return result["text"]
            return "LLM ERROR: {}".format(result.get("error", "unknown"))

        engine = cls(
            tools=tools,
            llm_fn=_omlx_llm,
            max_steps=max_steps,
            total_timeout=total_timeout,
        )
        engine._soul_text = soul_text
        return engine

    @staticmethod
    def _extract_balanced_json(text: str) -> Optional[str]:
        """從文字中找出第一個完整的 JSON 物件（支援巢狀結構）。

        使用大括號計數器，正確處理 {"a": {"b": "c"}} 這類巢狀 JSON，
        解決 r'[^}]*' 正則在遇到第一個 } 就停止的問題。
        """
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start: i + 1]
        return None

    @staticmethod
    def _fix_json_lite(raw: str) -> str:
        """修復 LLM 常見的 JSON 格式問題。

        - 單引號 → 雙引號
        - 尾逗號（trailing comma）移除
        不使用 rstrip("}"') 以免破壞巢狀物件。
        """
        fixed = raw.replace("'", '"')
        # 移除物件/陣列末尾的尾逗號
        fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
        return fixed

    def _parse_action(self, response: str) -> Tuple[str, dict]:
        """從 LLM 回應中解析 ACTION + PARAMS。寬容處理各種格式。"""
        tool_name = ""
        params = {}

        # 找 ACTION: line（支援 `ACTION:tool` 或 `ACTION: tool`）
        action_match = re.search(r'ACTION:\s*(\w+)', response)
        if action_match:
            tool_name = action_match.group(1).strip()

        # 找 PARAMS: 後的完整 JSON 物件（支援巢狀、多行）
        params_start = re.search(r'PARAMS:\s*', response)
        if params_start:
            after_params = response[params_start.end():]
            raw = self._extract_balanced_json(after_params)
            if raw and raw != "{}":
                try:
                    params = json.loads(raw)
                except json.JSONDecodeError:
                    # 嘗試修復常見格式問題（單引號、尾逗號）
                    try:
                        params = json.loads(self._fix_json_lite(raw))
                    except json.JSONDecodeError:
                        logger.debug("Non-JSON PARAMS ignored: %s", raw[:100])

        # 如果沒找到 PARAMS 但有 tool_name，嘗試從 response 整體提取第一個 JSON
        if tool_name and not params:
            raw2 = self._extract_balanced_json(response)
            if raw2:
                try:
                    params = json.loads(raw2)
                except json.JSONDecodeError:
                    try:
                        params = json.loads(self._fix_json_lite(raw2))
                    except json.JSONDecodeError:
                        pass

        return tool_name, params

    def _parse_final(self, response: str) -> Optional[str]:
        """從 LLM 回應中解析 FINAL: answer。"""
        final_match = re.search(r'FINAL:\s*(.*)', response, re.DOTALL)
        if final_match:
            return final_match.group(1).strip()
        return None

    def _iron_dome_check(self, tool_name: str, params: dict) -> Optional[str]:
        """Iron Dome 安全檢查。回傳 None 表示安全，否則回傳攔截原因。"""
        param_str = json.dumps(params, ensure_ascii=False).lower()
        for blocked in _IRON_DOME_BLOCKED:
            if blocked in param_str:
                return f"Iron Dome blocked: '{blocked}' detected in parameters"

        # 檢查工具是否存在
        if tool_name not in self.tools:
            return f"Tool '{tool_name}' not found. Available: {list(self.tools.keys())}"

        return None

    def run(
        self,
        user_query: str,
        context: str = "",
        conversation_history: Optional[List[dict]] = None,
    ) -> Dict[str, Any]:
        """
        執行 ReAct 推理迴圈。

        Args:
            user_query: 使用者的問題或指令
            context: 額外背景資訊（記憶召回結果等）
            conversation_history: 對話歷史（可選）

        Returns:
            {
                "success": bool,
                "answer": str,        # 最終回答
                "trace": list[dict],  # 推理軌跡
                "steps": int,         # 使用步數
                "tools_used": list,   # 使用的工具列表
                "elapsed_sec": float,
            }
        """
        soul = getattr(self, "_soul_text", "")
        system_prompt = self._build_system_prompt(soul_text=soul)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # 加入背景資訊
        if context:
            messages.append({
                "role": "system",
                "content": f"相關背景資訊（來自記憶庫）：\n{context[:MAX_OBS_CHARS]}",
            })

        # 加入對話歷史
        if conversation_history:
            for msg in conversation_history[-6:]:  # 最多 6 輪歷史
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": str(msg.get("content", ""))[:500],
                })

        # 加入使用者問題
        messages.append({"role": "user", "content": user_query})

        trace: list[dict] = []
        tools_used: list[str] = []
        started = time.monotonic()

        for step in range(1, self.max_steps + 1):
            # 超時檢查
            elapsed = time.monotonic() - started
            if elapsed > self.total_timeout:
                trace.append({"step": step, "type": "timeout", "elapsed": round(elapsed, 1)})
                break

            # Context 壓縮 — 多階段策略（工具結果裁剪 + head/tail 保護 + 中間段摘要）
            if len(messages) > 14:
                try:
                    from skills.engine.trajectory_compressor import TrajectoryCompressor
                    _tc = TrajectoryCompressor()
                    messages = _tc.compress_for_react(messages, max_tokens=6000)
                except Exception:
                    # Fallback: 原有粗暴裁剪
                    system_msgs = [m for m in messages if m["role"] == "system"]
                    non_system = [m for m in messages if m["role"] != "system"]
                    messages = system_msgs + non_system[-6:]

            # 呼叫 LLM
            try:
                response = self._llm(messages)
            except Exception as exc:
                trace.append({"step": step, "type": "llm_error", "error": str(exc)})
                break

            if not response or not response.strip():
                trace.append({"step": step, "type": "empty_response"})
                break

            # 檢查是否有 FINAL
            final_answer = self._parse_final(response)
            if final_answer:
                trace.append({"step": step, "type": "final", "content": final_answer[:500]})
                return {
                    "success": True,
                    "answer": final_answer,
                    "trace": trace,
                    "steps": step,
                    "tools_used": tools_used,
                    "elapsed_sec": round(time.monotonic() - started, 2),
                }

            # 檢查是否有 ACTION
            if "ACTION:" in response:
                tool_name, params = self._parse_action(response)

                # 記錄思考過程
                think_match = re.search(r'<think>(.*?)</think>', response, re.DOTALL)
                if think_match:
                    trace.append({"step": step, "type": "think", "content": think_match.group(1).strip()[:300]})

                # Iron Dome 安全檢查
                block_reason = self._iron_dome_check(tool_name, params)
                if block_reason:
                    observation = f"⛔ {block_reason}"
                    trace.append({"step": step, "type": "blocked", "tool": tool_name, "reason": block_reason})
                else:
                    # 執行工具
                    trace.append({"step": step, "type": "action", "tool": tool_name, "params": params})
                    tools_used.append(tool_name)

                    try:
                        tool_fn = self.tools[tool_name]["fn"]
                        tool_result = tool_fn(**params) if params else tool_fn()
                        observation = str(tool_result)[:MAX_OBS_CHARS]
                    except Exception as exc:
                        observation = f"工具執行錯誤: {type(exc).__name__}: {exc}"

                    trace.append({"step": step, "type": "observation", "content": observation[:300]})

                # 把 LLM 回應和觀察加到 messages
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": f"OBSERVATION: {observation}"})
            else:
                # LLM 既沒有 FINAL 也沒有 ACTION — 可能直接回答了
                # 如果回覆夠長且有實質內容，視為最終回答
                clean = response.strip()
                if len(clean) > 20 and not clean.startswith("LLM ERROR"):
                    trace.append({"step": step, "type": "direct_answer", "content": clean[:300]})
                    return {
                        "success": True,
                        "answer": clean,
                        "trace": trace,
                        "steps": step,
                        "tools_used": tools_used,
                        "elapsed_sec": round(time.monotonic() - started, 2),
                    }

                # 第 2 步以後不再提醒格式，避免無限循環浪費 token
                if step >= 2:
                    # 已經嘗試過了，直接結束
                    trace.append({"step": step, "type": "give_up_format"})
                    break
                # 第 1 步提醒一次格式
                messages.append({"role": "assistant", "content": response})
                messages.append({
                    "role": "user",
                    "content": "請使用 ACTION: 工具名稱 + PARAMS: {...} 呼叫工具，或 FINAL: 給出答案。",
                })
                trace.append({"step": step, "type": "format_reminder"})

        # 超過最大步數或超時 — 強制用 LLM 做最後總結
        elapsed = round(time.monotonic() - started, 2)

        # 收集所有 observation 作為已知資訊
        observations = [t["content"] for t in trace if t["type"] == "observation" and t.get("content")]
        if observations and elapsed < self.total_timeout - 30:
            # 還有時間，讓 LLM 根據已收集的資訊給最終答案
            summary_prompt = (
                f"你已經完成了 {len(tools_used)} 個步驟但還沒給出最終答案。\n"
                f"以下是你收集到的資訊：\n\n" +
                "\n---\n".join(obs[:500] for obs in observations[-4:]) +
                "\n\n請根據以上資訊，直接回答使用者的問題。不要再呼叫工具。"
            )
            messages.append({"role": "user", "content": summary_prompt})
            try:
                final_response = self._llm(messages)
                if final_response and len(final_response.strip()) > 10:
                    trace.append({"step": self.max_steps + 1, "type": "forced_summary", "content": final_response.strip()[:500]})
                    return {
                        "success": True,
                        "answer": final_response.strip(),
                        "trace": trace,
                        "steps": self.max_steps + 1,
                        "tools_used": tools_used,
                        "elapsed_sec": round(time.monotonic() - started, 2),
                        "partial": True,
                    }
            except Exception:
                pass

        # fallback: 從 trace 找最後有用內容
        if trace:
            for t in reversed(trace):
                if t["type"] in ("observation", "direct_answer", "think") and t.get("content"):
                    return {
                        "success": True,
                        "answer": t["content"],
                        "trace": trace,
                        "steps": len([t for t in trace if t["type"] in ("action", "final", "direct_answer")]),
                        "tools_used": tools_used,
                        "elapsed_sec": elapsed,
                        "partial": True,
                    }

        return {
            "success": False,
            "answer": "抱歉，我嘗試了多個步驟但無法完成這個任務。請試著把問題拆小，或換個方式描述。",
            "error": "max_steps_reached" if elapsed < self.total_timeout else "timeout",
            "trace": trace,
            "steps": self.max_steps,
            "tools_used": tools_used,
            "elapsed_sec": elapsed,
        }
