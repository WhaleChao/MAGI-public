from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional


class UserInsightsEngine:
    def __init__(self, events_path: str = "") -> None:
        self.events_path = Path(events_path) if events_path else None

    def _load_events(self) -> List[Dict[str, object]]:
        if not self.events_path or not self.events_path.exists():
            return []
        rows: List[Dict[str, object]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
            except Exception:
                continue
        return rows

    def extract_insights(self, days: int = 7, events: Optional[List[Dict[str, object]]] = None) -> Dict[str, object]:
        cutoff = datetime.now() - timedelta(days=max(1, int(days)))
        rows = list(events) if events is not None else self._load_events()
        filtered: List[Dict[str, object]] = []
        for row in rows:
            ts = str(row.get("timestamp") or row.get("ts") or "").strip()
            if not ts:
                filtered.append(row)
                continue
            try:
                if datetime.fromisoformat(ts) >= cutoff:
                    filtered.append(row)
            except Exception:
                filtered.append(row)

        skills = Counter(str(row.get("skill") or "").strip() for row in filtered if str(row.get("skill") or "").strip())
        intents = Counter(str(row.get("intent") or "").strip() for row in filtered if str(row.get("intent") or "").strip())
        return {
            "days": days,
            "event_count": len(filtered),
            "top_skills": skills.most_common(5),
            "top_intents": intents.most_common(5),
        }

    def get_personalization_context(self, days: int = 7, events: Optional[List[Dict[str, object]]] = None) -> str:
        insights = self.extract_insights(days=days, events=events)
        top_skills = ", ".join(f"{name}({count})" for name, count in insights.get("top_skills", [])[:3])
        top_intents = ", ".join(f"{name}({count})" for name, count in insights.get("top_intents", [])[:3])
        return (
            f"近 {insights['days']} 天互動 {insights['event_count']} 次；"
            f"常用技能：{top_skills or '無'}；"
            f"常見意圖：{top_intents or '無'}。"
        )
