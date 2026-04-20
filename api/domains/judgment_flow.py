"""
Judgment collection & search operations extracted from Orchestrator.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`.
"""
from __future__ import annotations

import json
import importlib.util
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _get_local_db_manager() -> Optional[Any]:
    """Return a DB manager that can query the local judgment archive reliably."""
    osc_compat_path = os.path.join(_MAGI_ROOT, "osc.py")
    try:
        if os.path.isfile(osc_compat_path):
            spec = importlib.util.spec_from_file_location("magi_osc_compat", osc_compat_path)
            if spec and spec.loader:
                osc_compat = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(osc_compat)
                osc_db_manager = getattr(osc_compat, "DatabaseManager", None)
                if osc_db_manager is not None:
                    return osc_db_manager(
                        {
                            "host": os.environ.get("OSC_DB_HOST", "127.0.0.1"),
                            "port": int(os.environ.get("OSC_DB_PORT", "3307") or "3307"),
                            "user": os.environ.get("OSC_DB_USER", "python_user"),
                            "password": os.environ.get("OSC_DB_PASSWORD", ""),
                            "database": os.environ.get("OSC_DB_NAME", "law_firm_data"),
                        }
                    )
    except Exception as exc:
        logger.debug("osc compat db manager unavailable: %s", exc, exc_info=True)

    try:
        if _MAGI_ROOT not in sys.path:
            sys.path.insert(0, _MAGI_ROOT)
        from osc import DatabaseManager as OscDatabaseManager

        return OscDatabaseManager(
            {
                "host": os.environ.get("OSC_DB_HOST", "127.0.0.1"),
                "port": int(os.environ.get("OSC_DB_PORT", "3307") or "3307"),
                "user": os.environ.get("OSC_DB_USER", "python_user"),
                "password": os.environ.get("OSC_DB_PASSWORD", ""),
                "database": os.environ.get("OSC_DB_NAME", "law_firm_data"),
            }
        )
    except Exception as exc:
        logger.warning("local judgment DB manager unavailable: %s", exc)
        return None


def extract_judgment_collect_payload(message: str) -> tuple[Optional[dict], str]:
    text = str(message or "").strip()
    if not text:
        return None, "\U0001f50e \u8acb\u63d0\u4f9b\u6848\u7531\u6216\u6848\u865f\uff0c\u4f8b\u5982\uff1a`\u67e5\u5224\u6c7a \u50b7\u5bb3`\u3001`\u67e5\u5224\u6c7a 113\u5e74\u5ea6\u4e0a\u8a34\u5b57\u7b2c12\u865f`"

    raw = re.sub(r"^@MAGI\s*", "", text, flags=re.IGNORECASE).strip()
    for _ in range(3):
        prev = raw
        raw = re.sub(r"^(?:\u5e6b\u6211|\u8acb|\u9ebb\u7169|\u5e6b\u5fd9|\u53ef\u4ee5\u5e6b\u6211|\u5354\u52a9\u6211)\s*", "", raw).strip()
        raw = re.sub(
            r"^(?:\u67e5\u5224\u6c7a|\u627e\u5224\u6c7a|\u5224\u6c7a\u641c\u5c0b|\u641c\u5c0b\u5224\u6c7a|\u6536\u96c6\u5224\u6c7a|\u5224\u6c7a\u641c\u96c6|\u641c\u5c0b\u6700\u9ad8\u6cd5\u9662\u5224\u6c7a|\u5be6\u52d9\u898b\u89e3|\u6cd5\u5f8b\u898b\u89e3|\u6cd5\u9662\u898b\u89e3)\s*",
            "",
            raw,
        ).strip()
        raw = re.sub(r"^(?:\u67e5\u4e00\u4e0b|\u627e\u4e00\u4e0b|\u641c\u5c0b\u4e00\u4e0b|\u641c\u4e00\u4e0b)\s*", "", raw).strip()
        if raw == prev:
            break
    raw = raw.strip(" \uff1a:\uff0c,\u3002\uff1b;")

    case_match = re.search(
        r"(\d{4}-\d{4}|\d{2,3}\u5e74\u5ea6[^\s]{1,12}\u5b57\u7b2c?\d+\u865f?)",
        raw,
    )
    if case_match:
        return {"case_number": case_match.group(1).strip()}, ""

    reason = re.sub(r"^(?:\u6700\u8fd1\u7684?|\u6700\u65b0\u7684?|\u6700\u9ad8\u6cd5\u9662\u7684?|\u6cd5\u9662\u7684?)", "", raw).strip()
    reason = re.sub(r"(?:\u7684)?(?:\u6cd5\u9662)?\u5224\u6c7a$", "", reason).strip(" \uff1a:\uff0c,\u3002\uff1b;")
    reason = re.sub(r"\s+", " ", reason).strip()

    generic_only = {
        "\u6700\u8fd1", "\u6700\u65b0", "\u6cd5\u9662", "\u5224\u6c7a", "\u6700\u8fd1\u5224\u6c7a", "\u6700\u65b0\u5224\u6c7a",
        "\u6cd5\u9662\u5224\u6c7a", "\u6700\u8fd1\u6cd5\u9662\u5224\u6c7a", "\u6700\u8fd1\u7684\u6cd5\u9662\u5224\u6c7a",
        "\u6700\u65b0\u6cd5\u9662\u5224\u6c7a", "\u6700\u65b0\u7684\u6cd5\u9662\u5224\u6c7a", "\u6700\u9ad8\u6cd5\u9662\u5224\u6c7a",
    }
    if not reason or len(reason) < 2 or reason in generic_only:
        return None, "\U0001f50e \u8acb\u63d0\u4f9b\u6848\u7531\u6216\u6848\u865f\uff0c\u4f8b\u5982\uff1a`\u67e5\u5224\u6c7a \u50b7\u5bb3`\u3001`\u67e5\u5224\u6c7a 113\u5e74\u5ea6\u4e0a\u8a34\u5b57\u7b2c12\u865f`"
    return {"case_reason": reason}, ""


def format_judgment_collect_result(payload: dict) -> str:
    if not isinstance(payload, dict):
        return "\u274c \u5224\u6c7a\u641c\u5c0b\u5931\u6557\uff1a\u56de\u50b3\u683c\u5f0f\u7570\u5e38"
    if not payload.get("success"):
        err = str(payload.get("error") or "unknown").strip()
        return f"\u274c \u5224\u6c7a\u641c\u5c0b\u5931\u6557\uff1a{err}"

    reason = str(payload.get("case_reason") or payload.get("case_number") or "").strip()
    _reason_label = reason or "\u6848\u4ef6"
    lines = [f"\U0001f4da \u5224\u6c7a\u641c\u5c0b\u5b8c\u6210\uff1a{_reason_label}"]
    court_level = str(payload.get("court_level") or "").strip()
    if court_level:
        lines.append(f"\u6cd5\u9662\uff1a{court_level}")
    count = payload.get("count")
    if count is not None:
        lines.append(f"\u6536\u96c6\u7b46\u6578\uff1a{count}")

    LINE_MSG_BUDGET = 4500
    header_len = len("\n".join(lines)) + 2
    remaining = LINE_MSG_BUDGET - header_len

    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    for row in items:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        summary = str(row.get("summary_full") or row.get("summary_preview") or "").strip()
        is_degraded = row.get("is_degraded", False)

        entry_lines = [f"\n{'=' * 30}", f"\u3010{title[:80]}\u3011"]
        if row.get("url"):
            entry_lines.append(str(row["url"]))
        if summary and not is_degraded:
            if len(summary) > 600:
                summary = summary[:600] + "\u2026\uff08\u5b8c\u6574\u5167\u5bb9\u898b\u5831\u544a\uff09"
            entry_lines.append(summary)
        elif is_degraded and summary:
            entry_lines.append(f"[\u6458\u8981\u54c1\u8cea\u4e0d\u4f73\uff0c\u5f85\u91cd\u8a66]\n{summary[:200]}\u2026")
        else:
            entry_lines.append("[\u5c1a\u7121\u6458\u8981]")

        entry_text = "\n".join(entry_lines)
        if len(entry_text) > remaining:
            _shown = len([l for l in lines if l.startswith("\u3010")])
            lines.append(f"\n\u2026\u5176\u9918 {len(items) - _shown} \u7b46\u8acb\u898b\u5831\u544a\u6a94\u6848")
            break
        lines.append(entry_text)
        remaining -= len(entry_text)

    retry_queued_count = payload.get("retry_queued_count")
    if retry_queued_count:
        lines.append(f"\n\u6458\u8981\u91cd\u8a66\u4f47\u5217\uff1a+{retry_queued_count}")
    return "\n".join(lines)


def _run_skill_json(skill_script: str, task: str, timeout_sec: int) -> Dict[str, Any]:
    py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"
    proc = subprocess.run(
        [py, skill_script, "--task", task],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        cwd=_MAGI_ROOT,
        env=os.environ.copy(),
    )
    out = (proc.stdout or "").strip()
    err_text = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "error": (err_text or out or "unknown")[:280], "returncode": proc.returncode}
    if not out:
        return {"ok": False, "error": "empty_output", "returncode": proc.returncode}
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"ok": False, "error": out[:500], "returncode": proc.returncode}


def _is_practical_insight_request(message: str) -> bool:
    text = str(message or "")
    return any(keyword in text for keyword in ["實務見解", "法律見解", "法院見解"])


def _format_statute_items(items: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for item in items[:3]:
        source = str(item.get("source") or "")
        match = re.search(r"law=([^|]+)\|article=([^|]+)", source)
        law = match.group(1) if match else ""
        article = match.group(2) if match else ""
        content = str(item.get("content") or "").strip().replace("\n", " ")
        if len(content) > 120:
            content = content[:120] + "…"
        label = " ".join(part for part in [law, article] if part)
        lines.append(f"- {label or source}: {content}")
    return lines


def _search_local_judgment_archive(query: str, limit: int = 3) -> Dict[str, Any]:
    """本地實務見解庫 fallback（判決-搜尋）。

    2026-04-21 後：主查詢來源改為 `court_judgments`（與 OSC 面板同一張表）。
    `judgment_archive` 已由 `scripts/ops/merge_judgment_archive_to_court.py` 合併
    至 `court_judgments`；仍留的 662 筆無 jid 舊案作為 secondary fallback。
    """
    text = str(query or "").strip()
    if not text:
        return {"success": False, "error": "missing_query"}
    try:
        db = _get_local_db_manager()
        if db is None:
            return {"success": False, "error": "local_archive_db_unavailable"}
        like = f"%{text}%"
        limit_int = max(1, int(limit))

        # 主查詢：court_judgments（OSC 可見正式實務見解庫）
        rows = db.execute(
            """
            SELECT
                jid,
                court_name,
                case_number,
                case_type,
                judgment_date,
                LEFT(COALESCE(summary, ''), 1200) AS summary_text,
                source_url,
                crawled_at
            FROM court_judgments
            WHERE
                case_number LIKE %s
                OR summary LIKE %s
                OR full_text LIKE %s
                OR court_name LIKE %s
            ORDER BY crawled_at DESC
            LIMIT %s
            """,
            (like, like, like, like, limit_int),
            fetch="all",
        ) or []
    except Exception as exc:
        logger.warning("local judgment court_judgments fallback failed: %s", exc)
        return {"success": False, "error": f"local_archive_failed: {str(exc)[:160]}"}

    items: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        summary = str(row.get("summary_text") or "").strip()
        is_degraded = "系統降級回覆" in summary
        court_name = str(row.get("court_name") or "").strip()
        case_number = str(row.get("case_number") or "").strip()
        title_parts = [p for p in [court_name, case_number] if p]
        title = " ".join(title_parts) if title_parts else str(row.get("jid") or "").strip()
        items.append(
            {
                "title": title,
                "summary_preview": summary,
                "url": str(row.get("source_url") or "").strip(),
                "is_degraded": is_degraded,
                "source": "court_judgments_local",
            }
        )

    # Secondary fallback: 662 筆無 jid 舊 judgment_archive（merge 時無法正規化）
    if not items:
        try:
            db = _get_local_db_manager()
            if db is not None:
                like = f"%{text}%"
                legacy_rows = db.execute(
                    """
                    SELECT
                        judgment_title,
                        judgment_url,
                        LEFT(summary_text, 1200) AS summary_text,
                        case_reason,
                        crawled_at
                    FROM judgment_archive
                    WHERE
                        (source_jid IS NULL OR source_jid = '')
                        AND (
                            case_reason LIKE %s
                            OR summary_text LIKE %s
                            OR judgment_title LIKE %s
                        )
                    ORDER BY crawled_at DESC
                    LIMIT %s
                    """,
                    (like, like, like, max(1, int(limit))),
                    fetch="all",
                ) or []
                for row in legacy_rows:
                    if not isinstance(row, dict):
                        continue
                    summary = str(row.get("summary_text") or "").strip()
                    is_degraded = "系統降級回覆" in summary
                    items.append(
                        {
                            "title": str(row.get("judgment_title") or "").strip(),
                            "summary_preview": summary,
                            "url": str(row.get("judgment_url") or "").strip(),
                            "is_degraded": is_degraded,
                            "source": "judgment_archive_legacy",
                        }
                    )
        except Exception as exc:
            logger.debug("legacy judgment_archive secondary fallback failed: %s", exc)

    items = [item for item in items if item.get("title")]
    non_degraded_items = [item for item in items if not item.get("is_degraded")]
    degraded_items = [item for item in items if item.get("is_degraded")]
    items = non_degraded_items[: max(1, int(limit))] or degraded_items[: max(1, int(limit))]
    if not items:
        return {"success": False, "error": "no_local_archive_matches"}
    return {"success": True, "source_label": "本地實務見解庫", "items": items[: max(1, int(limit))]}


def format_practical_insight_result(query: str, judgments: Dict[str, Any], statutes: Dict[str, Any]) -> str:
    lines = [f"📚 實務見解整理：{query}"]

    statute_items = statutes.get("items") if isinstance(statutes.get("items"), list) else []
    if statute_items:
        lines.append("\n【適用法規】")
        lines.extend(_format_statute_items(statute_items))
    elif statutes.get("error"):
        lines.append(f"\n【適用法規】\n- 查詢失敗：{statutes.get('error')}")

    if judgments.get("success"):
        source_label = str(judgments.get("source_label") or "").strip()
        if source_label:
            lines.append(f"\n【相關判決／法院見解】（{source_label}）")
        else:
            lines.append("\n【相關判決／法院見解】")
        items = judgments.get("items") if isinstance(judgments.get("items"), list) else []
        for row in items[:3]:
            title = str(row.get("title") or "").strip()
            summary = str(row.get("summary_full") or row.get("summary_preview") or "").strip()
            if len(summary) > 180:
                summary = summary[:180] + "…"
            if title:
                lines.append(f"- {title}")
            if summary:
                lines.append(f"  {summary}")
            url = str(row.get("url") or "").strip()
            if url:
                lines.append(f"  {url}")
    else:
        lines.append(f"\n【相關判決／法院見解】\n- 查詢失敗：{judgments.get('error') or 'unknown'}")
    return "\n".join(lines)


def run_practical_insight_command(orch, message: str, notify: bool = False) -> str:
    payload, err = extract_judgment_collect_payload(message)
    if not payload:
        return err

    query = str(payload.get("case_reason") or payload.get("case_number") or "").strip()
    judgment_script = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
    statutes_script = f"{_MAGI_ROOT}/skills/statutes-vdb/action.py"
    if not os.path.exists(judgment_script):
        return "❌ 找不到實務見解判決來源。"
    if not os.path.exists(statutes_script):
        return "❌ 找不到法規查詢來源。"

    judgment_payload = {
        **payload,
        "max_results": int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "6") or "6"),
        "headless": True,
        "save_to_db": True,
        "notify": bool(notify),
    }
    judgments = _run_skill_json(
        judgment_script,
        "collect " + json.dumps(judgment_payload, ensure_ascii=False),
        timeout_sec=int(os.environ.get("MAGI_JUDGMENT_CHAT_TIMEOUT_SEC", "180") or "180"),
    )
    if (not judgments.get("success")) or (not isinstance(judgments.get("items"), list)) or (not judgments.get("items")):
        fallback = _search_local_judgment_archive(query, limit=3)
        if fallback.get("success"):
            judgments = fallback
    statutes = _run_skill_json(
        statutes_script,
        "search " + json.dumps({"query": query, "top_k": 5}, ensure_ascii=False),
        timeout_sec=int(os.environ.get("MAGI_STATUTE_CHAT_TIMEOUT_SEC", "90") or "90"),
    )
    return format_practical_insight_result(query, judgments, statutes)


def run_judgment_collector_command(orch, message: str, notify: bool = False) -> str:
    if _is_practical_insight_request(message):
        return run_practical_insight_command(orch, message, notify=notify)
    payload, err = extract_judgment_collect_payload(message)
    if not payload:
        return err

    py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"
    skill_script = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
    if not os.path.exists(skill_script):
        return "\u274c \u627e\u4e0d\u5230\u5224\u6c7a\u641c\u5c0b skill\u3002"

    payload = {
        **payload,
        "max_results": int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "12") or "12"),
        "headless": True,
        "save_to_db": True,
        "notify": bool(notify),
    }
    task = "collect " + json.dumps(payload, ensure_ascii=False)
    try:
        data = _run_skill_json(
            skill_script,
            task,
            timeout_sec=int(os.environ.get("MAGI_JUDGMENT_CHAT_TIMEOUT_SEC", "180") or "180"),
        )
    except Exception as e:
        return f"\u274c \u5224\u6c7a\u641c\u5c0b\u932f\u8aa4\uff1a{e}"
    if not isinstance(data, dict):
        return str(data)[:1500]
    if not data.get("success"):
        return f"\u274c \u5224\u6c7a\u641c\u5c0b\u5931\u6557\uff1a{str(data.get('error') or 'unknown')[:280]}"
    return format_judgment_collect_result(data)


def run_judgment_trend_command(orch, message: str) -> str:
    """Execute judgment trend analysis."""
    py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"
    skill_script = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
    if not os.path.exists(skill_script):
        return "\u274c \u627e\u4e0d\u5230\u5224\u6c7a\u641c\u5c0b skill\u3002"
    case_reason = ""
    for prefix in ["\u5224\u6c7a\u8da8\u52e2", "\u8da8\u52e2\u5206\u6790", "\u6848\u7531\u5206\u6790", "\u5224\u6c7a\u5206\u6790"]:
        if prefix in message:
            case_reason = message.split(prefix)[-1].strip()
            break
    payload = {}
    if case_reason:
        payload["case_reason"] = case_reason
    task = "trend_analysis " + json.dumps(payload, ensure_ascii=False) if payload else "trend_analysis"
    try:
        proc = subprocess.run(
            [py, skill_script, "--task", task],
            capture_output=True, text=True, timeout=30,
            cwd=_MAGI_ROOT, env=os.environ.copy(),
        )
        return (proc.stdout or "").strip()[:2000] or "\u274c \u8da8\u52e2\u5206\u6790\u7121\u8f38\u51fa"
    except Exception as e:
        return f"\u274c \u8da8\u52e2\u5206\u6790\u932f\u8aa4\uff1a{e}"
