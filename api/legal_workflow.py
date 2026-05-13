"""Public-safe legal workflow guardrails shared by MAGI legal features.

This module deliberately contains generic legal-tech workflow rules only. It
does not depend on private OSC data, client names, paths, or credentials.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


LEGAL_WORKFLOW_AGENTS: list[dict[str, Any]] = [
    {
        "key": "legal_research_agent",
        "title": "實務見解檢索代理",
        "scope": "法律問題、書狀引用、裁判檢索",
        "steps": ["確認案由與爭點", "查可用法律資料來源", "列出可引用來源", "無來源時明示查不到"],
        "guardrails": ["不得把摘要當全文", "引用前須保留裁判字號與來源", "查不到不得補故事"],
        "review_gate": "律師核對全文後才能放入正式書狀。",
    },
    {
        "key": "pleading_review_agent",
        "title": "書狀覆核代理",
        "scope": "草稿到定稿",
        "steps": ["比對同案由學習紀錄", "檢查狀頭與案號", "檢查引用來源", "檢查格式與段落", "輸出需人工確認清單"],
        "guardrails": ["同案由才套用學習", "不得跨程序混用範本", "含待確認欄位不得視為完成"],
        "review_gate": "人工確認後才匯出 DOCX/PDF。",
    },
    {
        "key": "laf_compliance_agent",
        "title": "法扶回報代理",
        "scope": "法扶開辦、活動計數、報結",
        "steps": ["確認法扶案號", "統計律見、閱卷、聯繫、開庭", "排除同名不同案", "產生可複製回報文字"],
        "guardrails": ["法扶送出不自動提交", "同名多案需看案件種類與案號", "結案搬移先預覽後執行"],
        "review_gate": "對外提交前必須人工按確認。",
    },
]


PRACTICE_AREA_PROFILES: list[dict[str, Any]] = [
    {
        "key": "debt_profile",
        "title": "消債事件設定檔",
        "scope": "更生 / 清算 / 調解",
        "rules": ["所得清單依聲請前兩年度動態推算", "債權人清冊與財產資料需分項追蹤"],
    },
    {
        "key": "criminal_profile",
        "title": "刑事案件設定檔",
        "scope": "閱卷、律見、開庭、筆錄",
        "rules": ["閱卷次數以有效內容優先", "純繳費單資料夾不列入閱卷", "筆錄下載只顯示實際有新增者"],
    },
    {
        "key": "civil_profile",
        "title": "民事案件設定檔",
        "scope": "一般民事、強制執行、家事",
        "rules": ["同名不同程序不得混案", "強制執行可用執行命令作為結案依據", "書狀引用需回到來源文件"],
    },
]


_LEGAL_HINT_RE = re.compile(
    r"(法律|法條|法規|判決|裁判|實務見解|法院見解|釋字|憲判|書狀|起訴|答辯|聲請|"
    r"法扶|法律扶助|消債|更生|清算|閱卷|律見|筆錄|強制執行|監護|損害賠償)"
)
_CITATION_RE = re.compile(r"\d{2,3}年度[^\s，。、；;]{1,16}字第?\d{1,6}號")
_CASE_PROFILE_RULES: tuple[tuple[str, str], ...] = (
    ("debt_profile", r"消債|更生|清算|債務|債權|前置調解|司消債|消債調"),
    ("criminal_profile", r"刑|偵|訴字|上訴|原訴|閱卷|律見|筆錄|羈押|不起訴"),
    ("civil_profile", r"民事|損害賠償|契約|強制執行|司執|家事|監護|給付|清償|侵權"),
)


def _copy(item: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(item)


def _find(items: list[dict[str, Any]], key: str) -> dict[str, Any]:
    for item in items:
        if item.get("key") == key:
            return _copy(item)
    return _copy(items[0])


def _haystack(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts).strip()


def is_legal_workflow_candidate(*parts: Any, mode: str = "answer") -> bool:
    text = _haystack(*parts)
    if mode in {"draft", "laf", "legal"}:
        return True
    return bool(_LEGAL_HINT_RE.search(text))


def select_practice_profile(text: str) -> dict[str, Any]:
    for key, pattern in _CASE_PROFILE_RULES:
        if re.search(pattern, text):
            return _find(PRACTICE_AREA_PROFILES, key)
    return _find(PRACTICE_AREA_PROFILES, "civil_profile")


def select_legal_agent(text: str, *, mode: str = "answer", doc_type: str = "") -> dict[str, Any]:
    combined = _haystack(text, doc_type)
    if mode == "laf" or re.search(r"法扶|法律扶助|開辦|報結|法扶案號", combined):
        return _find(LEGAL_WORKFLOW_AGENTS, "laf_compliance_agent")
    if mode == "draft" or re.search(r"書狀|起訴|答辯|聲請|準備狀|陳報狀|抗告", combined):
        return _find(LEGAL_WORKFLOW_AGENTS, "pleading_review_agent")
    return _find(LEGAL_WORKFLOW_AGENTS, "legal_research_agent")


def detect_legal_workflow(
    *,
    text: str = "",
    reason: str = "",
    doc_type: str = "",
    mode: str = "answer",
) -> dict[str, Any]:
    combined = _haystack(text, reason, doc_type)
    if not is_legal_workflow_candidate(combined, mode=mode):
        return {"enabled": False, "agent": None, "practice_profile": None, "must_use_tools": [], "guardrails": []}
    agent = select_legal_agent(combined, mode=mode, doc_type=doc_type)
    profile = select_practice_profile(combined)
    if agent.get("key") == "legal_research_agent":
        tools = ["configured_legal_sources"]
    elif agent.get("key") == "pleading_review_agent":
        tools = ["selected_reference_documents", "same_reason_learning", "source_quality_check"]
    else:
        tools = ["laf_case_database", "calendar_activity_counter", "folder_status_check"]
    return {
        "enabled": True,
        "agent": agent,
        "practice_profile": profile,
        "must_use_tools": tools,
        "guardrails": list(agent.get("guardrails") or []) + list(profile.get("rules") or []),
        "review_gate": agent.get("review_gate") or "",
    }


def workflow_prompt_block(workflow: dict[str, Any]) -> str:
    if not workflow.get("enabled"):
        return ""
    agent = workflow.get("agent") or {}
    profile = workflow.get("practice_profile") or {}
    lines = [f"法律工作流：{agent.get('title', '法律工作流')}", f"案件設定：{profile.get('title', '一般法律事件')}", "必做步驟："]
    lines.extend(f"- {step}" for step in (agent.get("steps") or []))
    if workflow.get("must_use_tools"):
        lines.append("必用資料來源/工具：" + "、".join(str(tool) for tool in workflow.get("must_use_tools") or []))
    if workflow.get("guardrails"):
        lines.append("限制：")
        lines.extend(f"- {rule}" for rule in workflow.get("guardrails") or [])
    if workflow.get("review_gate"):
        lines.append(f"覆核門檻：{workflow.get('review_gate')}")
    return "\n".join(lines)


def append_workflow_footer(text: str, workflow: dict[str, Any], *, tool_used: bool = False) -> str:
    body = str(text or "").rstrip()
    if not body or not workflow.get("enabled") or "法律工作流：" in body:
        return body
    agent = workflow.get("agent") or {}
    profile = workflow.get("practice_profile") or {}
    source_note = "已啟用可用法律資料來源" if tool_used else "已套用法律工作流規則"
    return (
        f"{body}\n\n---\n"
        f"法律工作流：{agent.get('title', '法律工作流')}｜{profile.get('title', '一般法律事件')}｜{source_note}\n"
        f"覆核：{workflow.get('review_gate') or '正式引用前請核對來源全文。'}"
    )


def workflow_review(
    text: str,
    workflow: dict[str, Any],
    *,
    source_count: int = 0,
    selected_insights: int = 0,
    selected_documents: int = 0,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if not workflow.get("enabled"):
        return {"pass": True, "issues": issues}
    source_total = max(0, int(source_count or 0)) + max(0, int(selected_insights or 0)) + max(0, int(selected_documents or 0))
    citations = _CITATION_RE.findall(str(text or ""))
    if citations and source_total <= 0:
        issues.append(
            {
                "severity": "high",
                "code": "legal_citation_without_source",
                "message": f"偵測到 {len(citations)} 個裁判/案號引用，但沒有來源文件或見解資料。",
            }
        )
    if re.search(r"待確認|TODO|FIXME", str(text or ""), re.I):
        issues.append({"severity": "medium", "code": "workflow_placeholder", "message": "法律工作流偵測到仍有待確認欄位。"})
    return {"pass": not any(issue.get("severity") in {"critical", "high"} for issue in issues), "issues": issues}
