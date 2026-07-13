import logging
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

PENDING_FILE = f"{_MAGI_ROOT}/nightly_core_change_pending.json"
MAX_PREVIEW = 320
LEGACY_FALLBACK_CUTOFF = "2026-07-13T00:00:00"

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
    target_dir = os.path.dirname(PENDING_FILE) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".nightly_core_change_", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, PENDING_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _short(text: str, limit: int = MAX_PREVIEW) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit] + "...(truncated)"


def _issue_key(issue: str) -> str:
    """Return a stable key so changing counters do not create a new proposal every night."""
    body = (issue or "").strip().lower()
    known = (
        (("court_judgments", "摘要"), "judgment_summary_backlog"),
        (("missing_summary",), "judgment_summary_backlog"),
        (("判決摘要缺失",), "judgment_summary_backlog"),
        (("bad_notes",), "obsidian_note_quality"),
        (("weak_extraction",), "obsidian_note_quality"),
        (("low_text_signal",), "obsidian_note_quality"),
        (("orphan_notes",), "obsidian_index_alignment"),
        (("孤兒筆記",), "obsidian_index_alignment"),
        (("zero_chunk",), "obsidian_zero_chunk"),
        (("wiki_staleness",), "knowledge_staleness"),
        (("過時案例",), "knowledge_staleness"),
        (("info_gap",), "knowledge_quality_gap"),
        (("contradiction",), "knowledge_quality_gap"),
        (("ram",), "system_memory_pressure"),
        (("記憶體",), "system_memory_pressure"),
        (("faiss", "量化"), "faiss_optimization"),
        (("faiss", "壓縮"), "faiss_optimization"),
    )
    for terms, key in known:
        if all(term in body for term in terms):
            return key

    body = re.sub(r"\b(?:issue|action)\s*:\s*", " ", body, flags=re.IGNORECASE)
    body = re.sub(r"\d+(?:[.,]\d+)?%?", "#", body)
    body = re.sub(r"[`*_#|()（）\[\]{}:：,，。/\\\-]+", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:160] or "unspecified"


def _risk_profile(issue: str, proposal: str) -> tuple[str, List[str]]:
    body = f"{issue or ''}\n{proposal or ''}".lower()
    reasons: List[str] = []
    if re.search(r"\b\d{4,}\b", body) or any(x in body for x in ("萬筆", "全量", "大規模", "批次處理")):
        reasons.append("涉及大量資料")
    if any(x in body for x in ("刪除", "清理", "移動", "隔離", "drop table", "delete from", "purge")):
        reasons.append("可能變更或移動既有資料")
    if any(x in body for x in ("法律資料", "判決", "案件", "裁判")):
        reasons.append("涉及法律或案件資料")
    if any(x in body for x in ("資料庫", "索引重構", "schema", "migration")):
        reasons.append("涉及資料庫或索引結構")
    reasons = list(dict.fromkeys(reasons))
    return ("high" if reasons else "normal"), reasons


def _new_approval_id(items: List[Dict]) -> str:
    used = {str(item.get("id") or "") for item in items}
    stamp = datetime.now()
    for offset in range(120):
        candidate = (stamp + timedelta(seconds=offset)).strftime("ccr-%Y%m%d%H%M%S")
        if candidate not in used:
            return candidate
    raise RuntimeError("unable to allocate unique approval id")


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
    issue_key = _issue_key(issue)
    now = _now_iso()
    for existing in reversed(items):
        if existing.get("status") != "pending":
            continue
        existing_key = existing.get("issue_key") or _issue_key(existing.get("issue", ""))
        if existing_key != issue_key:
            continue
        existing["issue_key"] = existing_key
        existing["last_seen_at"] = now
        existing["repeat_count"] = int(existing.get("repeat_count") or 1) + 1
        existing["latest_issue"] = _short(issue, 220)
        _save(data)
        return {
            "success": True,
            "item": existing,
            "path": PENDING_FILE,
            "created": False,
            "deduplicated": True,
        }

    approval_id = _new_approval_id(items)
    risk_level, risk_reasons = _risk_profile(issue, proposal)
    quorum_lower = (quorum_rule or "").lower()
    degraded_review = "fallback" in quorum_lower or "degraded" in quorum_lower
    payload = {
        "id": approval_id,
        "status": "pending",
        "source": source,
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "issue": _short(issue, 220),
        "proposal": _short(proposal, 1800),
        "issue_key": issue_key,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "review_mode": "degraded_draft" if degraded_review else "full_council",
        "repeat_count": 1,
        "votes": votes or {},
        "quorum_rule": quorum_rule,
        "approved_by": "",
        "decision_note": "",
    }
    items.append(payload)
    _save(data)
    return {"success": True, "item": payload, "path": PENDING_FILE, "created": True, "deduplicated": False}


def archive_legacy_fallback_pending(
    *,
    cutoff: str = LEGACY_FALLBACK_CUTOFF,
    reason: str = "舊版降級審查不構成完整表決，已保留紀錄但停止等待核准。",
) -> Dict:
    """Retire old fallback proposals without deleting their audit trail."""
    data = _load()
    archived: List[str] = []
    now = _now_iso()
    for item in data.get("items", []):
        if item.get("status") != "pending":
            continue
        if "fallback" not in str(item.get("quorum_rule") or "").lower():
            continue
        if str(item.get("created_at") or "") >= cutoff:
            continue
        item["status"] = "expired"
        item["updated_at"] = now
        item["decision_note"] = reason
        item["approved_by"] = "policy_migration"
        archived.append(str(item.get("id") or ""))
    if archived:
        _save(data)
    return {"success": True, "archived": len(archived), "ids": archived, "path": PENDING_FILE}


def list_pending_core_changes(limit: int = 20) -> Dict:
    data = _load()
    items = [x for x in data.get("items", []) if x.get("status") == "pending"]
    items = sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)[: max(1, int(limit))]
    return {"success": True, "count": len(items), "items": items}


def get_core_change(approval_id: str) -> Dict:
    data = _load()
    for item in data.get("items", []):
        if item.get("id") == approval_id:
            return {"success": True, "item": item}
    return {"success": False, "error": f"approval_id not found: {approval_id}"}


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

    lines = [f"🧾 **核心變更待審清單：{len(items)} 項**"]
    for item in items:
        risk = "高風險" if item.get("risk_level") == "high" else "一般"
        mode = "降級草案" if item.get("review_mode") == "degraded_draft" else "完整審查"
        lines.append(
            f"- `{item.get('id')}`｜{risk}｜{mode}｜{item.get('issue','')[:90]}"
        )
    lines.append("先用 `查看提案 <id>` 閱讀內容，再用 `批准 <id>` 或 `拒絕 <id> [原因]`。")
    return "\n".join(lines)


def format_core_change_detail(approval_id: str) -> str:
    result = get_core_change(approval_id)
    if not result.get("success"):
        return f"❌ 找不到提案：`{approval_id}`"
    item = result["item"]
    status_map = {
        "pending": "等待您決定",
        "approved": "已核准",
        "rejected": "已拒絕",
        "expired": "已封存，不會執行",
    }
    stored_risk_reasons = item.get("risk_reasons") or []
    if not stored_risk_reasons:
        _, stored_risk_reasons = _risk_profile(item.get("issue", ""), item.get("proposal", ""))
    risk_reasons = "、".join(stored_risk_reasons) or "未偵測到額外風險"
    quorum_lower = str(item.get("quorum_rule") or "").lower()
    degraded = item.get("review_mode") == "degraded_draft" or "fallback" in quorum_lower or "degraded" in quorum_lower
    review_mode = "降級草案，未經完整三方審查" if degraded else "完整三方審查"
    return (
        f"🧾 **提案 {approval_id}**\n"
        f"狀態：{status_map.get(item.get('status'), item.get('status', '未知'))}\n"
        f"審查：{review_mode}\n"
        f"風險：{risk_reasons}\n\n"
        f"問題：\n{item.get('issue', '')}\n\n"
        f"提案內容：\n{item.get('proposal', '')}\n\n"
        "此處只顯示提案，不會執行任何變更。"
    )
