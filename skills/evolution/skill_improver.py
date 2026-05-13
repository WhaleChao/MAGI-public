from __future__ import annotations

from typing import Dict


def build_improvement_plan(skill_name: str, summary: Dict[str, object]) -> Dict[str, object]:
    success_rate = float(summary.get("success_rate") or 0.0)
    top_failure = str(summary.get("top_failure_reason") or "").strip()
    suggestions = []
    if success_rate < 0.8:
        suggestions.append("增加 self_test 與失敗案例回歸測試")
    if top_failure:
        suggestions.append(f"優先處理高頻失敗原因：{top_failure}")
        if "timeout" in top_failure.lower():
            suggestions.append("檢查 timeout、重試策略與外部依賴健康狀態")
        if "auth" in top_failure.lower() or "login" in top_failure.lower():
            suggestions.append("補強登入驗證、憑證檢查與 session 續存機制")
    suggestions.append("補充輸入驗證與結構化錯誤訊息")
    return {
        "skill": skill_name,
        "success_rate": success_rate,
        "suggestions": suggestions,
    }
