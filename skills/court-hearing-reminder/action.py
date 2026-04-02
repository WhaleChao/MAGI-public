#!/usr/bin/env python3
"""
court-hearing-reminder/action.py

開庭提醒、補正期限、繳費期限 — 統一排程提醒。
- scan:   掃描未來 N 天內的排程（開庭/補正/繳費）
- remind: 發送提醒（前一天 + 當天）
- prep:   生成特定案件庭前準備摘要
- list:   列出所有待辦排程（人類可讀）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

logger = logging.getLogger("court-hearing-reminder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# ── DB ──────────────────────────────────────────────────────────────
def _get_conn():
    _osc_dir = str(_MAGI_ROOT / "skills" / "osc-orchestrator")
    if _osc_dir not in sys.path:
        sys.path.insert(0, _osc_dir)
    from osc_headless.db import DBConfig, connect_mysql
    return connect_mysql(DBConfig())


_SUPPORTED_TODO_TYPES = ("開庭", "補正", "繳費")


def _fetch_upcoming_hearings(conn, days_ahead: int = 7, todo_types: Optional[tuple] = None) -> List[Dict[str, Any]]:
    """從 case_todos 取得未來 N 天內指定 todo_type 的排程。"""
    if todo_types is None:
        todo_types = _SUPPORTED_TODO_TYPES
    today = date.today()
    end = today + timedelta(days=days_ahead)
    cur = conn.cursor(dictionary=True)
    placeholders = ", ".join(["%s"] * len(todo_types))
    try:
        cur.execute(
            f"""
            SELECT
                ct.id,
                ct.case_number,
                ct.client_name,
                ct.todo_type,
                ct.todo_date,
                ct.todo_time,
                ct.description,
                ct.source_file,
                ct.status,
                COALESCE(c.court_name, '') AS court_name,
                COALESCE(c.case_reason, '') AS case_reason,
                COALESCE(c.case_type, '') AS case_type,
                COALESCE(NULLIF(c.court_case_no, ''), c.court_case_number, '') AS court_case_number
            FROM case_todos ct
            LEFT JOIN cases c
              ON c.case_number COLLATE utf8mb4_unicode_ci
               = ct.case_number COLLATE utf8mb4_unicode_ci
            WHERE ct.todo_type IN ({placeholders})
              AND ct.todo_date >= %s
              AND ct.todo_date <= %s
              AND (ct.status IS NULL OR ct.status IN ('', 'pending'))
            ORDER BY ct.todo_date ASC, ct.todo_time ASC
            """,
            (*todo_types, today.isoformat(), end.isoformat()),
        )
        rows = cur.fetchall() or []
        result = []
        for r in rows:
            d = dict(r)
            # Serialize date/time for JSON
            for k in ("todo_date", "created_date", "completed_date"):
                if k in d and d[k] is not None and not isinstance(d[k], str):
                    d[k] = str(d[k])
            if "todo_time" in d and d["todo_time"] is not None and not isinstance(d["todo_time"], str):
                d["todo_time"] = str(d["todo_time"])
            result.append(d)
        return result
    finally:
        cur.close()


# ── 提醒狀態追蹤 ────────────────────────────────────────────────────
_REMIND_STATE_PATH = os.environ.get(
    "MAGI_HEARING_REMIND_STATE",
    str(_MAGI_ROOT / ".agent" / "hearing_remind_state.json"),
)


def _load_remind_state() -> Dict[str, Any]:
    try:
        from skills.ops.safe_state import safe_load_json
        return safe_load_json(_REMIND_STATE_PATH, default={})
    except ImportError:
        try:
            with open(_REMIND_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def _is_remind_key_sent(key: str, sent_keys: set) -> bool:
    """Check if a remind key was already sent — DB 優先，JSON fallback。"""
    try:
        from skills.ops.dedup_db import is_done as _dd_is_done
        if _dd_is_done("hearing_remind", key):
            sent_keys.add(key)  # 同步到 JSON set
            return True
    except Exception:
        pass
    return key in sent_keys


def _save_remind_state(state: Dict[str, Any]):
    try:
        from skills.ops.safe_state import safe_save_json
        safe_save_json(_REMIND_STATE_PATH, state, default=str)
    except ImportError:
        import tempfile
        dir_path = os.path.dirname(_REMIND_STATE_PATH)
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, _REMIND_STATE_PATH)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    # DB dedup sync: write all sent keys
    try:
        from skills.ops.dedup_db import mark_done as _dd_mark
        for key in (state.get("sent") or []):
            _dd_mark("hearing_remind", str(key), metadata={"source": "court_hearing_reminder"})
    except Exception:
        pass


# ── 通知 ────────────────────────────────────────────────────────────
def _send_reminder(message: str, severity: str = "info"):
    """透過 red_phone 發送提醒。"""
    try:
        from skills.ops.red_phone import alert_admin
        alert_admin(message, severity=severity, source="court_hearing_reminder", topic_key="alert")
        logger.info("提醒已發送")
    except Exception as e:
        logger.error("發送提醒失敗: %s", e)


# ── 庭前準備摘要 ────────────────────────────────────────────────────
def _generate_prep_summary(hearing: Dict[str, Any]) -> str:
    """為單一開庭排程生成準備摘要。"""
    case_number = hearing.get("case_number", "")
    client_name = hearing.get("client_name", "")
    court_name = hearing.get("court_name", "")
    court_case_no = hearing.get("court_case_number", "")
    case_reason = hearing.get("case_reason", "")
    todo_date = hearing.get("todo_date", "")
    todo_time = hearing.get("todo_time", "")
    description = hearing.get("description", "")

    # 時間格式化
    time_str = ""
    if todo_time:
        try:
            parts = str(todo_time).split(":")
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            period = "上午" if h < 12 else "下午"
            display_h = h if h <= 12 else h - 12
            time_str = f"{period}{display_h}時{m}分" if m else f"{period}{display_h}時"
        except Exception:
            time_str = str(todo_time)

    todo_type = hearing.get("todo_type", "開庭")
    type_labels = {"開庭": "庭前準備摘要", "補正": "補正期限提醒", "繳費": "繳費期限提醒"}
    date_labels = {"開庭": "開庭日期", "補正": "補正期限", "繳費": "繳費期限"}
    title = type_labels.get(todo_type, f"{todo_type}提醒")
    date_label = date_labels.get(todo_type, "日期")

    lines = [
        f"📋 {title}",
        f"━━━━━━━━━━━━━━━━━━",
        f"📅 {date_label}：{todo_date} {time_str}",
    ]
    if court_name:
        lines.append(f"🏛 法院：{court_name}")
    if court_case_no:
        lines.append(f"📁 案號：{court_case_no}")
    if case_reason:
        lines.append(f"📌 案由：{case_reason}")
    if client_name:
        lines.append(f"👤 當事人：{client_name}")
    if description:
        lines.append(f"📝 備註：{description}")

    # 嘗試從判決收集器取得相關見解
    related_insights = _fetch_related_judgments(case_reason, court_name)
    if related_insights:
        lines.append("")
        lines.append("📚 相關裁判見解：")
        for i, insight in enumerate(related_insights[:3], 1):
            lines.append(f"  {i}. {insight}")

    return "\n".join(lines)


_DEGRADED_MARKERS = (
    "系統降級回覆", "降級摘要", "摘要失敗", "逾時", "timeout",
    "模型忙碌", "請稍後再試",
    "預覽片段", "前 20 行預覽",
)


def _is_safe_summary(summary: str, case_reason: str) -> bool:
    """判斷摘要是否可信（非降級、非幻覺）。用於庭前準備，寧缺勿濫。"""
    if not summary or len(summary) < 50:
        return False
    # 降級標記
    if any(m in summary for m in _DEGRADED_MARKERS):
        return False
    # 必須包含結構化摘要的關鍵區塊
    has_structure = any(k in summary for k in ("裁判要旨", "法院見解", "爭點", "適用法條"))
    if not has_structure:
        return False
    # 幻覺偵測：摘要裡有完全不同的案由
    if case_reason:
        reason_norm = case_reason.replace(" ", "")
        import re as _re
        other = _re.search(r"裁判案由[：:]\s*(.+)", summary)
        if other:
            found = other.group(1).strip().replace(" ", "")
            if found and reason_norm not in found and found not in reason_norm:
                return False
    return True


def _fetch_related_judgments(case_reason: str, court_name: str) -> List[str]:
    """從 judgments.json 找出與案由相關的可信見解摘要。只取通過品質檢查的 LLM 摘要。"""
    if not case_reason:
        return []
    jdg_path = _MAGI_ROOT / "skills" / "judgment-collector" / "judgments.json"
    if not jdg_path.exists():
        return []
    try:
        with open(jdg_path, "r", encoding="utf-8") as f:
            judgments = json.load(f)
        if not isinstance(judgments, list):
            return []

        related = []
        reason_lower = case_reason.lower()
        for j in judgments:
            # 只取 LLM 摘要，排除 preview 類型
            if j.get("summary_type") not in (None, "llm"):
                continue
            j_reason = str(j.get("case_reason") or j.get("案由") or "").lower()
            if reason_lower in j_reason or j_reason in reason_lower:
                summary = j.get("summary") or j.get("裁判要旨") or ""
                if not _is_safe_summary(summary, case_reason):
                    continue
                # 取「裁判要旨」區塊（最精煉的一行）
                short = _extract_key_holding(summary, max_chars=120)
                title = j.get("title") or j.get("案號") or ""
                related.append(f"[{title}] {short}" if title else short)
        return related[:5]
    except Exception:
        return []


def _extract_key_holding(summary: str, max_chars: int = 120) -> str:
    """從結構化摘要中提取「裁判要旨」段落，比盲截前 100 字更精準。"""
    import re as _re
    # 嘗試提取 ## 裁判要旨 到下一個 ## 之間的內容
    m = _re.search(r"(?:##\s*裁判要旨|裁判要旨)\s*\n(.*?)(?=\n##|\Z)", summary, _re.DOTALL)
    if m:
        text = m.group(1).strip().replace("\n", " ")
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return text
    # fallback: 取前 max_chars 字
    short = summary[:max_chars].replace("\n", " ")
    if len(summary) > max_chars:
        short += "…"
    return short


# ── 主要任務 ────────────────────────────────────────────────────────
def task_scan(days_ahead: int = 7) -> Dict[str, Any]:
    """掃描未來 N 天開庭排程，回傳 JSON。"""
    try:
        conn = _get_conn()
        hearings = _fetch_upcoming_hearings(conn, days_ahead=days_ahead)
        conn.close()
        return {
            "success": True,
            "count": len(hearings),
            "days_ahead": days_ahead,
            "hearings": hearings,
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}", "hearings": []}


def task_remind(notify: bool = True) -> Dict[str, Any]:
    """發送開庭提醒：前一天提醒 + 當天提醒。"""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    state = _load_remind_state()
    sent_keys = set(state.get("sent", []))

    try:
        conn = _get_conn()
        hearings = _fetch_upcoming_hearings(conn, days_ahead=7)
        conn.close()
    except Exception as e:
        return {"success": False, "error": str(e), "sent": 0}

    sent_count = 0
    new_sent_keys = list(sent_keys)

    for h in hearings:
        h_date_str = str(h.get("todo_date", ""))
        try:
            h_date = date.fromisoformat(h_date_str)
        except Exception:
            continue

        todo_id = h.get("id", 0)
        todo_type = h.get("todo_type", "開庭")
        days_until = (h_date - today).days

        is_deadline = todo_type in ("補正", "繳費")

        if is_deadline:
            # ── 繳費/補正：每天推播，從入庫到截止日（含當天）──
            today_str = today.isoformat()
            key = f"daily_{todo_id}_{today_str}"
            if not _is_remind_key_sent(key, sent_keys) and days_until >= 0:
                prep = _generate_prep_summary(h)
                if days_until == 0:
                    header = f"🚨 今日{todo_type}截止"
                    sev = "critical"
                elif days_until == 1:
                    header = f"⚠️ 明天{todo_type}截止"
                    sev = "warning"
                elif days_until <= 3:
                    header = f"⚠️ {todo_type}期限倒數 {days_until} 天"
                    sev = "warning"
                else:
                    header = f"📌 {todo_type}期限倒數 {days_until} 天"
                    sev = "info"
                msg = f"{header}\n\n{prep}\n\n💡 已完成請回覆：「XX案繳了」或「XX案補正了」"
                if notify:
                    _send_reminder(msg, severity=sev)
                new_sent_keys.append(key)
                sent_count += 1
        else:
            # ── 開庭：前一天 + 當天提醒 ──
            if days_until == 1:
                key = f"eve_{todo_id}_{h_date_str}"
                if not _is_remind_key_sent(key, sent_keys):
                    prep = _generate_prep_summary(h)
                    msg = f"⏰ 明天開庭提醒\n\n{prep}"
                    if notify:
                        _send_reminder(msg, severity="info")
                    new_sent_keys.append(key)
                    sent_count += 1

            if days_until == 0:
                key = f"day_{todo_id}_{h_date_str}"
                if not _is_remind_key_sent(key, sent_keys):
                    prep = _generate_prep_summary(h)
                    msg = f"🔔 今日開庭提醒\n\n{prep}"
                    if notify:
                        _send_reminder(msg, severity="warning")
                    new_sent_keys.append(key)
                    sent_count += 1

    # 清理超過 14 天的舊 key
    cutoff = (today - timedelta(days=14)).isoformat()
    cleaned = [k for k in new_sent_keys if not k.split("_")[-1] < cutoff]
    state["sent"] = cleaned
    state["last_run"] = datetime.now().isoformat()
    _save_remind_state(state)

    return {"success": True, "sent": sent_count, "upcoming": len(hearings)}


def task_prep(case_number: str) -> str:
    """生成特定案件的庭前準備摘要。"""
    try:
        conn = _get_conn()
        hearings = _fetch_upcoming_hearings(conn, days_ahead=30)
        conn.close()
    except Exception as e:
        return f"❌ 資料庫連線失敗：{e}"

    q = case_number.lower()
    matched = [h for h in hearings if q in str(h.get("case_number", "")).lower()]
    # fallback: 用當事人姓名或法院案號搜尋
    if not matched:
        matched = [
            h for h in hearings
            if q in str(h.get("client_name", "")).lower()
            or q in str(h.get("court_case_number", "")).lower()
        ]
    if not matched:
        return f"找不到「{case_number}」的近期排程（已搜尋案號、當事人、法院案號）。"

    parts = []
    for h in matched:
        parts.append(_generate_prep_summary(h))
    return "\n\n".join(parts)


def task_list() -> str:
    """列出所有待開庭排程（人類可讀格式）。"""
    result = task_scan(days_ahead=30)
    hearings = result.get("hearings", [])
    if not hearings:
        return "📅 近 30 天內沒有排程（開庭/補正/繳費）。"

    type_counts = {}
    for h in hearings:
        t = h.get("todo_type", "開庭")
        type_counts[t] = type_counts.get(t, 0) + 1
    count_str = "、".join(f"{t}{n}件" for t, n in type_counts.items())
    lines = [f"📅 近期排程（共 {len(hearings)} 件：{count_str}）", ""]
    type_icons = {"開庭": "⚖️", "補正": "📝", "繳費": "💰"}
    for h in hearings:
        todo_date = h.get("todo_date", "")
        todo_time = h.get("todo_time", "")
        todo_type = h.get("todo_type", "開庭")
        client = h.get("client_name", "")
        court = h.get("court_name", "")
        case_no = h.get("court_case_number", "") or h.get("case_number", "")
        reason = h.get("case_reason", "")
        desc = h.get("description", "")

        time_display = ""
        if todo_time:
            try:
                parts = str(todo_time).split(":")
                h_val, m_val = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
                period = "上午" if h_val < 12 else "下午"
                dh = h_val if h_val <= 12 else h_val - 12
                time_display = f" {period}{dh}:{m_val:02d}"
            except Exception:
                time_display = f" {todo_time}"

        icon = type_icons.get(todo_type, "📌")
        line = f"{icon} {todo_date}{time_display} [{todo_type}]"
        if court:
            line += f" | {court}"
        if case_no:
            line += f" | {case_no}"
        if reason:
            line += f"（{reason}）"
        if client:
            line += f" | {client}"
        if desc:
            line += f" — {desc}"
        lines.append(line)

    return "\n".join(lines)


# ── 標記完成 ────────────────────────────────────────────────────────
def _mark_todo_completed(conn, todo_id: int) -> bool:
    """在 DB 中將指定 case_todos.id 標記為 completed。"""
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE case_todos SET status='completed', completed_date=NOW() WHERE id=%s",
            (todo_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()


def _match_pending_todos(query: str, todo_types: Optional[tuple] = None) -> List[Dict[str, Any]]:
    """從 DB 撈 pending 狀態的 todos，用 query 模糊比對案號/當事人/法院案號/案由。"""
    try:
        conn = _get_conn()
        hearings = _fetch_upcoming_hearings(conn, days_ahead=90, todo_types=todo_types)
        conn.close()
    except Exception:
        return []
    q = query.lower()
    return [
        h for h in hearings
        if q in str(h.get("case_number", "")).lower()
        or q in str(h.get("client_name", "")).lower()
        or q in str(h.get("court_case_number", "")).lower()
        or q in str(h.get("case_reason", "")).lower()
    ]


def task_done(query: str, notify: bool = True) -> str:
    """標記特定案件的繳費/補正為已完成。
    - 精確匹配 1 筆 → 直接完成
    - 匹配多筆 → 列出讓使用者指定
    - 匹配 0 筆 → 提示找不到
    """
    if not query or len(query.strip()) < 1:
        return "❌ 請指定案件，例如：「張國賢繳了」「補字第54號補正了」"

    # 清理常見的動詞後綴，保留案件識別用的關鍵字
    clean = query.strip()
    for suffix in ["繳了", "交了", "繳費了", "補正了", "完成了", "已繳", "已補正", "已交", "已完成",
                    "繳", "交", "的繳了", "的交了", "的補正了", "案繳了", "案交了", "案補正了"]:
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)].strip()
            break
    # 也清理前綴
    for prefix in ["幫我關掉", "關掉", "取消提醒", "關閉"]:
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
            break

    if not clean:
        return "❌ 請指定案件名稱或案號，例如：「張國賢繳了」「補字第54號交了」"

    matched = _match_pending_todos(clean, todo_types=("繳費", "補正"))

    if len(matched) == 0:
        # 如果搜不到，擴大到全部類型（含開庭）
        matched = _match_pending_todos(clean)
        if len(matched) == 0:
            return f"❌ 找不到「{clean}」的待辦排程。目前待辦：\n\n{task_list()}"

    if len(matched) == 1:
        h = matched[0]
        todo_id = h.get("id", 0)
        try:
            conn = _get_conn()
            ok = _mark_todo_completed(conn, todo_id)
            conn.close()
        except Exception as e:
            return f"❌ 資料庫更新失敗：{e}"
        if ok:
            client = h.get("client_name", "")
            todo_type = h.get("todo_type", "")
            case_no = h.get("court_case_number", "") or h.get("case_number", "")
            todo_date = h.get("todo_date", "")
            label = f"{client or case_no}（{todo_type}，期限 {todo_date}）"
            msg = f"✅ 已標記完成：{label}\n提醒已關閉。"
            if notify:
                _send_reminder(msg, severity="info")
            return msg
        return "❌ 標記失敗，請確認案件 ID。"

    # 多筆匹配，列出讓使用者選
    lines = [f"⚠️ 「{clean}」匹配到 {len(matched)} 筆待辦，請指定更精確的名稱：", ""]
    for h in matched:
        client = h.get("client_name", "")
        todo_type = h.get("todo_type", "")
        case_no = h.get("court_case_number", "") or h.get("case_number", "")
        reason = h.get("case_reason", "")
        todo_date = h.get("todo_date", "")
        label = f"[{todo_type}] {todo_date}"
        if case_no:
            label += f" | {case_no}"
        if reason:
            label += f"（{reason}）"
        if client:
            label += f" | {client}"
        lines.append(f"• {label}")
    lines.append("")
    lines.append("💡 請用更精確的名稱回覆，例如：「張國賢繳了」「補字第54號交了」")
    return "\n".join(lines)


# ── 跨案件 pattern detection ───────────────────────────────────────
def task_patterns(query: str = "") -> str:
    """
    查詢同一對造、同一法院、同一案由的歷史案件關聯。
    自動偵測重複對手方和常見案由組合。
    """
    try:
        conn = _get_conn()
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT case_number, client_name, case_type, case_reason,
                   court_name, status, opponent_name,
                   COALESCE(NULLIF(court_case_no, ''), court_case_number, '') AS court_case_number
            FROM cases
            ORDER BY case_number DESC
            LIMIT 500
            """,
        )
        cases = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        return f"❌ 資料庫連線失敗：{e}"

    if not cases:
        return "📊 案件庫為空。"

    # 依對造分組
    opponent_groups: dict = {}
    # 依案由分組
    reason_groups: dict = {}
    # 依法院分組
    court_groups: dict = {}

    for c in cases:
        opponent = str(c.get("opponent_name") or "").strip()
        reason = str(c.get("case_reason") or "").strip()
        court = str(c.get("court_name") or "").strip()

        if opponent:
            if opponent not in opponent_groups:
                opponent_groups[opponent] = []
            opponent_groups[opponent].append(c)

        if reason:
            if reason not in reason_groups:
                reason_groups[reason] = []
            reason_groups[reason].append(c)

        if court:
            if court not in court_groups:
                court_groups[court] = []
            court_groups[court].append(c)

    lines = ["📊 跨案件 Pattern Detection", "━━━━━━━━━━━━━━━━━━", ""]

    # 如果有查詢，過濾結果
    if query:
        q = query.lower()
        # 搜尋對造
        found_opponents = {k: v for k, v in opponent_groups.items() if q in k.lower()}
        # 搜尋案由
        found_reasons = {k: v for k, v in reason_groups.items() if q in k.lower()}
        # 搜尋當事人
        found_clients = {}
        for c in cases:
            client = str(c.get("client_name") or "").lower()
            if q in client:
                name = c.get("client_name", "")
                if name not in found_clients:
                    found_clients[name] = []
                found_clients[name].append(c)

        if found_opponents:
            lines.append(f"🔍 對造「{query}」的歷史案件：")
            for opp, cs in found_opponents.items():
                lines.append(f"\n  👤 {opp}（{len(cs)} 件）")
                for c in cs[:10]:
                    status = c.get("status", "")
                    lines.append(f"    • {c.get('case_number', '')} {c.get('client_name', '')} "
                                 f"| {c.get('case_reason', '')} | {status}")

        if found_reasons:
            lines.append(f"\n🔍 案由含「{query}」的案件：")
            for reason, cs in found_reasons.items():
                lines.append(f"\n  📌 {reason}（{len(cs)} 件）")
                for c in cs[:5]:
                    lines.append(f"    • {c.get('case_number', '')} {c.get('client_name', '')} "
                                 f"| {c.get('court_name', '')}")

        if found_clients:
            lines.append(f"\n🔍 當事人含「{query}」的案件：")
            for client, cs in found_clients.items():
                lines.append(f"\n  👤 {client}（{len(cs)} 件）")
                for c in cs[:10]:
                    lines.append(f"    • {c.get('case_number', '')} | {c.get('case_reason', '')} "
                                 f"| {c.get('court_name', '')} | {c.get('status', '')}")

        if not found_opponents and not found_reasons and not found_clients:
            lines.append(f"找不到「{query}」的相關案件。")

        return "\n".join(lines)

    # 無查詢時，顯示整體 pattern
    # 重複對造（>= 2 件）
    repeat_opponents = {k: v for k, v in opponent_groups.items() if len(v) >= 2}
    if repeat_opponents:
        lines.append("👥 重複對造（≥ 2 件）：")
        for opp, cs in sorted(repeat_opponents.items(), key=lambda x: -len(x[1]))[:10]:
            reasons = set(str(c.get("case_reason", "")) for c in cs if c.get("case_reason"))
            lines.append(f"  {opp}: {len(cs)} 件（案由：{', '.join(reasons)}）")
        lines.append("")

    # 常見案由分布
    top_reasons = sorted(reason_groups.items(), key=lambda x: -len(x[1]))[:10]
    if top_reasons:
        lines.append("📊 案由分布 Top 10：")
        for reason, cs in top_reasons:
            active = sum(1 for c in cs if c.get("status") not in ("已結案", "結案"))
            lines.append(f"  {reason}: {len(cs)} 件（進行中 {active}）")
        lines.append("")

    # 法院分布
    top_courts = sorted(court_groups.items(), key=lambda x: -len(x[1]))[:8]
    if top_courts:
        lines.append("🏛 法院分布：")
        for court, cs in top_courts:
            lines.append(f"  {court}: {len(cs)} 件")
        lines.append("")

    lines.append(f"📈 總案件數：{len(cases)}（含已結案）")

    return "\n".join(lines)


# ── 案件時程 dashboard ─────────────────────────────────────────────
def task_dashboard() -> str:
    """一次看所有案件的下次期日、補正期限、繳費期限，按日期排序。"""
    try:
        conn = _get_conn()
        cur = conn.cursor(dictionary=True)
        # 取得所有未完成的 case_todos，不限天數
        cur.execute(
            """
            SELECT
                ct.id, ct.case_number, ct.client_name, ct.todo_type,
                ct.todo_date, ct.todo_time, ct.description, ct.status,
                COALESCE(c.court_name, '') AS court_name,
                COALESCE(c.case_reason, '') AS case_reason,
                COALESCE(NULLIF(c.court_case_no, ''), c.court_case_number, '') AS court_case_number
            FROM case_todos ct
            LEFT JOIN cases c
              ON c.case_number COLLATE utf8mb4_unicode_ci
               = ct.case_number COLLATE utf8mb4_unicode_ci
            WHERE (ct.status IS NULL OR ct.status IN ('', 'pending'))
              AND ct.todo_date >= %s
            ORDER BY ct.todo_date ASC, ct.todo_time ASC
            """,
            (date.today().isoformat(),),
        )
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        return f"❌ 資料庫連線失敗：{e}"

    if not rows:
        return "📊 目前沒有待辦排程。"

    today = date.today()
    type_icons = {"開庭": "⚖️", "補正": "📝", "繳費": "💰"}

    # 依日期分組
    date_groups: dict = {}
    for r in rows:
        d = str(r.get("todo_date", ""))
        if d not in date_groups:
            date_groups[d] = []
        date_groups[d].append(r)

    type_counts = {}
    for r in rows:
        t = r.get("todo_type", "開庭")
        type_counts[t] = type_counts.get(t, 0) + 1
    count_str = "、".join(f"{t}{n}件" for t, n in type_counts.items())

    lines = [
        f"📊 案件時程總覽（共 {len(rows)} 件：{count_str}）",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for d, items in date_groups.items():
        try:
            d_date = date.fromisoformat(d)
            days_until = (d_date - today).days
            if days_until == 0:
                day_label = "🔴 今天"
            elif days_until == 1:
                day_label = "🟡 明天"
            elif days_until <= 3:
                day_label = f"🟡 {days_until}天後"
            elif days_until <= 7:
                day_label = f"🟢 {days_until}天後"
            else:
                day_label = f"⚪ {days_until}天後"
        except Exception:
            day_label = ""

        lines.append(f"📅 {d} ({day_label})")
        for r in items:
            todo_type = r.get("todo_type", "開庭")
            icon = type_icons.get(todo_type, "📌")
            client = r.get("client_name", "")
            court = r.get("court_name", "")
            case_no = r.get("court_case_number", "") or r.get("case_number", "")
            reason = r.get("case_reason", "")
            time_str = ""
            if r.get("todo_time"):
                try:
                    parts = str(r["todo_time"]).split(":")
                    time_str = f" {int(parts[0])}:{int(parts[1]):02d}"
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 792, exc_info=True)
            line = f"  {icon} [{todo_type}]{time_str}"
            if court:
                line += f" {court}"
            if case_no:
                line += f" {case_no}"
            if reason:
                line += f"（{reason}）"
            if client:
                line += f" | {client}"
            lines.append(line)
        lines.append("")

    # 本週摘要
    this_week = [r for r in rows if _days_until(r) is not None and _days_until(r) <= 7]
    urgent = [r for r in rows if _days_until(r) is not None and _days_until(r) <= 3]
    if urgent:
        lines.append(f"⚠️ 3天內有 {len(urgent)} 件待辦需注意！")
    elif this_week:
        lines.append(f"📌 本週有 {len(this_week)} 件待辦。")

    return "\n".join(lines)


def _days_until(row: dict) -> Optional[int]:
    try:
        d = date.fromisoformat(str(row.get("todo_date", "")))
        return (d - date.today()).days
    except Exception:
        return None


# ── 庭前準備 checklist ──────────────────────────────────────────────
_CHECKLIST_BY_TYPE = {
    "民事": {
        "通用": [
            "□ 確認開庭通知書（時間、法庭、股別）",
            "□ 攜帶委任狀正本（如未提出）",
            "□ 攜帶身分證正本（核對用）",
            "□ 攜帶相關證物正本（供法院核對影本）",
            "□ 確認繳費狀態（裁判費、保全費）",
        ],
        "準備程序": [
            "□ 準備爭點整理狀",
            "□ 確認不爭執事項",
            "□ 準備證據清單",
            "□ 確認證人名單及傳喚狀態",
        ],
        "言詞辯論": [
            "□ 準備言詞辯論意旨狀",
            "□ 確認最後爭點（準備程序筆錄）",
            "□ 準備最終聲明",
        ],
        "調解": [
            "□ 確認調解方案底線",
            "□ 準備和解條件草案",
            "□ 確認當事人出席（本人或代理）",
        ],
    },
    "刑事": {
        "通用": [
            "□ 確認開庭通知書（時間、法庭、股別）",
            "□ 攜帶委任狀正本",
            "□ 攜帶被告身分證（如需）",
            "□ 確認起訴書/上訴書內容",
            "□ 確認是否需要通譯",
        ],
        "準備程序": [
            "□ 準備答辯狀",
            "□ 確認證據能力爭議",
            "□ 聲請調查證據",
        ],
        "審判": [
            "□ 準備辯護要旨",
            "□ 確認證人到庭狀態",
            "□ 準備交互詰問大綱",
            "□ 準備科刑辯論意見",
        ],
    },
    "行政": {
        "通用": [
            "□ 確認開庭通知書",
            "□ 攜帶委任狀正本",
            "□ 確認訴願決定書",
            "□ 確認行政處分內容",
        ],
        "準備程序": [
            "□ 準備爭點整理",
            "□ 確認管轄權無爭議",
            "□ 準備證據資料",
        ],
    },
    "勞動": {
        "通用": [
            "□ 確認開庭通知書",
            "□ 攜帶委任狀正本",
            "□ 確認勞動調解前置程序是否完成",
        ],
        "調解": [
            "□ 確認調解方案",
            "□ 準備薪資明細/出勤紀錄",
            "□ 準備勞動契約/規則",
            "□ 確認資遣費/特休計算",
        ],
    },
}


def task_checklist(case_number: str = "") -> str:
    """依案件類型自動生成庭前準備 checklist。"""
    try:
        conn = _get_conn()
        hearings = _fetch_upcoming_hearings(conn, days_ahead=30)
        conn.close()
    except Exception as e:
        return f"❌ 資料庫連線失敗：{e}"

    if case_number:
        q = case_number.lower()
        hearings = [
            h for h in hearings
            if q in str(h.get("case_number", "")).lower()
            or q in str(h.get("client_name", "")).lower()
            or q in str(h.get("court_case_number", "")).lower()
        ]

    if not hearings:
        return f"📋 找不到「{case_number or ''}」的近期排程。" if case_number else "📋 近期沒有排程。"

    parts = []
    for h in hearings:
        todo_type = h.get("todo_type", "開庭")
        if todo_type != "開庭":
            continue

        case_type = str(h.get("case_type", "") or "")
        case_reason = str(h.get("case_reason", "") or "")
        court_case_no = h.get("court_case_number", "") or h.get("case_number", "")
        client = h.get("client_name", "")
        todo_date = h.get("todo_date", "")
        description = str(h.get("description", "") or "").lower()

        # 判斷案件類型
        detected_type = "民事"  # default
        if any(k in case_type for k in ("刑", "少年")):
            detected_type = "刑事"
        elif any(k in case_type for k in ("行政",)):
            detected_type = "行政"
        elif any(k in case_reason for k in ("勞動", "勞基", "資遣", "工資", "解僱")):
            detected_type = "勞動"
        elif any(k in case_type for k in ("勞動",)):
            detected_type = "勞動"

        # 判斷程序階段
        stage = "通用"
        if any(k in description for k in ("準備", "整理", "爭點")):
            stage = "準備程序"
        elif any(k in description for k in ("辯論", "言詞辯論", "審判")):
            stage = "言詞辯論" if detected_type == "民事" else "審判"
        elif any(k in description for k in ("調解", "和解", "調處")):
            stage = "調解"

        checklist = _CHECKLIST_BY_TYPE.get(detected_type, _CHECKLIST_BY_TYPE["民事"])
        items = list(checklist.get("通用", []))
        if stage != "通用" and stage in checklist:
            items.extend(checklist[stage])

        lines = [
            f"📋 庭前準備 Checklist — {detected_type}（{stage}）",
            f"📅 {todo_date} | {court_case_no} | {client}",
            f"案由：{case_reason}",
            "━━━━━━━━━━━━━━━━━━",
        ]
        lines.extend(items)
        parts.append("\n".join(lines))

    if not parts:
        return "📋 近期沒有開庭排程（僅補正/繳費不產生 checklist）。"

    return "\n\n".join(parts)


# ── CLI ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="MAGI 開庭提醒技能")
    ap.add_argument("--task", default="list", choices=["scan", "remind", "prep", "list", "done", "checklist", "dashboard", "patterns", "help"])
    ap.add_argument("--days", type=int, default=7, help="掃描天數（默認 7）")
    ap.add_argument("--case-number", default="", help="案號（prep/done 任務用）")
    ap.add_argument("--notify", default="1", help="1=發送通知")
    ap.add_argument("--text", default="", help="自然語句（通訊軟體路由用）")
    args = ap.parse_args()

    task = str(args.task or "list").strip().lower()
    notify = str(args.notify or "1").strip().lower() in {"1", "true", "yes", "on"}

    if task == "help":
        print(json.dumps({
            "skill": "court-hearing-reminder",
            "tasks": ["scan", "remind", "prep", "list", "done", "checklist", "dashboard", "patterns"],
            "description": "開庭提醒、補正/繳費期限提醒、標記完成、庭前checklist、案件時程總覽、跨案件分析",
        }, ensure_ascii=False, indent=2))
        return 0

    if task == "scan":
        result = task_scan(days_ahead=args.days)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("success") else 1

    if task == "remind":
        result = task_remind(notify=notify)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("success") else 1

    if task == "prep":
        case_no = args.case_number or args.text
        if not case_no:
            print("❌ 請提供案號：--case-number XXX")
            return 1
        print(task_prep(case_no))
        return 0

    if task == "done":
        query = args.text or args.case_number
        if not query:
            print("❌ 請指定案件，例如：--text '張國賢繳了'")
            return 1
        print(task_done(query, notify=notify))
        return 0

    if task == "checklist":
        case_no = args.case_number or args.text
        print(task_checklist(case_no))
        return 0

    if task == "dashboard":
        print(task_dashboard())
        return 0

    if task == "patterns":
        query = args.text or args.case_number
        print(task_patterns(query))
        return 0

    if task == "list":
        print(task_list())
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
