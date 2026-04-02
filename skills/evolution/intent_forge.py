import logging
import json
import os
import re
from datetime import datetime
from typing import Dict, Optional

from skills.evolution.skill_genesis import acquire_skill, run_skill_action, run_skill_ci
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

PENDING_FILE = f"{_MAGI_ROOT}/.agent/intent_forge_pending.json"


def _ensure_pending_store():
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    if not os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump({"items": {}}, f, ensure_ascii=False, indent=2)


def _load_pending() -> dict:
    _ensure_pending_store()
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("items", {})
            if isinstance(data["items"], dict):
                return data
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 29, exc_info=True)
    return {"items": {}}


def _save_pending(data: dict):
    _ensure_pending_store()
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_pending_issue(user_id: str) -> Optional[dict]:
    store = _load_pending()
    return (store.get("items") or {}).get(str(user_id))


def clear_pending_issue(user_id: str):
    store = _load_pending()
    items = store.get("items") or {}
    if str(user_id) in items:
        items.pop(str(user_id), None)
        _save_pending(store)


def _set_pending_issue(user_id: str, payload: dict):
    store = _load_pending()
    items = store.get("items") or {}
    payload = dict(payload or {})
    payload["updated_at"] = datetime.now().isoformat()
    items[str(user_id)] = payload
    store["items"] = items
    _save_pending(store)


def _extract_error_text(run_result: dict) -> str:
    if not isinstance(run_result, dict):
        return "unknown error"
    trace = run_result.get("trace") or []
    if trace and isinstance(trace[-1], dict):
        t = trace[-1]
        text = (t.get("stderr") or t.get("stdout") or "").strip()
        if text:
            return text
    return (run_result.get("error") or "unknown error").strip()


def _question_from_error(err: str, skill_folder: str = "") -> str:
    e = (err or "").lower()
    if "module not found" in e or "no module named" in e:
        return "我缺少依賴套件，請問你是否允許我為此功能安裝對應 Python 套件？（回覆：允許安裝）"
    if "permission denied" in e:
        return "遇到權限限制。請告訴我這個任務允許操作的資料夾路徑。"
    if "file not found" in e or "no such file" in e:
        return "找不到必要檔案。請提供正確檔案路徑，或說明要用哪個檔案。"
    if "401" in e or "403" in e or "unauthorized" in e or "forbidden" in e:
        return "遇到授權/權限問題。請提供可用金鑰或授權方式（例如 API token / 帳密設定）。"
    if "timeout" in e or "timed out" in e:
        return "執行逾時。請告訴我要優先『提高超時』還是『縮小任務範圍』？"
    if "syntax" in e or "parse" in e:
        return "目前生成的程式仍有語法/解析問題。你希望我偏保守修補（最小改動）還是重寫該功能核心？"
    if "iron dome blocked" in e:
        return "鐵穹阻擋了這次實作。請告訴我此任務的白名單邊界（允許與禁止行為），我再重新生成。"
    skill_info = f"（技能：{skill_folder}）" if skill_folder else ""
    return f"我已自動除錯但仍卡住{skill_info}。請補充更具體輸入/範例，讓我續跑修復。"


def _build_summary(acquire_result: dict, run_result: dict) -> str:
    skill = acquire_result.get("skill_folder", "")
    output = (run_result.get("output") or "").strip()
    if len(output) > 1200:
        output = output[:1200] + "\n...(truncated)"
    header = "🧩 **CASPER 任務鍛造完成**\n"
    if skill:
        header += f"技能: `{skill}`\n"
    return f"{header}\n✅ 已完成執行。\n\n{output or '(無輸出)'}"


def forge_execute(
    user_id: str,
    request_text: str,
    clarification: str = "",
    route_tag: str = "intent_forge",
) -> Dict:
    """
    AIForge-like autonomous loop:
    intent -> acquire capability -> execute -> self-debug -> ask user on blocker.
    """
    user_id = str(user_id or "unknown")
    task = (request_text or "").strip()
    if clarification:
        task = f"{task}\n\n[User Clarification]\n{clarification.strip()}"
    if not task:
        return {"success": False, "reply": "❓ 請提供你要我執行的任務內容。"}

    acquire_result = acquire_skill(task, auto_generate=True, auto_activate=True)
    if not acquire_result.get("success"):
        err = acquire_result.get("message") or acquire_result.get("error") or "acquire failed"
        q = _question_from_error(err)
        _set_pending_issue(
            user_id,
            {
                "stage": "acquire",
                "origin_task": request_text,
                "last_error": err[:1200],
                "question": q,
                "skill": "",
            },
        )
        return {
            "success": False,
            "reply": (
                "🧩 我理解了你的目標，已嘗試自動建立能力，但目前被阻塞。\n"
                f"錯誤: {err}\n"
                f"👉 {q}\n"
                "請直接回覆你的補充，我會繼續自動修復。"
            ),
            "needs_user_input": True,
            "question": q,
        }

    skill_folder = acquire_result.get("skill_folder", "")
    run_result = run_skill_action(
        skill_folder,
        task,
        timeout_sec=45,
        auto_repair=True,
        rollback_on_fail=True,
        route_key=f"{route_tag}:{user_id}:{task[:100]}",
    )
    if run_result.get("success"):
        clear_pending_issue(user_id)
        return {"success": True, "reply": _build_summary(acquire_result, run_result), "skill": skill_folder}

    # One extra repair cycle using CI before asking user.
    ci_result = run_skill_ci(skill_folder, task=task, attempt_repair=True) if skill_folder else {"success": False}
    if ci_result.get("success") and skill_folder:
        rerun = run_skill_action(
            skill_folder,
            task,
            timeout_sec=45,
            auto_repair=True,
            rollback_on_fail=True,
            route_key=f"{route_tag}:{user_id}:{task[:100]}:ci_rerun",
        )
        if rerun.get("success"):
            clear_pending_issue(user_id)
            return {"success": True, "reply": _build_summary(acquire_result, rerun), "skill": skill_folder}
        run_result = rerun

    err = _extract_error_text(run_result)
    q = _question_from_error(err, skill_folder=skill_folder)
    _set_pending_issue(
        user_id,
        {
            "stage": "run",
            "origin_task": request_text,
            "last_error": err[:1200],
            "question": q,
            "skill": skill_folder,
        },
    )
    return {
        "success": False,
        "needs_user_input": True,
        "question": q,
        "reply": (
            f"🧩 已自動除錯，但 `{skill_folder or 'unknown-skill'}` 仍失敗。\n"
            f"錯誤摘要: {err[:500]}\n"
            f"👉 {q}\n"
            "請回覆你的補充內容，我會直接接續修復執行。"
        ),
        "skill": skill_folder,
    }


def forge_continue_with_user_feedback(user_id: str, user_feedback: str) -> Dict:
    user_id = str(user_id or "unknown")
    pending = get_pending_issue(user_id)
    if not pending:
        return {"success": False, "reply": "ℹ️ 目前沒有待補充的除錯問題。"}

    task = pending.get("origin_task", "")
    clarification = (user_feedback or "").strip()
    skill_folder = pending.get("skill", "")

    # If skill was already acquired and only execution failed, skip re-acquisition
    if skill_folder and pending.get("stage") == "run":
        logger.info(f"🔄 Resuming execution (skip acquire) for {skill_folder} with user feedback")
        combined_task = f"{task}\n\n[User Clarification]\n{clarification}" if clarification else task
        run_result = run_skill_action(
            skill_folder,
            combined_task,
            timeout_sec=45,
            auto_repair=True,
            rollback_on_fail=True,
            route_key=f"intent_forge_continue:{user_id}:{task[:80]}",
        )
        if run_result.get("success"):
            clear_pending_issue(user_id)
            return {"success": True, "reply": _build_summary({"skill_folder": skill_folder}, run_result), "skill": skill_folder}

        # Still failing — try CI repair then one more run
        ci_result = run_skill_ci(skill_folder, task=combined_task, attempt_repair=True)
        if ci_result.get("success"):
            rerun = run_skill_action(
                skill_folder, combined_task, timeout_sec=45,
                auto_repair=True, rollback_on_fail=True,
                route_key=f"intent_forge_continue:{user_id}:{task[:80]}:ci_rerun",
            )
            if rerun.get("success"):
                clear_pending_issue(user_id)
                return {"success": True, "reply": _build_summary({"skill_folder": skill_folder}, rerun), "skill": skill_folder}
            run_result = rerun

        # Update pending with new error
        err = _extract_error_text(run_result)
        q = _question_from_error(err, skill_folder=skill_folder)
        _set_pending_issue(user_id, {
            "stage": "run",
            "origin_task": task,
            "last_error": err[:1200],
            "question": q,
            "skill": skill_folder,
        })
        return {
            "success": False,
            "needs_user_input": True,
            "question": q,
            "reply": f"🧩 已用您的補充重新執行 `{skill_folder}`，但仍失敗。\n錯誤: {err[:500]}\n👉 {q}",
            "skill": skill_folder,
        }

    # Acquisition failed or no skill yet — full re-execution
    return forge_execute(
        user_id=user_id,
        request_text=task,
        clarification=clarification,
        route_tag="intent_forge_continue",
    )

