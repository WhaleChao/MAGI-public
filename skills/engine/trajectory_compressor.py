"""
trajectory_compressor.py — 對話軌跡壓縮器
================================================
Phase 1: 工具結果裁剪（純規則，不呼叫 LLM）
Phase 2: Head/Tail 邊界保護
Phase 3: （可選）LLM 摘要中間段
Phase 4: 摘要注入 + anti-thrashing

相容層：原有 compress() API 不變。
新增：compress_for_react() 給 ReAct engine 用。

移植自 Hermes Agent (NousResearch/hermes-agent) context_compressor.py，
適配 MAGI Python 3.9 + oMLX 架構。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


class TrajectoryCompressor:

    # ── Phase 1 設定 ──
    TOOL_RESULT_MAX_CHARS = 300       # 工具結果超過此長度就壓縮成摘要
    TOOL_SUMMARY_TEMPLATE = "[{role}] {preview}"

    # ── Phase 2 設定 ──
    HEAD_KEEP = 3          # 保留 system 後的前 N 輪
    TAIL_TOKEN_BUDGET = 5000   # tail 區段的 token 預算（ReAct 用 E4B 8K context）

    # ── Anti-thrashing ──
    _last_savings = [100, 100]  # 最近兩次壓縮的節省百分比

    def __init__(self, milestone_keywords: Optional[List[str]] = None) -> None:
        self.milestone_keywords = milestone_keywords or [
            "法扶", "閱卷", "筆錄", "判決", "法規", "錯誤", "修正", "完成",
        ]

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(str(text or "")) // 4)

    def _total_tokens(self, messages: List[Dict[str, str]]) -> int:
        return sum(self._estimate_tokens(m.get("content", "")) for m in messages)

    def _is_milestone(self, message: Dict[str, str]) -> bool:
        content = str(message.get("content") or "")
        role = str(message.get("role") or "")
        if role in {"tool", "system"}:
            return True
        return any(keyword in content for keyword in self.milestone_keywords)

    # ═══════════════════════════════════════════════════
    # Phase 1：工具結果裁剪
    # ═══════════════════════════════════════════════════

    @classmethod
    def _prune_tool_result(cls, message: Dict[str, str]) -> Dict[str, str]:
        """把大型工具結果壓成 1 行摘要。"""
        content = str(message.get("content") or "")
        if len(content) <= cls.TOOL_RESULT_MAX_CHARS:
            return message

        role = str(message.get("role") or "assistant")
        lines = content.split("\n")
        line_count = len(lines)
        first_line = lines[0][:80] if lines else ""
        last_line = lines[-1][:80] if lines else ""

        # 檢測常見模式
        exit_match = re.search(r'exit[_ ]code[:\s]*(\d+)', content[:200], re.IGNORECASE)
        exit_info = " exit={}".format(exit_match.group(1)) if exit_match else ""

        preview = "{}...{} ({} lines{})".format(first_line, last_line, line_count, exit_info)
        summary = cls.TOOL_SUMMARY_TEMPLATE.format(role=role, preview=preview)

        pruned = dict(message)
        pruned["content"] = summary
        return pruned

    @classmethod
    def prune_tool_results(cls, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Phase 1：對所有工具結果做裁剪。不動 system/user 訊息。"""
        result = []  # type: List[Dict[str, str]]
        for msg in messages:
            role = str(msg.get("role") or "")
            if role in ("tool", "function") or (
                role == "assistant" and len(str(msg.get("content") or "")) > cls.TOOL_RESULT_MAX_CHARS
            ):
                result.append(cls._prune_tool_result(msg))
            else:
                result.append(msg)
        return result

    # ═══════════════════════════════════════════════════
    # Phase 2：Head/Tail 邊界保護
    # ═══════════════════════════════════════════════════

    def _split_head_middle_tail(
        self, messages: List[Dict[str, str]]
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
        """把 messages 切成 head / middle / tail 三段。"""
        # Head: system + 前 N 輪
        head = []  # type: List[Dict[str, str]]
        rest = list(messages)

        if rest and str(rest[0].get("role") or "") == "system":
            head.append(rest.pop(0))

        head_count = min(self.HEAD_KEEP * 2, len(rest))  # user+assistant 為一輪
        head.extend(rest[:head_count])
        rest = rest[head_count:]

        # Tail: 從後面累積到 token 預算
        tail = []  # type: List[Dict[str, str]]
        tail_tokens = 0
        for msg in reversed(rest):
            cost = self._estimate_tokens(msg.get("content", ""))
            if tail_tokens + cost > self.TAIL_TOKEN_BUDGET:
                break
            tail.insert(0, msg)
            tail_tokens += cost

        # Middle: 剩下的
        middle_end = len(rest) - len(tail)
        middle = rest[:middle_end]

        return head, middle, tail

    # ═══════════════════════════════════════════════════
    # Phase 3：中間段摘要（純規則版，不呼叫 LLM）
    # ═══════════════════════════════════════════════════

    def _summarize_middle_heuristic(self, middle: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
        """用規則從中間段提取重點，不需要 LLM。"""
        if not middle:
            return None

        actions = []  # type: List[str]
        observations = []  # type: List[str]

        for msg in middle:
            content = str(msg.get("content") or "")
            role = str(msg.get("role") or "")

            if role == "assistant" and "ACTION:" in content:
                # 提取工具呼叫
                action_match = re.search(r'ACTION:\s*(\w+)', content)
                if action_match:
                    actions.append(action_match.group(1))

            if role in ("tool", "function"):
                # 提取工具結果的首行
                first_line = content.split("\n")[0][:100]
                observations.append(first_line)

            if self._is_milestone(msg) and role != "system":
                # 提取 milestone 內容首行
                first_line = content.split("\n")[0][:100]
                if first_line not in observations:
                    observations.append(first_line)

        if not actions and not observations:
            return None

        lines = ["[CONTEXT COMPACTION — REFERENCE ONLY]"]
        lines.append("以下是先前推理步驟的摘要，僅供參考，不要重新執行：")
        if actions:
            lines.append("已使用工具: {}".format(", ".join(actions)))
        if observations:
            lines.append("關鍵觀察:")
            for obs in observations[:8]:  # 最多 8 條
                lines.append("  - {}".format(obs))
        lines.append("[/CONTEXT COMPACTION]")

        return {"role": "system", "content": "\n".join(lines)}

    # ═══════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════

    def compress_for_react(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 6000,
    ) -> List[Dict[str, str]]:
        """
        ReAct engine 專用壓縮。多階段策略：
        1. 工具結果裁剪
        2. Head/Tail 邊界保護
        3. 中間段規則摘要
        4. Anti-thrashing

        Args:
            messages: 對話歷史
            max_tokens: token 預算

        Returns:
            壓縮後的 messages list
        """
        if not messages:
            return []

        before_tokens = self._total_tokens(messages)

        # Anti-thrashing: 如果最近 2 次壓縮各省 <10%，就不再壓
        if all(s < 10 for s in self._last_savings):
            return messages

        # Phase 1: 工具結果裁剪
        pruned = self.prune_tool_results(messages)

        # 如果裁剪後已在預算內，直接回傳
        if self._total_tokens(pruned) <= max_tokens:
            return pruned

        # Phase 2: Head/Tail 切分
        head, middle, tail = self._split_head_middle_tail(pruned)

        # Phase 3: 中間段摘要
        summary_msg = self._summarize_middle_heuristic(middle)

        # Phase 4: 組合
        result = list(head)
        if summary_msg:
            result.append(summary_msg)
        result.extend(tail)

        # 記錄壓縮效果（anti-thrashing 用）
        after_tokens = self._total_tokens(result)
        savings_pct = int((1 - after_tokens / max(1, before_tokens)) * 100)
        self._last_savings = [self._last_savings[-1], savings_pct]

        return result

    # ── 原有 API（向下相容）──

    def _trim_to_max_messages(self, messages: List[Dict[str, str]], max_messages: int) -> List[Dict[str, str]]:
        limit = max(3, int(max_messages))
        if len(messages) <= limit:
            return messages
        system = [messages[0]] if messages and str(messages[0].get("role") or "") == "system" else []
        body = messages[1:] if system else list(messages)
        milestones = [msg for msg in body[:-6] if self._is_milestone(msg)]
        tail = body[-6:]
        kept = system + milestones + tail
        if len(kept) <= limit:
            return kept

        pruned_list = list(system)  # type: List[Dict[str, str]]
        remaining_slots = max(0, limit - len(system) - len(tail))
        if remaining_slots > 0:
            if len(milestones) > remaining_slots:
                step = max(1, len(milestones) // remaining_slots)
                pruned_list.extend(milestones[::step][:remaining_slots])
            else:
                pruned_list.extend(milestones)
        pruned_list.extend(tail[-max(1, limit - len(pruned_list)):])
        return pruned_list[:limit]

    def compress(self, messages: List[Dict[str, str]], max_tokens: int, max_messages: int = 20) -> List[Dict[str, str]]:
        """原有 API，不動。"""
        if not messages:
            return []
        budget = max(20, int(max_tokens))
        kept = []  # type: List[Dict[str, str]]

        if messages and str(messages[0].get("role") or "") == "system":
            kept.append(messages[0])

        middle = messages[1:-10] if len(messages) > 12 else []
        tail = messages[-10:] if len(messages) > 1 else messages

        for message in middle:
            if self._is_milestone(message):
                kept.append(message)

        kept.extend(tail)

        final = []  # type: List[Dict[str, str]]
        used = 0
        for message in kept:
            cost = self._estimate_tokens(message.get("content") or "")
            if used + cost > budget:
                trimmed = dict(message)
                text = str(trimmed.get("content") or "")
                allowed_chars = max(32, (budget - used) * 4)
                trimmed["content"] = text[:allowed_chars] + ("…" if len(text) > allowed_chars else "")
                final.append(trimmed)
                break
            final.append(message)
            used += cost
        return self._trim_to_max_messages(final, max_messages=max_messages)
