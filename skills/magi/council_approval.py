import logging
import json
import os
import re
from datetime import datetime
from typing import Dict, List
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

PENDING_FILE = f"{_MAGI_ROOT}/nightly_core_change_pending.json"
MAX_PREVIEW = 320

CORE_CHANGE_PATTERNS = [
    r"\bbrain[_\s-]?manager\b",
    r"\bdistributed\b",
    r"\bcluster\b",
    r"\borchestrator\b",
    r"\bdaemon\b",
    r"\bsecurity\b",
    r"\bauth\b",
    r"\btoken\b",
    r"\bsecret\b",
    r"\bcredential\b",
    r"\bdatabase\b",
    r"\bmigration\b",
    r"\bschema\b",
    r"\bdrop table\b",
    r"\bdelete from\b",
    r"核心",
    r"分散式",
    r"推理",
    r"憑證",
    r"認證",
    r"資料庫",
    r"遷移",
    r"安全",
]


def _now_iso() -> str:
    return datetime.now().isoformat()


def _ensure_file():
    if not os.path.exists(PENDING_FILE):
        payload = {"version": 1, "items": []}
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _load() -> Dict:
    _ensure_file()
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 56, exc_info=True)
    return {"version": 1, "items": []}


def _save(data: Dict):
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _short(text: str, limit: int = MAX_PREVIEW) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit] + "...(truncated)"


def is_core_change(issue: str, proposal: str) -> bool:
    text = f"{issue or ''}\n{proposal or ''}".lower()
    for pattern in CORE_CHANGE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def queue_core_change_for_approval(
    issue: str,
    proposal: str,
    votes: Dict,
    quorum_rule: str,
    source: str = "nightly_council",
) -> Dict:
    data = _load()
    items = data.setdefault("items", [])
    approval_id = datetime.now().strftime("ccr-%Y%m%d%H%M%S")
    payload = {
        "id": approval_id,
        "status": "pending",
        "source": source,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "issue": _short(issue, 220),
        "proposal": _short(proposal, 1800),
        "votes": votes or {},
        "quorum_rule": quorum_rule,
        "approved_by": "",
        "decision_note": "",
    }
    items.append(payload)
    _save(data)
    return {"success": True, "item": payload, "path": PENDING_FILE}


def list_pending_core_changes(limit: int = 20) -> Dict:
    data = _load()
    items = [x for x in data.get("items", []) if x.get("status") == "pending"]
    items = sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)[: max(1, int(limit))]
    return {"success": True, "count": len(items), "items": items}


def resolve_core_change(
    approval_id: str,
    decision: str,
    approver: str = "admin",
    note: str = "",
) -> Dict:
    decision_norm = (decision or "").strip().lower()
    if decision_norm not in {"approved", "rejected"}:
        return {"success": False, "error": "decision must be approved/rejected"}

    data = _load()
    for item in data.get("items", []):
        if item.get("id") == approval_id:
            if item.get("status") != "pending":
                return {"success": False, "error": f"item already {item.get('status')}"}
            item["status"] = decision_norm
            item["updated_at"] = _now_iso()
            item["approved_by"] = approver
            item["decision_note"] = (note or "").strip()
            _save(data)

            # ── Post-approval: auto-execute the patch ──
            if decision_norm == "approved":
                item["execution"] = _execute_after_approval(item)

            return {"success": True, "item": item}
    return {"success": False, "error": f"approval_id not found: {approval_id}"}


def _execute_after_approval(item: Dict) -> Dict:
    """Attempt to auto-apply the approved proposal. Non-blocking on failure."""
    try:
        from skills.magi.council_executor import execute_approved_change
        result = execute_approved_change(item)
        # Persist execution result
        data = _load()
        for entry in data.get("items", []):
            if entry.get("id") == item.get("id"):
                entry["execution"] = {
                    "success": result.get("success", False),
                    "patches_applied": result.get("patches_applied", []),
                    "error": result.get("error", ""),
                    "executed_at": _now_iso(),
                }
                break
        _save(data)

        # Notify admin of result
        try:
            from skills.ops.red_phone import alert_admin
            patch_id = item.get("id", "?")
            if result.get("success"):
                files = ", ".join(result.get("patches_applied", []))
                alert_admin(
                    f"✅ 核心變更 `{patch_id}` 已自動執行\n"
                    f"修改檔案：{files}\n"
                    f"備份位置：{result.get('details', {}).get('backup_dir', '?')}",
                    severity="info",
                )
            else:
                alert_admin(
                    f"❌ 核心變更 `{patch_id}` 執行失敗\n"
                    f"原因：{result.get('error', '?')[:300]}\n"
                    f"回滾：{'是' if result.get('details', {}).get('rolled_back') else '否'}",
                    severity="warning",
                )
        except Exception:
            logging.getLogger(__name__).debug("Notify after execution failed", exc_info=True)

        return result
    except Exception as e:
        logging.getLogger(__name__).error("Post-approval execution failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def format_pending_summary(limit: int = 10) -> str:
    result = list_pending_core_changes(limit=limit)
    if not result.get("success"):
        return "❌ 讀取核心變更待審失敗。"
    items: List[Dict] = result.get("items", [])
    if not items:
        return "📭 目前沒有核心變更待審。"

    lines = [f"🧾 **核心變更待審清單** ({len(items)})"]
    for item in items:
        lines.append(
            f"- `{item.get('id')}` | {item.get('created_at','')} | Issue: {item.get('issue','')[:90]}"
        )
    lines.append("可用：`批准核心變更 <id>` 或 `拒絕核心變更 <id> [原因]`")
    return "\n".join(lines)
