from __future__ import annotations

from typing import Dict, List


class TrajectoryCompressor:
    def __init__(self, milestone_keywords: List[str] = None) -> None:
        self.milestone_keywords = milestone_keywords or [
            "法扶", "閱卷", "筆錄", "判決", "法規", "實務見解", "錯誤", "修正", "完成",
        ]

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(str(text or "")) // 4)

    def _is_milestone(self, message: Dict[str, str]) -> bool:
        content = str(message.get("content") or "")
        role = str(message.get("role") or "")
        if role in {"tool", "system"}:
            return True
        return any(keyword in content for keyword in self.milestone_keywords)

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

        pruned: List[Dict[str, str]] = list(system)
        remaining_slots = max(0, limit - len(system) - len(tail))
        if remaining_slots > 0:
            if len(milestones) > remaining_slots:
                step = max(1, len(milestones) // remaining_slots)
                pruned.extend(milestones[::step][:remaining_slots])
            else:
                pruned.extend(milestones)
        pruned.extend(tail[-max(1, limit - len(pruned)) :])
        return pruned[:limit]

    def compress(self, messages: List[Dict[str, str]], max_tokens: int, max_messages: int = 20) -> List[Dict[str, str]]:
        if not messages:
            return []
        budget = max(20, int(max_tokens))
        kept: List[Dict[str, str]] = []

        if messages and str(messages[0].get("role") or "") == "system":
            kept.append(messages[0])

        middle = messages[1:-10] if len(messages) > 12 else []
        tail = messages[-10:] if len(messages) > 1 else messages

        for message in middle:
            if self._is_milestone(message):
                kept.append(message)

        kept.extend(tail)

        final: List[Dict[str, str]] = []
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
