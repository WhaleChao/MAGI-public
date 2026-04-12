from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List


class UsageTracker:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def record(
        self,
        skill: str,
        success: bool,
        latency_ms: int,
        intent: str = "",
        failure_reason: str = "",
        auto_repaired: bool = False,
    ) -> Dict[str, object]:
        payload = {
            "timestamp": datetime.now().isoformat(),
            "skill": str(skill or "").strip(),
            "success": bool(success),
            "latency_ms": int(latency_ms or 0),
            "intent": str(intent or "").strip(),
            "failure_reason": str(failure_reason or "").strip(),
            "auto_repaired": bool(auto_repaired),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload

    def _load_rows(self) -> List[Dict[str, object]]:
        if not self.path.exists():
            return []
        rows: List[Dict[str, object]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows

    def summarize(self, days: int = 0) -> Dict[str, object]:
        rows = self._load_rows()
        if days and rows:
            cutoff = datetime.now() - timedelta(days=max(1, int(days)))
            filtered: List[Dict[str, object]] = []
            for row in rows:
                ts = str(row.get("timestamp") or "").strip()
                if not ts:
                    filtered.append(row)
                    continue
                try:
                    if datetime.fromisoformat(ts) >= cutoff:
                        filtered.append(row)
                except Exception:
                    filtered.append(row)
            rows = filtered
        skills = Counter(str(row.get("skill") or "").strip() for row in rows if str(row.get("skill") or "").strip())
        failures = Counter(
            str(row.get("failure_reason") or "").strip()
            for row in rows
            if (not bool(row.get("success"))) and str(row.get("failure_reason") or "").strip()
        )
        ok_count = sum(1 for row in rows if bool(row.get("success")))
        return {
            "event_count": len(rows),
            "success_rate": (ok_count / len(rows)) if rows else 0.0,
            "top_skills": skills.most_common(5),
            "top_failure_reason": (failures.most_common(1)[0][0] if failures else ""),
        }

    def daily_report(self, days: int = 7) -> str:
        summary = self.summarize(days=days)
        top_skills = ", ".join(f"{name}({count})" for name, count in summary.get("top_skills", [])[:3])
        return (
            f"近 {max(1, int(days))} 天共 {summary['event_count']} 次技能執行，"
            f"成功率 {summary['success_rate']:.0%}，"
            f"常用技能：{top_skills or '無'}，"
            f"高頻失敗原因：{summary.get('top_failure_reason') or '無'}。"
        )
