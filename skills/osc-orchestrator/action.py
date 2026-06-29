#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
osc-orchestrator/action.py

Headless OSC automation:
- Parse todos from filenames (doc received date = filename YYYYMMDD).
- Write to MariaDB (Casper local by default), or degrade to a pending queue.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path, get_config_path, get_orch_dir, get_skill_python
from api.case_path_mapper import local_case_path_candidates, preferred_case_roots, translate_local_path_to_canonical
from skills.bridge.shared_utils.judgment_folder_names import JUDGMENT_FOLDER_LABEL, path_has_judgment_folder
from scripts.ops.token_health_check import google_token_file_lock

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_QUEUE_PATH = os.path.join(SKILL_DIR, "_pending_todos.jsonl")
PENDING_QUEUE_TMP_PATH = os.path.join(SKILL_DIR, "._pending_todos.tmp")
DEADLETTER_PATH = os.path.join(SKILL_DIR, "_pending_todos.deadletter.jsonl")

CODE_ROOT = str(get_orch_dir())
VENV_PY = str(get_skill_python())

_LS_BIN = "/bin/ls"
_TEST_BIN = "/bin/test"


def _write_token_atomic(path: str | Path, text: str) -> None:
    token_path = Path(path).expanduser()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(token_path.parent), delete=False) as tmp:
            try:
                os.fchmod(tmp.fileno(), 0o600)
            except Exception:
                pass
            tmp.write(text)
            tmp.flush()
            try:
                os.fsync(tmp.fileno())
            except Exception:
                pass
            tmp_path = Path(tmp.name)
        os.replace(str(tmp_path), token_path)
        try:
            token_path.chmod(0o600)
        except Exception:
            pass
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise

def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    """
    Best-effort：將 OSC headless 的關鍵入庫/佇列事件寫入向量記憶，便於日後追溯。
    """
    try:
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags=tags or {}, source="osc_orchestrator")
    except Exception:
        return


def _maybe_reexec_venv() -> None:
    if os.environ.get("OSC_ORCH_NO_VENV", "").strip() == "1":
        return
    if os.path.exists(VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(VENV_PY):
        os.execv(VENV_PY, [VENV_PY] + sys.argv)


def _json_load_maybe(s: str) -> Any:
    s = (s or "").strip()
    if not s:
        return {}
    if s.startswith("{") or s.startswith("["):
        return json.loads(s)
    return {"path": s}

def _listdir_timeout(path: str, *, timeout_sec: int = 8) -> List[str]:
    """
    Synology Drive/CloudStorage 上偶爾會卡死在 os.listdir()/os.scandir()。
    用系統 ls + timeout 略過卡住的資料夾，避免整個巡檢被拖垮。
    """
    p = (path or "").strip()
    if not p:
        return []
    if not os.path.isdir(p):
        return []
    try:
        # -1: one entry per line; avoid color/extra formatting
        r = subprocess.run(
            [_LS_BIN, "-1", p],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
        )
        if r.returncode != 0:
            return []
        out = (r.stdout or "").splitlines()
        return [x for x in (s.strip() for s in out) if x]
    except subprocess.TimeoutExpired:
        return []
    except Exception:
        return []


def _extract_case_number_from_text(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"(\d{4}-\d{4})", s)
    return m.group(1) if m else ""


def _extract_case_number_from_path(path: str) -> str:
    p = os.path.abspath(path or "")
    parts = p.split(os.sep)
    for part in reversed(parts):
        cn = _extract_case_number_from_text(part)
        if cn:
            return cn
    return ""


_GCAL_CLOSED_STATUSES = {"已結案", "結案", "closed", "Closed", "CLOSED"}
_GCAL_HISTORY_CUTOFF_ENV = "OSC_EVENTS_REFRESH_HISTORY_CUTOFF_DATE"
_GCAL_DEFAULT_HISTORY_CUTOFF_DATE = "2026-01-01"


def _gcal_history_cutoff_date(payload: Optional[Dict[str, Any]] = None) -> datetime:
    raw = ""
    if isinstance(payload, dict):
        raw = str(payload.get("history_cutoff_date") or "").strip()
    raw = raw or os.environ.get(_GCAL_HISTORY_CUTOFF_ENV) or _GCAL_DEFAULT_HISTORY_CUTOFF_DATE
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except Exception:
        return datetime.strptime(_GCAL_DEFAULT_HISTORY_CUTOFF_DATE, "%Y-%m-%d")


def _gcal_event_start_date(event: Dict[str, Any]) -> Optional[datetime]:
    try:
        start = event.get("start") or {}
        raw = start.get("date") or str(start.get("dateTime") or "")[:10]
        if not raw:
            return None
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    except Exception:
        return None


def _gcal_norm_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("臺", "台")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,。；;：:／/\\|_\-–—\[\]（）()【】<>《》「」『』]", "", text)
    return text


def _gcal_client_tokens(client_name: str) -> list[str]:
    raw = str(client_name or "").strip()
    if not raw:
        return []
    tokens: list[str] = []
    for item in (raw, raw.split()[0] if raw.split() else ""):
        n = _gcal_norm_text(item)
        if len(n) >= 2 and n not in tokens:
            tokens.append(n)
    for run in re.findall(r"[\u4e00-\u9fff○Ｏ]{2,12}", raw):
        n = _gcal_norm_text(run)
        if len(n) >= 2 and n not in tokens:
            tokens.append(n)
    return tokens


def _gcal_date_before_case_start(event_date: str, row: dict[str, Any]) -> bool:
    if not event_date:
        return False
    start = str(row.get("start_date") or row.get("approval_date") or "").strip()[:10]
    return bool(start and event_date[:10] < start)


def _lookup_gcal_case_by_number(conn: Any, case_number: str) -> tuple[str, str]:
    case_number = str(case_number or "").strip()
    if not case_number:
        return "", ""
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT case_number, client_name
                FROM cases
                WHERE case_number=%s
                   OR court_case_no=%s
                   OR court_case_number=%s
                   OR laf_case_no=%s
                   OR application_no=%s
                ORDER BY CASE WHEN case_number=%s THEN 0 ELSE 1 END, case_number DESC
                LIMIT 1
                """,
                (case_number, case_number, case_number, case_number, case_number, case_number),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        if row:
            return str(row.get("case_number") or case_number), str(row.get("client_name") or "")
    except Exception:
        logging.getLogger(__name__).debug("gcal case lookup by number failed", exc_info=True)
    return case_number, ""


def _resolve_gcal_event_case_identity(conn: Any, summary: str, description: str = "", event_date: str = "") -> tuple[str, str]:
    """Resolve a manually-entered Google Calendar event to a unique active case.

    This is deliberately conservative: if a name appears in multiple active
    cases and the event text does not contain a case/law-aid number or reason
    hint, MAGI leaves the calendar row unassigned instead of mixing procedures.
    """
    text = f"{summary or ''}\n{description or ''}"
    norm_text = _gcal_norm_text(text)
    if not norm_text:
        return "", ""
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT case_number, client_name, case_reason, case_type, case_category,
                       court_case_no, court_case_number, laf_case_no, application_no,
                       status, start_date, approval_date
                FROM cases
                WHERE COALESCE(case_number, '') != ''
                  AND COALESCE(client_name, '') != ''
                  AND COALESCE(status, '') NOT IN ('已結案', '結案', 'closed', 'Closed', 'CLOSED')
                ORDER BY CHAR_LENGTH(client_name) DESC, case_number DESC
                LIMIT 2000
                """
            )
            rows = cur.fetchall() or []
        finally:
            cur.close()
    except Exception:
        logging.getLogger(__name__).debug("gcal active case identity cache failed", exc_info=True)
        return "", ""

    scored: list[tuple[int, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _gcal_date_before_case_start(event_date, row):
            continue
        case_no = str(row.get("case_number") or "").strip()
        client = str(row.get("client_name") or "").strip()
        if not case_no or not client:
            continue
        best_token_len = 0
        for token in _gcal_client_tokens(client):
            if token and token in norm_text:
                best_token_len = max(best_token_len, len(token))
        if best_token_len <= 0:
            continue
        score = 100 + best_token_len
        for field in ("court_case_no", "court_case_number", "laf_case_no", "application_no", "case_number"):
            raw = str(row.get(field) or "").strip()
            if raw and _gcal_norm_text(raw) in norm_text:
                score += 80
        reason = str(row.get("case_reason") or row.get("case_type") or "").strip()
        for hint in {reason, reason[:2], str(row.get("case_type") or "").strip()}:
            hint_norm = _gcal_norm_text(hint)
            if len(hint_norm) >= 2 and hint_norm in norm_text:
                score += 10
        category_norm = _gcal_norm_text(row.get("case_category") or "")
        if category_norm and category_norm in norm_text:
            score += 5
        scored.append((score, case_no, client))

    if not scored:
        return "", ""
    scored.sort(reverse=True)
    top_score = scored[0][0]
    top = [(case_no, client) for score, case_no, client in scored if score == top_score]
    top_case_numbers = {case_no for case_no, _client in top}
    if len(top_case_numbers) == 1:
        case_no, client = top[0]
        return case_no, client
    return "", ""


def _backfill_gcal_import_case_identity(
    conn: Any,
    *,
    lookback_days: int = 30,
    lookahead_days: int = 180,
    limit: int = 300,
) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=max(0, lookback_days))).isoformat()
    end = (today + timedelta(days=max(1, lookahead_days))).isoformat()
    out: dict[str, Any] = {"updated": 0, "skipped": 0, "items": []}
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT id, todo_type, todo_date, todo_time, description, source_file
                FROM case_todos
                WHERE source_file LIKE 'gcal_import%%'
                  AND COALESCE(case_number, '') = ''
                  AND (
                    status IS NULL OR status=''
                    OR status NOT IN ('deleted', 'calendar_deduped', 'completed', 'done', '已完成', '完成', 'cancelled', 'canceled', '取消')
                  )
                  AND todo_date BETWEEN %s AND %s
                ORDER BY todo_date ASC, id ASC
                LIMIT %s
                """,
                (start, end, max(1, int(limit or 300))),
            )
            rows = cur.fetchall() or []
            for row in rows:
                summary = str(row.get("description") or "").strip()
                todo_type = str(row.get("todo_type") or "").strip()
                event_date = str(row.get("todo_date") or "").strip()[:10]
                case_no, client = _resolve_gcal_event_case_identity(
                    conn,
                    f"{summary} {todo_type}".strip(),
                    "",
                    event_date,
                )
                if not case_no:
                    out["skipped"] += 1
                    continue
                cur.execute(
                    """
                    UPDATE case_todos
                    SET case_number=%s, client_name=%s
                    WHERE id=%s
                    """,
                    (case_no, client, row.get("id")),
                )
                out["updated"] += int(getattr(cur, "rowcount", 0) or 0)
                if len(out["items"]) < 20:
                    out["items"].append({
                        "id": row.get("id"),
                        "case_number": case_no,
                        "client_name": client,
                        "description": summary[:120],
                    })
        finally:
            cur.close()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return out

def _is_dir_fast(path: str, timeout_sec: float = 1.0) -> bool:
    """
    Fast directory check that avoids Finder/CloudStorage stalls.
    """
    p = (path or "").strip()
    if not p:
        return False
    try:
        r = subprocess.run([_TEST_BIN, "-d", p], timeout=timeout_sec)
        return r.returncode == 0
    except Exception:
        return False


def _stat_mtime(path: str, timeout_sec: float = 1.5) -> float:
    """
    macOS stat mtime via subprocess (Synology-safe).
    """
    p = (path or "").strip()
    if not p:
        return 0.0
    try:
        r = subprocess.run(["/usr/bin/stat", "-f", "%m", p], capture_output=True, text=True, timeout=timeout_sec)
        if r.returncode != 0:
            return 0.0
        return float((r.stdout or "").strip() or "0")
    except Exception:
        return 0.0


def _pick_synology_case_root() -> str:
    candidates = preferred_case_roots(include_closed=False)
    env_override = (os.environ.get("MAGI_SYNOLOGY_CASE_ROOT") or "").strip()
    if env_override:
        candidates.insert(0, os.path.expanduser(env_override))
    for p in candidates:
        if _is_dir_fast(p):
            return p
    return ""


def _to_db_canonical_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    canonical = translate_local_path_to_canonical(p)
    return canonical or p


def _parse_case_folder_name(name: str) -> Dict[str, str]:
    """
    Parse folder name like:
      2025-0088-余秋菊-二審-毒品危害防制條例
    Returns: {case_number, client_name, case_reason}
    """
    n = (name or "").strip()
    if not n:
        return {"case_number": "", "client_name": "", "case_reason": ""}
    case_number = _extract_case_number_from_text(n)
    if not case_number:
        return {"case_number": "", "client_name": "", "case_reason": ""}
    rest = n
    if rest.startswith(case_number + "-"):
        rest = rest[len(case_number) + 1 :]
    parts = [p.strip() for p in rest.split("-") if p.strip()]
    client_name = ""
    case_reason = ""
    if parts:
        client_name = parts[0]
        case_reason = parts[-1]
    return {"case_number": case_number, "client_name": client_name, "case_reason": case_reason}


_COURT_HINT_MAP = {
    "花蓮地院": "臺灣花蓮地方法院",
    "花蓮地方法院": "臺灣花蓮地方法院",
    "臺灣花蓮地方法院": "臺灣花蓮地方法院",
    "臺東地院": "臺灣臺東地方法院",
    "臺東地方法院": "臺灣臺東地方法院",
    "臺灣臺東地方法院": "臺灣臺東地方法院",
    "臺北地院": "臺灣臺北地方法院",
    "臺北地方法院": "臺灣臺北地方法院",
    "臺中地院": "臺灣臺中地方法院",
    "臺中地方法院": "臺灣臺中地方法院",
    "臺南地院": "臺灣臺南地方法院",
    "臺南地方法院": "臺灣臺南地方法院",
    "高雄地院": "臺灣高雄地方法院",
    "高雄地方法院": "臺灣高雄地方法院",
    "高等法院": "臺灣高等法院",
    "臺灣高等法院": "臺灣高等法院",
    "最高法院": "最高法院",
    "高等行政法院": "臺北高等行政法院",
    "最高行政法院": "最高行政法院",
}


def _extract_doc_date_from_filename(fn: str) -> str:
    s = (fn or "").strip()
    # Western date at filename head: 20260414
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # ROC date at filename head: 1150414
    m = re.search(r"(?<!\d)(1\d{2})(\d{2})(\d{2})(?!\d)", s)
    if m:
        try:
            y = int(m.group(1)) + 1911
            return f"{y:04d}-{m.group(2)}-{m.group(3)}"
        except Exception:
            return ""
    return ""


def _extract_court_division_from_filename(fn: str) -> str:
    s = (fn or "").strip()
    if not s:
        return ""
    m = re.search(r"(?:股別|承辦股|分案股)\s*[:：]?\s*([A-Za-z0-9一-龥]{1,8}股)", s)
    if not m:
        m = re.search(r"([A-Za-z0-9一-龥]{1,6}股)(?!份)", s)
    if not m:
        return ""
    div = (m.group(1) or "").strip()
    return "" if "股份" in div else div


def _is_detention_ruling_case_number_source(fn: str) -> bool:
    """
    Criminal detention rulings often carry procedural numbers and should not
    replace the main court case number/division.
    """
    s = (fn or "").strip()
    if not s:
        return False
    detention_terms = ("羈押", "延長羈押", "停止羈押", "禁止接見", "禁止通信", "限制住居", "具保", "交保")
    if "裁定" in s and any(k in s for k in detention_terms):
        return True
    return False


def _extract_court_hint_and_case_no_from_filename(fn: str) -> Dict[str, str]:
    """
    Best-effort parse court + court case number from a PDF filename.
    """
    s = (fn or "").strip()
    if not s:
        return {"court_name": "", "court_case_number": "", "court_division": "", "doc_date": "", "excluded": False}

    court_name = ""
    for k, v in _COURT_HINT_MAP.items():
        if k and (k in s):
            court_name = v
            break

    # Generic fallbacks when no explicit hint matched.
    if not court_name:
        m = re.search(r"([一-龥]{2,4})地方法院", s)
        if m and ("臺灣" not in s):
            court_name = "臺灣" + m.group(1) + "地方法院"
        m2 = re.search(r"([一-龥]{2,4})高等行政法院", s)
        if not court_name and m2:
            court_name = m2.group(0)
        m3 = re.search(r"高等法院([一-龥]{1,4})分院", s)
        if not court_name and m3:
            court_name = "臺灣高等法院" + m3.group(1) + "分院"

    # e.g. 113年度原易字第179 ... (may miss '號')
    # Match patterns like: 113年度原易字第179  / 114年度重上更二字第95
    # Keep the "word" group generous (some case words are longer than 6 chars).
    m = re.search(r"(\d{2,3})年度([^\s]{1,16}?)(?:字)?第(\d{1,6})", s)
    if not m:
        if court_name:
            court_name = court_name.replace("台", "臺")
        return {
            "court_name": court_name,
            "court_case_number": "",
            "court_division": _extract_court_division_from_filename(s),
            "doc_date": _extract_doc_date_from_filename(s),
            "excluded": _is_detention_ruling_case_number_source(s),
        }
    year, word, num = m.group(1), m.group(2), m.group(3)
    # Pad to 6 digits for downstream systems (commonly used).
    try:
        num_padded = str(int(num)).zfill(6)
    except Exception:
        num_padded = num
    court_case_number = f"{year}年度{word}字第{num_padded}號"
    if court_name:
        court_name = court_name.replace("台", "臺")
    return {
        "court_name": court_name,
        "court_case_number": court_case_number,
        "court_division": _extract_court_division_from_filename(s),
        "doc_date": _extract_doc_date_from_filename(s),
        "excluded": _is_detention_ruling_case_number_source(s),
    }


def _court_case_word_from_number(court_case_number: str) -> str:
    m = re.search(r"\d{2,3}年度(.+?)字第\d{1,6}號", court_case_number or "")
    return (m.group(1) if m else "").strip()


def _court_case_number_quality(court_case_number: str) -> int:
    """Prefer substantive case numbers over procedural/interim numbers."""

    word = _court_case_word_from_number(court_case_number)
    if not word:
        return 0
    # Procedural/interim numbers must not keep a case stale after the main
    # case number appears in a judgment, indictment, or substantive notice.
    if any(k in word for k in ("強處", "聲羈", "偵聲", "國審聲", "科偵控", "限出")):
        return 10
    if "聲" in word and "訴" not in word:
        return 20
    if word.startswith("國蒞") or word.startswith("偵"):
        return 30
    if "上訴" in word:
        return 100
    if "訴" in word:
        return 96
    if any(k in word for k in ("易", "簡", "小", "消債", "司執", "司促", "家", "訴更")):
        return 90
    return 60


def _court_info_source_priority(source_path: str, filename: str) -> int:
    text = f"{source_path or ''}/{filename or ''}"
    if path_has_judgment_folder(text) or "判決" in filename:
        return 100
    if "起訴書" in filename:
        return 92
    if "法院通知" in text and "程序裁定" not in text:
        return 70
    if "程序裁定" in text or "裁定" in filename:
        return 55
    if "對方歷次書狀" in text:
        return 45
    if "我方歷次書狀" in text:
        return 40
    if "電子筆錄" in text or "筆錄" in text:
        return 35
    if "回執" in text:
        return 20
    return 30


def _court_info_rank(source_path: str, filename: str, info: Dict[str, str]) -> Tuple[Any, ...]:
    has_case_no = 1 if (info.get("court_case_number") or "").strip() else 0
    has_court_name = 1 if (info.get("court_name") or "").strip() else 0
    has_division = 1 if (info.get("court_division") or "").strip() else 0
    return (
        has_case_no,
        _court_info_source_priority(source_path, filename),
        _court_case_number_quality(info.get("court_case_number") or ""),
        info.get("doc_date") or "",
        int(_stat_mtime(os.path.join(source_path, filename)) or 0),
        has_court_name,
        has_division,
        filename or "",
    )


def _candidate_court_info_dirs(case_path: str, *, max_dirs: int = 48) -> List[str]:
    p = (case_path or "").strip()
    if not p:
        return []
    preferred_keywords = (
        JUDGMENT_FOLDER_LABEL, "判決書", "法院通知", "程序裁定", "對方歷次書狀", "我方歷次書狀",
        "回執", "筆錄",
    )

    def _dedupe(items: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for item in items:
            key = item.rstrip("/")
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    bases = _dedupe([p] + [c for c in local_case_path_candidates(p) if c])
    bases = [b for b in bases if _is_dir_fast(b)]
    if not bases:
        return []
    out: List[str] = []
    top_dirs: List[Tuple[int, str]] = []

    def _rank(name: str) -> int:
        for idx, kw in enumerate(preferred_keywords):
            if kw in name:
                return idx
        return 999

    for base in bases:
        if len(out) < max_dirs:
            out.append(base)
        for name in _listdir_timeout(base, timeout_sec=6):
            child = os.path.join(base, name)
            if not _is_dir_fast(child):
                continue
            rank = _rank(name)
            if rank < 999:
                top_dirs.append((rank, child))

    for _, child in sorted(top_dirs, key=lambda item: (item[0], item[1])):
        if child not in out:
            out.append(child)
            if len(out) >= max_dirs:
                return out

    for _, child in sorted(top_dirs, key=lambda item: (item[0], item[1])):
        # One extra level catches folders like "20251103 民事起訴狀繕本".
        for sub in _listdir_timeout(child, timeout_sec=4)[: max(1, max_dirs)]:
            grand = os.path.join(child, sub)
            if _is_dir_fast(grand) and grand not in out:
                out.append(grand)
                if len(out) >= max_dirs:
                    return out
    return out


def _discover_case_court_info(case_path: str, *, max_files: int = 120) -> Dict[str, str]:
    """
    Look into common "court notice" folders and infer court_name + court_case_number from filenames.
    No PDF parsing; filename-only.
    """
    p = (case_path or "").strip()
    if not p:
        return {"court_name": "", "court_case_number": "", "court_division": ""}

    picked = {"court_name": "", "court_case_number": "", "court_division": ""}
    ranked: List[Tuple[Tuple[Any, ...], Dict[str, str]]] = []
    dirs = _candidate_court_info_dirs(p)
    if not dirs:
        return picked

    for sp in dirs:
        names = _listdir_timeout(sp, timeout_sec=6)[: max(1, int(max_files))]
        for fn in names:
            if not fn.lower().endswith((".pdf", ".doc", ".docx", ".odt", ".txt")):
                continue
            info = _extract_court_hint_and_case_no_from_filename(fn)
            if info.get("excluded"):
                continue
            if not any(info.get(k) for k in ("court_name", "court_case_number", "court_division")):
                continue
            # Pick the most authoritative document first.  A later procedural
            # ruling (e.g. 國審強處/聲字) should not override a substantive
            # judgment or main case number.
            ranked.append((_court_info_rank(sp, fn, info), info))
    if ranked:
        ranked.sort(key=lambda item: item[0], reverse=True)
        picked = ranked[0][1]
    picked.pop("excluded", None)
    picked.pop("doc_date", None)
    return picked


def _norm_case_category(v: str) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    alias = {
        "法扶案件": "法律扶助案件",
        "法扶": "法律扶助案件",
        "法律扶助": "法律扶助案件",
    }
    return alias.get(s, s)


def task_index_cases(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Index Synology Drive case folders into `cases` table (local DB by default).
    This enables downstream skills (transcript download, queries) even when Keeper is offline.
    """
    from osc_headless.db import db_config_from_env, connect_mysql, ensure_cases_schema, upsert_case

    dry_run = bool(payload.get("dry_run", False))
    max_cases = int(payload.get("max_cases") or 200)
    max_files_per_case = int(payload.get("max_files_per_case") or 120)

    root = _pick_synology_case_root()
    if not root:
        out = {"ok": True, "skipped": True, "message": "找不到 Synology Drive 的 01_案件，略過案件索引"}
        _eventlog("osc:index_cases", ok=True, payload={"skipped": True, "reason": "synology_root_missing"})
        return out

    # Traverse depth 3: root/<category>/<type>/<case_folder>
    found = []
    for cat in _listdir_timeout(root, timeout_sec=6):
        p1 = os.path.join(root, cat)
        if not _is_dir_fast(p1):
            continue
        for typ in _listdir_timeout(p1, timeout_sec=6):
            p2 = os.path.join(p1, typ)
            if not _is_dir_fast(p2):
                continue
            for name in _listdir_timeout(p2, timeout_sec=6):
                p3 = os.path.join(p2, name)
                if not _is_dir_fast(p3):
                    continue
                if "/10_結案" in p3.replace("\\", "/") or ("結案" in name) or ("歸檔" in name) or ("封存" in name):
                    continue
                cn = _extract_case_number_from_text(name)
                if not cn:
                    continue
                mt = _stat_mtime(p3)
                found.append({"case_number": cn, "name": name, "path": p3, "category": cat, "type": typ, "mtime": mt})

    found.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
    found = found[: max(1, int(max_cases))]

    cfg = db_config_from_env()
    conn = connect_mysql(cfg)
    try:
        ensure_cases_schema(conn)
        inserted = 0
        updated = 0
        skipped_invalid = 0
        invalid_examples: List[str] = []
        for c in found:
            parsed = _parse_case_folder_name(c.get("name") or "")
            court_info = _discover_case_court_info(c.get("path") or "", max_files=max_files_per_case)
            if dry_run:
                continue
            if not (parsed.get("client_name") or "").strip():
                skipped_invalid += 1
                if len(invalid_examples) < 10:
                    invalid_examples.append(c.get("name") or "")
                continue
            res = upsert_case(
                conn,
                case_number=parsed.get("case_number") or c.get("case_number") or "",
                client_name=parsed.get("client_name") or "",
                case_reason=parsed.get("case_reason") or "",
                case_category=_norm_case_category(c.get("category") or ""),
                case_type=c.get("type") or "",
                folder_path=_to_db_canonical_path(c.get("path") or ""),
                court_name=court_info.get("court_name") or "",
                court_case_number=court_info.get("court_case_number") or "",
                court_division=court_info.get("court_division") or "",
                status="進行中",
            )
            inserted += int(res.get("inserted") or 0)
            updated += int(res.get("updated") or 0)

        out = {
            "ok": True,
            "dry_run": dry_run,
            "root": root,
            "scanned": len(found),
            "inserted": inserted,
            "updated": updated,
            "skipped_invalid": skipped_invalid,
            "invalid_examples": invalid_examples,
            "message": f"案件索引完成：掃描 {len(found)}，新增 {inserted}，更新 {updated}，略過異常命名 {skipped_invalid}",
        }
        _eventlog(
            "osc:index_cases",
            ok=True,
            payload={
                "dry_run": dry_run,
                "root": root,
                "scanned": len(found),
                "inserted": inserted,
                "updated": updated,
                "skipped_invalid": skipped_invalid,
            },
        )
        return out
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 409, exc_info=True)


def _enqueue_pending(payload: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """
    Append a record to the pending todo queue (best-effort).

    Safety/UX:
    - Do NOT enqueue for dry-run calls.
    - Do NOT enqueue for obvious temp paths (/tmp, /private/tmp).
      Otherwise a self-test can poison the queue and make OpenClaw cron think
      "needs human help" forever.
    - For skipped cases, we deadletter the record (so we can debug later) but
      do not block daily automation.
    """
    try:
        if bool(payload.get("dry_run")):
            raise RuntimeError("skip_enqueue:dishonors_dry_run")
        path = (payload.get("path") or payload.get("file_path") or "").strip()
        if path.startswith("/tmp/") or path.startswith("/private/tmp/") or path == "/tmp" or path == "/private/tmp":
            raise RuntimeError("skip_enqueue:tmp_path")
        # Optional: treat known temp roots as non-blocking
        try:
            ensure_orch_on_sys_path()
            import safe_fs  # type: ignore

            if path and getattr(safe_fs, "is_temp_path", None) and safe_fs.is_temp_path(path):
                raise RuntimeError("skip_enqueue:temp_root")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 438, exc_info=True)
    except Exception as e:
        rec = {
            "ts": datetime.now().isoformat(),
            "reason": reason,
            "skipped_enqueue": True,
            "skip_reason": str(e)[:120],
            "payload": payload,
        }
        try:
            with open(DEADLETTER_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 451, exc_info=True)
        return {"queued": False, "deadlettered": True, "deadletter_path": DEADLETTER_PATH}

    rec = {
        "ts": datetime.now().isoformat(),
        "reason": reason,
        "payload": payload,
    }
    with open(PENDING_QUEUE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"queued": True, "queue_path": PENDING_QUEUE_PATH}


def _load_patterns_from_db(conn) -> Dict[str, List[Dict]]:
    from osc_headless.db import fetch_active_todo_patterns

    rows = fetch_active_todo_patterns(conn)
    patterns: Dict[str, List[Dict]] = {}
    for todo_type, pattern, pattern_type, days in rows:
        patterns.setdefault(todo_type, []).append(
            {"pattern": pattern, "pattern_type": pattern_type, "days": days}
        )
    return patterns

def _merge_patterns(defaults: Dict[str, List[Dict]], extra: Optional[Dict[str, List[Dict]]]) -> Dict[str, List[Dict]]:
    """
    Merge DB patterns into defaults so baseline rules always exist.
    DB patterns are appended (higher priority can be handled by ordering later if needed).
    """
    out: Dict[str, List[Dict]] = {}
    for k, v in (defaults or {}).items():
        out[k] = list(v or [])
    for k, v in (extra or {}).items():
        out.setdefault(k, [])
        out[k].extend(list(v or []))
    return out


def task_db_smoke(_payload: Dict[str, Any]) -> Dict[str, Any]:
    from osc_headless.db import db_config_from_env, connect_mysql, ensure_osc_min_schema, seed_default_todo_keywords, fetch_active_todo_patterns

    cfg = db_config_from_env()
    conn = connect_mysql(cfg)
    try:
        schema = ensure_osc_min_schema(conn) or {}
        rows = fetch_active_todo_patterns(conn)
        seeded = 0
        if not rows:
            seeded = seed_default_todo_keywords(conn)
            rows = fetch_active_todo_patterns(conn)
        selected_host = str(getattr(conn, "magi_selected_host", "") or cfg.host)
        selected_port = int(getattr(conn, "magi_selected_port", cfg.port) or cfg.port)
        selected_db = str(getattr(conn, "magi_selected_db", "") or cfg.database)
        fallback_used = bool(selected_host != cfg.host or selected_port != int(cfg.port) or selected_db != cfg.database)
        return {
            "ok": True,
            "db": {
                "target_host": cfg.host,
                "target_port": cfg.port,
                "target_database": cfg.database,
                "connected_host": selected_host,
                "connected_port": selected_port,
                "connected_database": selected_db,
                "user": cfg.user,
                "fallback_used": fallback_used,
            },
            "schema": schema,
            "todo_patterns": len(rows),
            "seeded": seeded,
        }
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 525, exc_info=True)


def task_todo_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    from osc_headless.db import db_config_from_env, connect_mysql, ensure_osc_min_schema, seed_default_todo_keywords
    from osc_headless.todos import extract_document_date_from_filename, extract_todos_from_filename, get_default_patterns

    path = (payload.get("path") or payload.get("file_path") or "").strip()
    filename = (payload.get("filename") or (os.path.basename(path) if path else "")).strip()
    if not filename:
        raise ValueError("需要 filename 或 path")

    patterns = None
    db_err = ""
    try:
        cfg = db_config_from_env()
        conn = connect_mysql(cfg)
        try:
            ensure_osc_min_schema(conn)
            pats = _load_patterns_from_db(conn)
            if not pats:
                seed_default_todo_keywords(conn)
                pats = _load_patterns_from_db(conn)
            patterns = pats or None
        finally:
            conn.close()
    except Exception as e:
        db_err = f"{type(e).__name__}: {str(e)[:200]}"
        patterns = None

    merged = _merge_patterns(get_default_patterns(), patterns)
    todos = extract_todos_from_filename(filename, path, patterns=merged)
    doc_dt = extract_document_date_from_filename(filename, path)
    return {
        "ok": True,
        "filename": filename,
        "path": path,
        "document_date": (doc_dt.strftime("%Y-%m-%d") if doc_dt else None),
        "todos": todos,
        "db_patterns_used": bool(patterns),
        "db_error": (db_err or None),
    }


def task_todo_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    from osc_headless.db import (
        db_config_from_env,
        connect_mysql,
        ensure_osc_min_schema,
        seed_default_todo_keywords,
        insert_case_todos,
    )
    from osc_headless.todos import extract_todos_from_filename, get_default_patterns

    path = (payload.get("path") or payload.get("file_path") or "").strip()
    filename = (payload.get("filename") or (os.path.basename(path) if path else "")).strip()
    if not filename:
        raise ValueError("需要 filename 或 path")

    # Dry-run: never write DB, never enqueue pending. Just parse and return.
    if bool(payload.get("dry_run")):
        from osc_headless.todos import extract_todos_from_filename, get_default_patterns
        case_number_from_folder = _extract_case_number_from_text(payload.get("case_folder_name") or "")
        case_number_from_path = _extract_case_number_from_path(path) if path else ""
        case_number_explicit = (payload.get("case_number") or "").strip()
        case_number = case_number_explicit or case_number_from_folder or case_number_from_path
        todos = extract_todos_from_filename(filename, path, patterns=get_default_patterns())
        return {
            "ok": True,
            "dry_run": True,
            "filename": filename,
            "path": path,
            "case_number": case_number,
            "case_number_source": (
                "explicit" if case_number and case_number == case_number_explicit else
                "case_folder_name" if case_number and case_number == case_number_from_folder else
                "path" if case_number and case_number == case_number_from_path else
                "unknown"
            ),
            "todos": todos,
            "note": "dry_run=1：未入庫、未寫入佇列",
        }

    case_number_explicit = (payload.get("case_number") or "").strip()
    case_number_from_folder = _extract_case_number_from_text(payload.get("case_folder_name") or "")
    case_number_from_path = _extract_case_number_from_path(path) if path else ""

    # Safety: if user supplies explicit case_number but it conflicts with the filed folder, do not write.
    if case_number_explicit and case_number_from_folder and (case_number_explicit != case_number_from_folder):
        return {
            "ok": False,
            "error": "case_number 與 case_folder_name 不一致（為避免寫錯案件已改走佇列）",
            "case_number_explicit": case_number_explicit,
            "case_number_from_folder": case_number_from_folder,
            **_enqueue_pending(payload, "case_number_mismatch"),
        }

    case_number = case_number_explicit or case_number_from_folder or case_number_from_path
    case_number_source = (
        "explicit" if case_number == case_number_explicit and case_number_explicit else
        "case_folder_name" if case_number == case_number_from_folder and case_number_from_folder else
        "path" if case_number == case_number_from_path and case_number_from_path else
        "unknown"
    )
    if not case_number:
        # 不強行猜，避免寫錯案件；改走 pending 佇列
        return {
            "ok": False,
            "error": "缺 case_number（無法安全入庫）",
            **_enqueue_pending(payload, "missing_case_number"),
        }

    client_name = (payload.get("client_name") or "").strip()
    source_file = filename

    # Load patterns (best-effort)
    patterns = None
    conn = None
    try:
        cfg = db_config_from_env()
        conn = connect_mysql(cfg)
        ensure_osc_min_schema(conn)
        pats = _load_patterns_from_db(conn)
        if not pats:
            seed_default_todo_keywords(conn)
            pats = _load_patterns_from_db(conn)
        patterns = pats or None

        merged = _merge_patterns(get_default_patterns(), patterns)
        todos = extract_todos_from_filename(filename, path, patterns=merged)
        
        # --- Vision API Event Parsing ---
        doc_type = (payload.get("doc_type") or "").strip()
        if doc_type in ["法院通知", "裁定", "辦案通知", "法扶通知", "函文", "開庭通知", "開庭"]:
            try:
                from vision_event_parser import extract_events_from_pdf
                vision_events = extract_events_from_pdf(path)
                for ve in vision_events:
                    if isinstance(ve, dict) and ve.get("type") and ve.get("date"):
                        todos.append({
                            "type": ve.get("type") or "待辦",
                            "date": ve.get("date"),
                            "time": ve.get("time"),
                            "description": "(視覺辨識) " + ve.get("description", ""),
                        })
            except Exception as e:
                import logging
                logging.getLogger("osc-orchestrator").warning(f"Vision event parsing failed: {e}")
        res = insert_case_todos(
            conn,
            case_number=case_number,
            client_name=client_name,
            todos=todos,
            source_file=source_file,
            allow_duplicates=bool(payload.get("allow_duplicates")),
        )
        return {
            "ok": True,
            "case_number": case_number,
            "case_number_source": case_number_source,
            "insert": res,
            "todos": todos,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            **_enqueue_pending(payload, "db_error"),
        }
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 699, exc_info=True)


def task_queue_flush(payload: Dict[str, Any]) -> Dict[str, Any]:
    from osc_headless.db import db_config_from_env, connect_mysql, ensure_osc_min_schema

    limit = int(payload.get("limit") or 50)
    if not os.path.exists(PENDING_QUEUE_PATH):
        out = {"ok": True, "flushed": 0, "remaining": 0}
        _eventlog("osc:queue_flush", ok=True, payload={"limit": limit, "flushed": 0, "remaining": 0})
        return out

    # Read all then rewrite remaining (no deletes of business data, only queue maintenance)
    with open(PENDING_QUEUE_PATH, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]

    to_try = lines[:limit]
    rest = lines[limit:]

    cfg = db_config_from_env()
    conn = connect_mysql(cfg)
    try:
        ensure_osc_min_schema(conn)
        ok_count = 0
        fail_records = []
        for ln in to_try:
            try:
                rec = json.loads(ln)
                reason = (rec.get("reason") or "").strip()
                pl = rec.get("payload") or {}

                # Drop non-business / temp / dry-run records to deadletter so cron won't get stuck.
                try:
                    pth = str((pl.get("path") or pl.get("file_path") or "")).strip()
                    if bool(pl.get("dry_run")) or pth.startswith("/tmp/") or pth.startswith("/private/tmp/"):
                        with open(DEADLETTER_PATH, "a", encoding="utf-8") as f:
                            f.write(ln + "\n")
                        continue
                    # Scanner scratch exports (e.g. 頁面擷取自-Scan*.pdf) often have no case number by design.
                    # Keep them out of blocking queue to avoid false "needs human" alarms every tick.
                    bn = os.path.basename(pth).strip().lower()
                    looks_like_scan_scratch = (
                        reason == "missing_case_number"
                        and bn.startswith("頁面擷取自-scan")
                        and bn.endswith(".pdf")
                    )
                    if looks_like_scan_scratch or (reason == "missing_case_number" and pth and (not os.path.exists(pth))):
                        with open(DEADLETTER_PATH, "a", encoding="utf-8") as f:
                            f.write(ln + "\n")
                        continue
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 750, exc_info=True)

                # Reuse todo_sync logic, but keep it simple here to avoid recursion
                out = task_todo_sync(pl)
                if out.get("ok"):
                    ok_count += 1
                else:
                    # 若為測試資料導致的 mismatch，轉入 deadletter，避免排程永遠卡住
                    case_folder_name = (pl.get("case_folder_name") or "")
                    filename = (pl.get("filename") or "")
                    if reason == "case_number_mismatch" and ("測試" in case_folder_name or "測試" in filename):
                        try:
                            with open(DEADLETTER_PATH, "a", encoding="utf-8") as f:
                                f.write(ln + "\n")
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 765, exc_info=True)
                    else:
                        fail_records.append(ln)
            except Exception:
                fail_records.append(ln)

        # Rewrite queue (atomic-ish): failed + rest
        with open(PENDING_QUEUE_TMP_PATH, "w", encoding="utf-8") as f:
            for ln in fail_records + rest:
                f.write(ln + "\n")
        os.replace(PENDING_QUEUE_TMP_PATH, PENDING_QUEUE_PATH)

        remaining_lines = fail_records + rest
        # Classify remaining items:
        # - db_error: usually transient (DB down / network hiccup) => non-blocking, retry next run.
        # - missing_case_number / case_number_mismatch: needs human disambiguation => blocking.
        # - unknown/parse_error: treat as blocking to be safe.
        reason_counts: Dict[str, int] = {}
        remaining_blocking = 0
        remaining_nonblocking = 0
        for ln in remaining_lines:
            r = "unknown"
            try:
                obj = json.loads(ln)
                r = (obj.get("reason") or "").strip() or "unknown"
            except Exception:
                r = "parse_error"
            reason_counts[r] = int(reason_counts.get(r, 0) or 0) + 1
            if r in {"db_error"}:
                remaining_nonblocking += 1
            else:
                # Anything ambiguous or malformed blocks so the operator can review.
                remaining_blocking += 1

        out = {
            "ok": True,
            "flushed": ok_count,
            "remaining": len(remaining_lines),
            "remaining_blocking": remaining_blocking,
            "remaining_nonblocking": remaining_nonblocking,
            "reasons": reason_counts,
        }
        _eventlog("osc:queue_flush", ok=True, payload={"limit": limit, "flushed": ok_count, "remaining": out["remaining"]})
        return out
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 813, exc_info=True)


def task_queue_status(_payload: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(PENDING_QUEUE_PATH):
        return {"ok": True, "pending": 0, "queue_path": PENDING_QUEUE_PATH}
    try:
        with open(PENDING_QUEUE_PATH, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        tail = []
        for ln in lines[-5:]:
            try:
                tail.append(json.loads(ln))
            except Exception:
                tail.append({"raw": ln[:200]})
        return {"ok": True, "pending": len(lines), "queue_path": PENDING_QUEUE_PATH, "tail": tail}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "queue_path": PENDING_QUEUE_PATH}


def task_todo_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    from osc_headless.db import db_config_from_env, connect_mysql, ensure_osc_min_schema

    case_number = (payload.get("case_number") or "").strip()
    if not case_number:
        raise ValueError("需要 case_number")
    status = (payload.get("status") or "pending").strip()
    limit = int(payload.get("limit") or 50)

    cfg = db_config_from_env()
    conn = connect_mysql(cfg)
    try:
        ensure_osc_min_schema(conn)
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT `id`, `case_number`, `client_name`, `todo_type`, `todo_date`, `todo_time`,
                       `description`, `status`, `source_file`, `created_date`, `completed_date`
                FROM `case_todos`
                WHERE `case_number`=%s AND (`status`=%s OR %s='*')
                ORDER BY
                  CASE WHEN `todo_date` IS NULL THEN 1 ELSE 0 END,
                  `todo_date` ASC,
                  CASE WHEN `todo_time` IS NULL THEN 1 ELSE 0 END,
                  `todo_time` ASC,
                  `id` ASC
                LIMIT %s
                """,
                (case_number, status, status, limit),
            )
            rows = cur.fetchall()
            return {"ok": True, "case_number": case_number, "status": status, "limit": limit, "todos": rows}
        finally:
            cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 872, exc_info=True)


def task_keyword_sanity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inspect todo_keywords for common regex escaping mistakes (e.g. \\\\d instead of \\d).
    Report only; no destructive changes.
    """
    from osc_headless.db import db_config_from_env, connect_mysql, ensure_osc_min_schema

    limit = int(payload.get("limit") or 200)
    active_only = bool(payload.get("active_only", True))
    cfg = db_config_from_env()
    conn = connect_mysql(cfg)
    try:
        ensure_osc_min_schema(conn)
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT `id`, `todo_type`, `pattern`, `pattern_type`, `days`, `is_active`
                FROM `todo_keywords`
                WHERE (%s = 0) OR (`is_active` = 1)
                ORDER BY `id` DESC
                LIMIT %s
                """,
                (1 if active_only else 0, limit),
            )
            rows = cur.fetchall()
        finally:
            cur.close()

        suspicious = []
        for r in rows:
            if active_only and int(r.get("is_active") or 0) != 1:
                continue
            pat = r.get("pattern") or ""
            # Two backslashes in the stored regex is almost always a bug for digit classes.
            if "\\\\d" in pat or "\\\\s" in pat:
                suspicious.append(r)

        return {
            "ok": True,
            "checked": len(rows),
            "active_only": active_only,
            "suspicious": len(suspicious),
            "items": suspicious[:50],
        }
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 924, exc_info=True)


def task_keyword_fix_escapes(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fix common over-escaped patterns in todo_keywords:
    - \\\\d -> \\d
    - \\\\s -> \\s
    Strategy (safe with unique key):
    - INSERT IGNORE corrected pattern (same todo_type/pattern_type/days)
    - Mark old row inactive (is_active=0)
    """
    from osc_headless.db import db_config_from_env, connect_mysql, ensure_osc_min_schema

    dry_run = bool(payload.get("dry_run", True))
    limit = int(payload.get("limit") or 500)

    cfg = db_config_from_env()
    conn = connect_mysql(cfg)
    try:
        ensure_osc_min_schema(conn)
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT `id`, `todo_type`, `pattern`, `pattern_type`, `days`, `is_active`
                FROM `todo_keywords`
                WHERE `is_active` = 1
                ORDER BY `id` DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            cur.close()

        fixes = []
        for r in rows:
            pat = r.get("pattern") or ""
            if ("\\\\d" not in pat) and ("\\\\s" not in pat):
                continue
            fixed = pat.replace("\\\\d", "\\d").replace("\\\\s", "\\s")
            if fixed == pat:
                continue
            fixes.append(
                {
                    "id": r["id"],
                    "todo_type": r["todo_type"],
                    "pattern_type": r["pattern_type"],
                    "days": r.get("days"),
                    "from": pat,
                    "to": fixed,
                }
            )

        if dry_run:
            return {"ok": True, "dry_run": True, "candidates": len(fixes), "items": fixes[:50]}

        # Apply changes
        cur2 = conn.cursor()
        inserted = 0
        inactivated = 0
        try:
            for fx in fixes:
                cur2.execute(
                    """
                    INSERT IGNORE INTO `todo_keywords`
                      (`todo_type`, `pattern`, `pattern_type`, `days`, `is_active`)
                    VALUES (%s,%s,%s,%s,1)
                    """,
                    (fx["todo_type"], fx["to"], fx["pattern_type"], fx["days"]),
                )
                # Regardless of insert result, inactivate the old row to avoid double matching
                cur2.execute("UPDATE `todo_keywords` SET `is_active`=0 WHERE `id`=%s", (fx["id"],))
                inserted += int(getattr(cur2, "rowcount", 0) or 0)  # best-effort
                inactivated += 1
            conn.commit()
        finally:
            cur2.close()

        return {"ok": True, "dry_run": False, "candidates": len(fixes), "inactivated": inactivated, "insert_ops": inserted}
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1010, exc_info=True)


def task_scan_folder(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scan a folder recursively, parse todos from PDF filenames, and write into DB.
    Safe: read-only scan, insert-only DB writes; never deletes Synology files.
    """
    from osc_headless.db import (
        db_config_from_env,
        connect_mysql,
        ensure_osc_min_schema,
        seed_default_todo_keywords,
        insert_case_todos,
    )
    from osc_headless.todos import extract_todos_from_filename, get_default_patterns

    root = (payload.get("root") or payload.get("path") or "").strip()
    if not root:
        raise ValueError("需要 root/path")
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ValueError(f"資料夾不存在: {root}")

    max_files = int(payload.get("max_files") or 200)
    case_number_explicit = (payload.get("case_number") or "").strip()
    dry_run = bool(payload.get("dry_run"))

    cfg = db_config_from_env()
    conn = connect_mysql(cfg)
    try:
        ensure_osc_min_schema(conn)
        pats = _load_patterns_from_db(conn)
        if not pats:
            seed_default_todo_keywords(conn)
            pats = _load_patterns_from_db(conn)
        merged = _merge_patterns(get_default_patterns(), pats or None)

        scanned = 0
        inserted = 0
        skipped = 0
        queued = 0
        items = []

        for dirpath, dirnames, filenames in os.walk(root):
            # Skip hidden dirs and Synology metadata
            dirnames[:] = [d for d in dirnames if (not d.startswith(".")) and (d != "@eaDir")]

            for fn in filenames:
                if scanned >= max_files:
                    break
                if fn.startswith("."):
                    continue
                if not fn.lower().endswith(".pdf"):
                    continue

                full = os.path.join(dirpath, fn)
                scanned += 1

                case_number_from_path = _extract_case_number_from_path(full)
                case_number = case_number_explicit or case_number_from_path
                if not case_number:
                    queued += 1
                    _enqueue_pending({"path": full}, "missing_case_number")
                    continue

                todos = extract_todos_from_filename(fn, full, patterns=merged)
                if not todos:
                    continue

                if dry_run:
                    items.append({"path": full, "case_number": case_number, "todos": todos, "status": "preview"})
                    continue

                res = insert_case_todos(
                    conn,
                    case_number=case_number,
                    client_name=(payload.get("client_name") or "").strip(),
                    todos=todos,
                    source_file=fn,
                    allow_duplicates=bool(payload.get("allow_duplicates")),
                    commit=False,
                )
                inserted += int(res.get("inserted") or 0)
                skipped += int(res.get("skipped") or 0)
                status = (
                    "inserted" if int(res.get("inserted") or 0) > 0 else
                    "updated" if int(res.get("updated") or 0) > 0 else
                    "skipped"
                )
                items.append({"path": full, "case_number": case_number, "insert": res, "todos": todos, "status": status})

            if scanned >= max_files:
                break

        if not dry_run:
            conn.commit()

        return {
            "ok": True,
            "root": root,
            "dry_run": dry_run,
            "scanned": scanned,
            "inserted": inserted,
            "skipped": skipped,
            "queued": queued,
            "items": items[:50],
        }
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1117, exc_info=True)


def task_scan_cases(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scan all (or filtered) case folders under Synology 01_案件 and ingest todos.
    This uses pdf-namer's case index when available to avoid re-implementing parsing.
    """
    # Lazy import to keep skill light
    # Import by path (pdf-namer skill isn't a Python package)
    try:
        import importlib.util
        path = f"{_MAGI_ROOT}/skills/pdf-namer/smart_filer.py"
        spec = importlib.util.spec_from_file_location("_pdfn_smart_filer", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        case_index = mod.build_case_index(force_rebuild=bool(payload.get("force_rebuild")))
    except Exception:
        raise RuntimeError("無法載入 pdf-namer case index（請確認 pdf-namer skill 存在）")

    max_cases = int(payload.get("max_cases") or 50)
    max_files_per_case = int(payload.get("max_files_per_case") or 50)
    dry_run = bool(payload.get("dry_run"))
    case_number_filter = (payload.get("case_number") or "").strip()
    time_budget_sec = int(payload.get("time_budget_sec") or 0)  # 0=無限制
    t0 = time.monotonic()

    subfolder_keywords = payload.get("subfolder_keywords") or [
        "法院通知與程序裁定",
        "法院通知或程序裁定",
        "法院通知",
        "程序裁定",
        JUDGMENT_FOLDER_LABEL,
        "判決書",
        "法院_通知",
        "法院_傳票",
        "閱卷資料",
    ]
    if isinstance(subfolder_keywords, str):
        subfolder_keywords = [subfolder_keywords]

    processed_cases = 0
    total = {"scanned": 0, "inserted": 0, "skipped": 0, "queued": 0}
    results = []

    for c in case_index:
        if time_budget_sec > 0 and (time.monotonic() - t0) > float(time_budget_sec):
            break
        if processed_cases >= max_cases:
            break
        folder_name = c.get("folder_name") or ""
        case_path = c.get("path") or ""
        case_number = _extract_case_number_from_text(folder_name)
        if not case_number:
            continue
        if case_number_filter and case_number_filter != case_number:
            continue
        if not case_path or (not os.path.isdir(case_path)):
            continue

        # Find matching subfolders
        targets = []
        for sf in _listdir_timeout(case_path, timeout_sec=8):
            if sf.startswith("."):
                continue
            sf_path = os.path.join(case_path, sf)
            try:
                if not os.path.isdir(sf_path):
                    continue
            except Exception:
                continue
            clean = re.sub(r"^\d+_", "", sf)
            if any(k in clean for k in subfolder_keywords):
                targets.append(sf_path)

        case_res = {"case_number": case_number, "case_path": case_path, "targets": targets, "scans": []}
        for t in targets:
            if time_budget_sec > 0 and (time.monotonic() - t0) > float(time_budget_sec):
                break
            out = task_scan_folder(
                {
                    "root": t,
                    "case_number": case_number,
                    "max_files": max_files_per_case,
                    "dry_run": dry_run,
                }
            )
            case_res["scans"].append(out)
            for k in ("scanned", "inserted", "skipped", "queued"):
                total[k] += int(out.get(k, 0) or 0)

        results.append(case_res)
        processed_cases += 1

    out = {
        "ok": True,
        "dry_run": dry_run,
        "processed_cases": processed_cases,
        "total": total,
        "time_budget_sec": (time_budget_sec or None),
        "elapsed_sec": round(time.monotonic() - t0, 3),
        "results": results[:20],
    }
    _eventlog(
        "osc:scan_cases",
        ok=True,
        payload={
            "dry_run": dry_run,
            "processed_cases": processed_cases,
            "total": total,
            "time_budget_sec": (time_budget_sec or None),
            "elapsed_sec": out.get("elapsed_sec"),
        },
    )
    return out


def task_self_test(_payload: Dict[str, Any]) -> Dict[str, Any]:
    smoke = task_db_smoke({})
    preview = task_todo_preview({"filename": "20250101 臺灣臺北地方法院 113年度訴字第1號 10日內補正.pdf"})
    qstat = task_queue_status({})
    return {"ok": True, "db_smoke": smoke, "todo_preview": preview, "queue_status": qstat}


def _build_google_calendar_service(
    credentials_path: str,
    token_path: str,
    scopes: Optional[list[str]] = None,
    *,
    interactive: bool = False,
) -> Dict[str, Any]:
    """
    Build a Google Calendar API service (write-capable).
    Returns:
      {ok, service} or {ok:false, need_interactive_oauth:true, error:"need_interactive_oauth"}.
    """
    credentials_path = (credentials_path or "").strip()
    token_path = (token_path or "").strip()
    if not token_path:
        return {"ok": False, "error": "token_path_missing"}

    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except Exception as e:
        return {"ok": False, "error": f"missing_google_deps:{type(e).__name__}"}

    SCOPES = scopes or ["https://www.googleapis.com/auth/calendar"]
    def _load_pickle_cred(path: str):
        try:
            with open(path, "rb") as f:
                obj = pickle.load(f)
            if getattr(obj, "token", None):
                return obj
        except Exception:
            return None
        return None

    def _candidate_token_paths(primary: str) -> List[str]:
        out: List[str] = []
        p = (primary or "").strip()
        if p:
            out.append(p)
            base_dir = os.path.dirname(p)
            out.append(os.path.join(base_dir, "token.pickle"))
            out.append(os.path.join(base_dir, "calendar_token.pickle"))
        # de-duplicate while preserving order
        uniq: List[str] = []
        seen = set()
        for x in out:
            if not x or x in seen:
                continue
            seen.add(x)
            uniq.append(x)
        return uniq

    creds = None
    loaded_from = ""
    for cand in _candidate_token_paths(token_path):
        if not os.path.exists(cand):
            continue
        if cand.lower().endswith(".json"):
            try:
                creds = Credentials.from_authorized_user_file(cand, SCOPES)
                loaded_from = cand
                break
            except Exception:
                continue
        if cand.lower().endswith(".pickle"):
            c = _load_pickle_cred(cand)
            if c is not None:
                creds = c
                loaded_from = cand
                break

    if creds and creds.expired and creds.refresh_token:
        try:
            if token_path:
                with google_token_file_lock(Path(token_path)):
                    creds.refresh(Request())
                    _write_token_atomic(token_path, creds.to_json())
            else:
                creds.refresh(Request())
        except Exception:
            creds = None

    if (not creds or not creds.valid) and interactive:
        if not credentials_path or not os.path.exists(credentials_path):
            return {"ok": False, "error": f"credentials_not_found:{credentials_path}"}
        # Interactive OAuth (admin/daytime only): open local server flow, then persist token.
        try:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            with google_token_file_lock(Path(token_path)):
                _write_token_atomic(token_path, creds.to_json())
        except Exception as e:
            return {"ok": False, "error": f"interactive_oauth_failed:{type(e).__name__}"}

    if not creds or not creds.valid:
        # Headless runner must not trigger interactive flow. Return a signal.
        return {"ok": False, "need_interactive_oauth": True, "error": "need_interactive_oauth", "token_path": token_path}

    # If creds came from a legacy pickle path, persist canonical json token for nightly.
    try:
        if token_path and creds and creds.valid and loaded_from and os.path.abspath(loaded_from) != os.path.abspath(token_path):
            with google_token_file_lock(Path(token_path)):
                _write_token_atomic(token_path, creds.to_json())
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1333, exc_info=True)

    try:
        svc = build("calendar", "v3", credentials=creds)
        return {"ok": True, "service": svc}
    except Exception as e:
        return {"ok": False, "error": f"calendar_build_failed:{type(e).__name__}"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _default_gcal_token_path() -> str:
    paperclip_token = Path.home() / ".magi" / "google" / "token.json"
    if paperclip_token.exists():
        return str(paperclip_token)
    return str(get_config_path("google_calendar_token.json"))


def _default_gcal_credentials_path() -> str:
    env_path = os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH", "").strip()
    if env_path:
        return env_path
    repo_credentials = get_config_path("credentials.json")
    if repo_credentials.exists():
        return str(repo_credentials)
    # Paperclip OAuth stores client_id/client_secret inside token.json, so
    # headless refresh can still work without forcing a second credentials file.
    return ""


def _event_list_time_window_from_body(body: Dict[str, Any], tz: str) -> Tuple[str, str]:
    start = body.get("start") if isinstance(body, dict) else {}
    if not isinstance(start, dict):
        return "", ""
    date_time = str(start.get("dateTime") or "").strip()
    if date_time:
        try:
            dt = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_utc = dt.astimezone(timezone.utc)
            return (
                (dt_utc - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                (dt_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        except Exception:
            return "", ""
    date_only = str(start.get("date") or "").strip()
    if date_only:
        try:
            d0 = datetime.strptime(date_only, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return (
                d0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                (d0 + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        except Exception:
            return "", ""
    return "", ""


def _find_existing_gcal_event(
    service: Any,
    *,
    calendar_id: str,
    body: Dict[str, Any],
    dedup_key: str,
    tz: str,
) -> Optional[Dict[str, Any]]:
    try:
        from osc_headless.gcal_dedup import build_dedup_key_from_gcal_event, confidence_for_match
    except Exception:
        return None

    candidates: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _add_candidates(items: Any) -> None:
        if not isinstance(items, list):
            return
        for ev in items:
            if not isinstance(ev, dict):
                continue
            eid = str(ev.get("id") or "").strip()
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            candidates.append(ev)

    if dedup_key:
        try:
            res = service.events().list(
                calendarId=calendar_id,
                privateExtendedProperty=f"magi_dedup_key={dedup_key}",
                singleEvents=True,
                maxResults=25,
            ).execute()
            matched_items = (res or {}).get("items", [])
            _add_candidates(matched_items)
            # privateExtendedProperty is exact; trust it as first-class hit.
            if isinstance(matched_items, list) and matched_items:
                first_hit = matched_items[0]
                if isinstance(first_hit, dict) and first_hit.get("id"):
                    return first_hit
        except Exception:
            logging.getLogger(__name__).debug("gcal dedup lookup by private property failed", exc_info=True)

    time_min, time_max = _event_list_time_window_from_body(body, tz)
    if time_min and time_max:
        try:
            res = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=120,
            ).execute()
            _add_candidates((res or {}).get("items", []))
        except Exception:
            logging.getLogger(__name__).debug("gcal dedup time-window lookup failed", exc_info=True)

    probe = {
        "summary": body.get("summary") or "",
        "description": body.get("description") or "",
        "start": body.get("start") or {},
        "todo_type": ((body.get("extendedProperties") or {}).get("private") or {}).get("magi_todo_type") or "",
        "case_number": ((body.get("extendedProperties") or {}).get("private") or {}).get("magi_case_number") or "",
        "dedup_key": dedup_key,
    }
    for ev in candidates:
        try:
            ev_key = build_dedup_key_from_gcal_event(ev, tz=tz)
            if dedup_key and ev_key == dedup_key:
                return ev
            conf = confidence_for_match(
                probe,
                {
                    "summary": ev.get("summary") or "",
                    "description": ev.get("description") or "",
                    "start": ev.get("start") or {},
                    "todo_type": "",
                    "case_number": "",
                    "dedup_key": ev_key,
                },
            )
            if conf == "high":
                return ev
        except Exception:
            continue
    return None


def _todo_to_gcal_event(todo: Dict[str, Any], tz: str) -> Dict[str, Any]:
    """
    Convert a DB todo row into a Google Calendar event body.
    """
    client = (todo.get("client_name") or "").strip()
    case_number = (todo.get("case_number") or "").strip()
    court_case_no = (todo.get("court_case_number") or "").strip()
    court_name = (todo.get("court_name") or "").strip()
    todo_type = (todo.get("todo_type") or "").strip() or "待辦"
    todo_date = todo.get("todo_date")
    todo_time = todo.get("todo_time")
    desc = (todo.get("description") or "").strip()
    src = (todo.get("source_file") or "").strip()
    dedup_key = ""
    try:
        from osc_headless.gcal_dedup import build_dedup_key_from_todo
        dedup_key = build_dedup_key_from_todo(todo, tz=tz)
    except Exception:
        dedup_key = ""

    key = court_case_no or case_number
    source_is_human_calendar = src.startswith("gcal_import") or src.startswith("gcal_mirror")
    if source_is_human_calendar and todo_type == "行事曆事件" and desc:
        summary = desc[:120]
    else:
        summary = "⚖️ "
        if client:
            summary += f"{client} "
        if key:
            summary += f"{key} "
        summary += todo_type

    lines = []
    if court_name:
        lines.append(f"法院：{court_name}")
    if court_case_no:
        lines.append(f"法院案號：{court_case_no}")
    if case_number:
        lines.append(f"系統案號：{case_number}")
    if desc:
        lines.append(f"內容：{desc}")
    if src:
        lines.append(f"來源檔案：{src}")

    # 顏色對照 (與 code/osc.py EVENT_COLORS 一致)
    _EVENT_COLORS = {
        '開庭': '9', '宣判': '9', '調解': '9', '言詞辯論': '9', '準備程序': '9', '審理程序': '9',
        '會議': '3', '律見': '3', '電話聯繫': '3', '視訊會議': '3', '法律諮詢': '3', '快速新增會議': '3',
        '補正': '6', '繳費': '6', '上訴': '6', '抗告': '6', '再抗告': '6', '異議': '6',
        '陳述意見': '6', '陳報': '6', '提出資料': '6', '執行': '6', '法扶開辦末日': '6',
        '閱卷': '8', '來所提供資料': '8', '審查': '8', '其他': '8',
    }
    color_id = _EVENT_COLORS.get(todo_type, '8')

    body: Dict[str, Any] = {
        "summary": summary.strip(),
        "description": "\n".join(lines).strip(),
        "colorId": color_id,
        # Use extended properties for idempotency / debugging (future use).
        "extendedProperties": {
            "private": {
                "magi_case_number": case_number,
                "magi_todo_id": str(todo.get("id") or ""),
                "magi_todo_type": todo_type,
                "magi_dedup_key": dedup_key,
                "magi_source": "osc_gcal_sync",
                "magi_created_by": "MAGI",
            }
        },
    }

    # All-day vs timed event:
    # - If todo_time is present -> timed
    # - Otherwise -> all-day
    d_str = str(todo_date) if todo_date is not None else ""
    t_str = str(todo_time) if todo_time is not None else ""
    if d_str and t_str and t_str != "None":
        # Expect formats like YYYY-MM-DD and HH:MM:SS
        start_dt = f"{d_str}T{t_str}"
        body["start"] = {"dateTime": start_dt, "timeZone": tz}
        # Default duration: 60 minutes
        try:
            hh, mm, ss = [int(x) for x in t_str.split(":")]
            end = (datetime.strptime(d_str, "%Y-%m-%d") + timedelta(hours=hh, minutes=mm, seconds=ss) + timedelta(minutes=60))
            body["end"] = {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz}
        except Exception:
            body["end"] = {"dateTime": start_dt, "timeZone": tz}
    else:
        # All-day: end date is exclusive
        body["start"] = {"date": d_str}
        try:
            end_d = (datetime.strptime(d_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            end_d = d_str
        body["end"] = {"date": end_d}

    return body


def _classify_gcal_import_todo_type(summary: str, description: str = "") -> str:
    text = f"{summary or ''}\n{description or ''}"
    for kw, todo_type in [
        ("律見", "律見"),
        ("律師接見", "律見"),
        ("接見", "律見"),
        ("會議", "會議"),
        ("會面", "會議"),
        ("開會", "會議"),
        ("視訊會議", "視訊會議"),
        ("電話聯繫", "電話聯繫"),
        ("電聯", "電話聯繫"),
        ("開庭", "開庭"),
        ("庭期", "開庭"),
        ("準備程序", "準備程序"),
        ("言詞辯論", "言詞辯論"),
        ("審理程序", "審理程序"),
        ("宣判", "宣判"),
        ("期日", "期日"),
        ("調解", "調解"),
        ("期限", "期限"),
        ("補正", "補正"),
        ("繳費", "繳費"),
        ("閱卷", "閱卷"),
        ("筆錄", "筆錄"),
        ("來所提供資料", "來所提供資料"),
        ("來所簽", "來所提供資料"),
        ("來所用印", "來所提供資料"),
        ("面談", "會議"),
        ("提出", "提出"),
        ("答辯", "答辯"),
        ("法扶開辦末日", "法扶開辦末日"),
        ("法扶", "法扶"),
    ]:
        if kw in text:
            return todo_type
    return "行事曆事件"


def _target_calendar_aliases(service: Any, calendar_id: str) -> set[str]:
    raw = (calendar_id or "primary").strip() or "primary"
    aliases = {raw.lower()}
    try:
        items = service.calendarList().list().execute().get("items", [])
        for item in items:
            cid = str(item.get("id") or "").strip()
            if not cid:
                continue
            if item.get("primary") or raw == cid:
                aliases.add(cid.lower())
                if item.get("primary"):
                    aliases.add("primary")
    except Exception:
        aliases.add("primary")
    return aliases


def _annotate_unknown_import_calendar_sources(
    conn: Any,
    service: Any,
    *,
    calendar_ids: list[str],
    target_calendar_ids: set[str],
    limit: int = 100,
) -> Dict[str, Any]:
    """Backfill old `gcal_import` rows with their source calendar id."""

    out: Dict[str, Any] = {"updated": 0, "unresolved": 0, "items": []}
    ids = [str(x or "").strip() for x in calendar_ids if str(x or "").strip()]
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, google_calendar_id
            FROM case_todos
            WHERE source_file = 'gcal_import'
              AND google_calendar_id IS NOT NULL
              AND google_calendar_id <> ''
              AND todo_date IS NOT NULL
              AND todo_date >= CURDATE()
              AND todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
            ORDER BY todo_date ASC, id ASC
            LIMIT %s
            """,
            (max(1, min(int(limit or 100), 500)),),
        )
        rows = cur.fetchall() or []
        for row in rows:
            gid = str((row or {}).get("google_calendar_id") or "").strip()
            if not gid:
                continue
            active_hits: list[str] = []
            cancelled_hits: list[str] = []
            for cid in ids:
                try:
                    ev = service.events().get(calendarId=cid, eventId=gid).execute()
                    status = str((ev or {}).get("status") or "").lower()
                    if status == "cancelled":
                        cancelled_hits.append(cid)
                    else:
                        active_hits.append(cid)
                except Exception:
                    continue
            if not active_hits:
                out["unresolved"] += 1
                continue
            chosen = ""
            for cid in active_hits:
                if cid.lower() not in target_calendar_ids and cid.lower() != "primary":
                    chosen = cid
                    break
            if not chosen:
                chosen = active_hits[0]
            cur.execute("UPDATE case_todos SET source_file=%s WHERE id=%s", (f"gcal_import:{chosen}", int(row.get("id") or 0)))
            out["updated"] += 1
            if len(out["items"]) < 20:
                out["items"].append({
                    "id": row.get("id"),
                    "source_file": f"gcal_import:{chosen}",
                    "active_hits": active_hits,
                    "cancelled_hits": cancelled_hits,
                })
        if out["updated"]:
            conn.commit()
    finally:
        cur.close()
    return out


def _materialize_imported_calendar_mirrors(
    conn: Any,
    *,
    limit: int = 100,
    target_calendar_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Create OSC-owned mirror rows for case-specific imported calendar events.

    Rows imported from Google Calendar (`gcal_import:*`) are intentionally kept
    out of the normal push query to avoid import/export loops.  If one of those
    rows is a future case event and there is no OSC-owned todo for the same
    case/type/date/time, create a `gcal_mirror:*` row.  The regular sync path
    will then push that mirror to MAGI's primary calendar.
    """

    limit = max(1, min(int(limit or 100), 500))
    target_ids = {str(x or "").lower() for x in (target_calendar_ids or set()) if str(x or "").strip()}
    out: Dict[str, Any] = {"inserted": 0, "skipped": 0, "skipped_target_calendar": 0, "skipped_unknown_source": 0, "items": []}
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT
                ct.id AS source_todo_id,
                ct.case_number,
                COALESCE(NULLIF(ct.client_name, ''), c.client_name, '') AS client_name,
                ct.todo_type,
                ct.todo_date,
                ct.todo_time,
                ct.description,
                ct.source_file
            FROM case_todos ct
            LEFT JOIN cases c
              ON c.case_number COLLATE utf8mb4_unicode_ci
               = ct.case_number COLLATE utf8mb4_unicode_ci
            WHERE ct.source_file LIKE 'gcal_import%%'
              AND ct.todo_date IS NOT NULL
              AND ct.todo_date >= CURDATE()
              AND ct.todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
              AND (ct.status IS NULL OR ct.status = '' OR ct.status = 'pending')
              AND COALESCE(ct.case_number, '') <> ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM case_todos m
                  WHERE m.case_number COLLATE utf8mb4_unicode_ci
                        = ct.case_number COLLATE utf8mb4_unicode_ci
                    AND COALESCE(m.todo_type, '') = COALESCE(ct.todo_type, '')
                    AND m.todo_date = ct.todo_date
                    AND COALESCE(m.todo_time, '') = COALESCE(ct.todo_time, '')
                    AND COALESCE(m.status, '') <> 'deleted'
                    AND m.source_file LIKE 'gcal_mirror:%%'
                  LIMIT 1
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM case_todos o
                  WHERE o.case_number COLLATE utf8mb4_unicode_ci
                        = ct.case_number COLLATE utf8mb4_unicode_ci
                    AND COALESCE(o.todo_type, '') = COALESCE(ct.todo_type, '')
                    AND o.todo_date = ct.todo_date
                    AND COALESCE(o.todo_time, '') = COALESCE(ct.todo_time, '')
                    AND COALESCE(o.status, '') <> 'deleted'
                    AND (o.source_file IS NULL OR o.source_file = ''
                         OR (o.source_file NOT LIKE 'gcal_import%%'
                             AND o.source_file NOT LIKE 'gcal_mirror:%%'))
                  LIMIT 1
              )
            ORDER BY ct.todo_date ASC, ct.todo_time ASC, ct.id ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall() or []
        for row in rows:
            source_file = str((row or {}).get("source_file") or "")
            if not source_file.startswith("gcal_import"):
                out["skipped"] += 1
                continue
            source_calendar = source_file.split(":", 1)[1].strip() if ":" in source_file else ""
            if not source_calendar:
                out["skipped_unknown_source"] += 1
                continue
            if source_calendar.lower() in target_ids or source_calendar.lower() == "primary":
                out["skipped_target_calendar"] += 1
                continue
            case_number = str((row or {}).get("case_number") or "").strip()
            if not case_number:
                out["skipped"] += 1
                continue
            mirror_source = "gcal_mirror:" + source_calendar
            cur.execute(
                """
                INSERT INTO case_todos
                  (case_number, client_name, todo_type, todo_date, todo_time,
                   description, source_file, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                """,
                (
                    case_number,
                    str((row or {}).get("client_name") or ""),
                    str((row or {}).get("todo_type") or "行事曆事件"),
                    (row or {}).get("todo_date"),
                    (row or {}).get("todo_time") or None,
                    str((row or {}).get("description") or "")[:500],
                    mirror_source,
                ),
            )
            out["inserted"] += 1
            if len(out["items"]) < 20:
                out["items"].append(
                    {
                        "source_todo_id": (row or {}).get("source_todo_id"),
                        "case_number": case_number,
                        "client_name": str((row or {}).get("client_name") or ""),
                        "todo_type": str((row or {}).get("todo_type") or ""),
                        "todo_date": str((row or {}).get("todo_date") or ""),
                        "todo_time": str((row or {}).get("todo_time") or ""),
                        "source_file": mirror_source,
                    }
                )
        if out["inserted"]:
            conn.commit()
    finally:
        cur.close()
    return out


def _calendar_todo_type_priority(todo_type: str) -> int:
    t = str(todo_type or "").strip()
    return {
        "繳費": 100,
        "補正": 96,
        "陳報": 92,
        "陳述意見": 90,
        "表示意見": 88,
        "上訴": 86,
        "抗告": 86,
        "再抗告": 86,
        "異議": 84,
        "再議": 84,
        "提出資料": 74,
        "開庭": 72,
        "調解": 72,
        "準備程序": 72,
        "言詞辯論": 72,
        "審理": 72,
        "審理程序": 72,
        "訊問": 72,
        "調查": 72,
        "確認": 30,
        "待辦": 10,
    }.get(t, 50)


def _todo_calendar_kind(todo_type: str, description: str = "") -> str:
    try:
        from osc_headless.gcal_dedup import classify_event_kind

        return classify_event_kind(f"{todo_type or ''} {description or ''}", todo_type=todo_type or "")
    except Exception:
        text = f"{todo_type or ''} {description or ''}"
        tt = str(todo_type or "").strip()
        if any(k in text for k in ("開庭", "準備程序", "言詞辯論", "審理", "訊問", "調解", "調查")):
            return "hearing"
        if tt == "繳費" or any(k in text for k in ("繳納", "繳費", "裁判費", "規費", "聲請費")):
            return "deadline_payment"
        if tt == "補正" or any(k in text for k in ("補正", "補繳", "補提", "補送", "補件")):
            return "deadline_correction"
        if tt in {"上訴", "抗告", "再抗告", "異議", "再議"} or any(k in text for k in ("上訴", "抗告", "再抗告", "異議", "再議")):
            return "deadline_challenge"
        if tt in {"陳報", "陳述意見", "提出資料", "表示意見"} or any(k in text for k in ("陳報", "陳述意見", "表示意見", "提出資料", "具狀表示", "回覆", "確答", "陳明")):
            return "deadline_response"
        if any(k in text for k in ("期限", "日內", "日前", "文到", "送達後", "送達翌日起")):
            return "deadline"
        return "other"


def _is_google_gone_error(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) in {404, 410}
    except Exception:
        return "404" in str(exc) or "410" in str(exc)


_CHALLENGE_CALENDAR_TYPES = {"上訴", "抗告", "再抗告", "異議", "再議"}
_COURT_DOC_KIND_RE = re.compile(r"(判決|裁定|不起訴處分書|支付命令)")
_COURT_CASE_NO_RE = re.compile(r"(\d{2,3})年度(.{1,12}?字)第0*(\d{1,6})號")


def _calendar_court_doc_identity(row: Dict[str, Any]) -> tuple[str, str]:
    text = re.sub(r"\s+", "", f"{row.get('source_file') or ''} {row.get('description') or ''}")
    kind_match = _COURT_DOC_KIND_RE.search(text)
    kind = kind_match.group(1) if kind_match else ""
    case_match = _COURT_CASE_NO_RE.search(text)
    if not case_match:
        return kind, ""
    roc_year, case_word, serial = case_match.groups()
    return kind, f"{int(roc_year)}年度{case_word}第{int(serial)}號"


def _calendar_source_specificity(row: Dict[str, Any]) -> int:
    source = str(row.get("source_file") or "")
    desc = str(row.get("description") or "")
    score = 0
    if re.search(r"^(20\d{6}|\d{7})(?:\s|$)", Path(source).name):
        score += 20
    kind, court_no = _calendar_court_doc_identity(row)
    if kind:
        score += 10
    if court_no:
        score += 20
    score += min(len(Path(source).name), 120) // 20
    if "MAGI分享連結：" in desc:
        score += 2
    return score


def _calendar_row_date(row: Dict[str, Any]) -> Optional[datetime]:
    raw = row.get("todo_date")
    if isinstance(raw, datetime):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except Exception:
        return None


def _cleanup_duplicate_calendar_todos(
    conn: Any,
    service: Any,
    *,
    calendar_id: str,
    target_calendar_ids: set[str],
    limit: int = 300,
    lookback_days: int = 14,
) -> Dict[str, Any]:
    """Merge duplicate future calendar todos regardless of source calendar.

    The same hearing/deadline may be present from an old OSC push, a shared
    calendar import, and the newer PDF scanner.  Treat same
    case/date/time/kind as one calendar obligation before pushing/repairing.
    """

    out: Dict[str, Any] = {
        "groups": 0,
        "marked": 0,
        "deleted_events": 0,
        "delete_failed": 0,
        "db_only_marked": 0,
        "purged_deleted_events": 0,
        "purge_failed": 0,
        "items": [],
    }
    lookback_days = max(0, min(int(lookback_days or 0), 90))
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            f"""
            SELECT id, case_number, client_name, todo_type, todo_date, todo_time,
                   description, source_file, google_calendar_id, status
            FROM case_todos
            WHERE todo_date IS NOT NULL
              AND todo_date >= DATE_SUB(CURDATE(), INTERVAL {lookback_days} DAY)
              AND todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
              AND (status IS NULL OR status = '' OR status IN ('pending', 'calendar_deduped', 'deleted'))
            ORDER BY todo_date ASC, todo_time ASC, id ASC
            LIMIT %s
            """,
            (max(1, min(int(limit or 300), 1000)),),
        )
        rows = [dict(r) for r in (cur.fetchall() or []) if isinstance(r, dict)]

        def _event_calendar_for(row: Dict[str, Any]) -> str:
            source = str(row.get("source_file") or "")
            if source.startswith("gcal_import:"):
                return source.split(":", 1)[1].strip()
            return calendar_id or "primary"

        active_rows: list[Dict[str, Any]] = []
        for row in rows:
            row_id = int(row.get("id") or 0)
            status = str(row.get("status") or "").strip()
            if status != "deleted":
                active_rows.append(row)
                continue
            gid = str(row.get("google_calendar_id") or "").strip()
            if not row_id or not gid:
                continue
            event_calendar_id = _event_calendar_for(row)
            delete_ok = True
            try:
                service.events().delete(calendarId=event_calendar_id, eventId=gid).execute()
            except Exception as exc:
                if not _is_google_gone_error(exc):
                    delete_ok = False
                    out["purge_failed"] += 1
                    if len(out["items"]) < 20:
                        out["items"].append({
                            "id": row_id,
                            "case_number": str(row.get("case_number") or ""),
                            "todo_date": str(row.get("todo_date") or ""),
                            "todo_time": str(row.get("todo_time") or ""),
                            "source_file": str(row.get("source_file") or ""),
                            "google_calendar_id": gid,
                            "calendar_id": event_calendar_id,
                            "action": "deleted_event_purge_failed",
                            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                        })
            if not delete_ok:
                continue
            cur.execute(
                """
                UPDATE case_todos
                SET google_calendar_id=''
                WHERE id=%s
                """,
                (row_id,),
            )
            out["purged_deleted_events"] += int(getattr(cur, "rowcount", 0) or 0)
            if len(out["items"]) < 20:
                out["items"].append({
                    "id": row_id,
                    "case_number": str(row.get("case_number") or ""),
                    "todo_date": str(row.get("todo_date") or ""),
                    "todo_time": str(row.get("todo_time") or ""),
                    "source_file": str(row.get("source_file") or ""),
                    "google_calendar_id": gid,
                    "calendar_id": event_calendar_id,
                    "action": "deleted_event_purged",
                })

        known_by_slot: Dict[tuple[str, str, str], list[Dict[str, Any]]] = {}
        groups: Dict[tuple[str, str, str, str], list[Dict[str, Any]]] = {}
        for row in active_rows:
            kind = _todo_calendar_kind(str(row.get("todo_type") or ""), str(row.get("description") or ""))
            if kind == "other":
                kind = str(row.get("todo_type") or "other")
            row["_calendar_kind"] = kind
            case_no = str(row.get("case_number") or "").strip()
            if case_no:
                slot = (str(row.get("todo_date") or "").strip(), str(row.get("todo_time") or "").strip(), kind)
                known_by_slot.setdefault(slot, []).append(row)

        for row in active_rows:
            kind = str(row.get("_calendar_kind") or "")
            case_no = str(row.get("case_number") or "").strip()
            if not case_no:
                desc = str(row.get("description") or "")
                slot = (str(row.get("todo_date") or "").strip(), str(row.get("todo_time") or "").strip(), kind)
                matched = [
                    cand for cand in known_by_slot.get(slot, [])
                    if str(cand.get("client_name") or "").strip()
                    and str(cand.get("client_name") or "").strip() in desc
                ]
                matched_case_numbers = {str(c.get("case_number") or "").strip() for c in matched if str(c.get("case_number") or "").strip()}
                if len(matched_case_numbers) == 1:
                    case_no = next(iter(matched_case_numbers))
                    row["_resolved_case_number"] = case_no
                else:
                    continue
            key = (
                case_no,
                str(row.get("todo_date") or "").strip(),
                str(row.get("todo_time") or "").strip(),
                kind,
            )
            groups.setdefault(key, []).append(row)

        # Google Drive/NAS imports can leave two filenames for the same court
        # judgment/ruling.  Their appeal/objection deadlines may differ by a day
        # because one file has an OSC received-date prefix and the other falls
        # back to mtime.  Collapse same court document within a small date window.
        challenge_buckets: Dict[tuple[str, str, str], list[tuple[Dict[str, Any], str, Optional[datetime]]]] = {}
        for row in active_rows:
            todo_type = str(row.get("todo_type") or "").strip()
            if todo_type not in _CHALLENGE_CALENDAR_TYPES:
                continue
            case_no = str(row.get("case_number") or row.get("_resolved_case_number") or "").strip()
            if not case_no:
                continue
            kind, court_no = _calendar_court_doc_identity(row)
            if not kind:
                continue
            challenge_buckets.setdefault((case_no, todo_type, kind), []).append((row, court_no, _calendar_row_date(row)))

        for (case_no, todo_type, kind), bucket_rows in challenge_buckets.items():
            clusters: list[list[tuple[Dict[str, Any], str, Optional[datetime]]]] = []
            for row, court_no, row_date in sorted(bucket_rows, key=lambda item: (
                item[2] or datetime.max,
                -_calendar_source_specificity(item[0]),
                int(item[0].get("id") or 0),
            )):
                placed = False
                for cluster in clusters:
                    compatible = True
                    for other, other_court_no, other_date in cluster:
                        if row_date and other_date and abs((row_date.date() - other_date.date()).days) > 3:
                            compatible = False
                            break
                        if court_no and other_court_no and court_no != other_court_no:
                            compatible = False
                            break
                    if compatible:
                        cluster.append((row, court_no, row_date))
                        placed = True
                        break
                if not placed:
                    clusters.append([(row, court_no, row_date)])

            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                rows_only = [item[0] for item in cluster]
                preferred = sorted(rows_only, key=lambda r: (-_calendar_source_specificity(r), int(r.get("id") or 0)))[0]
                preferred_kind, preferred_court_no = _calendar_court_doc_identity(preferred)
                identity = preferred_court_no or str(preferred.get("source_file") or preferred.get("description") or "")[:80]
                group_date = str(preferred.get("todo_date") or "").strip()
                group_time = str(preferred.get("todo_time") or "").strip()
                group_key = (case_no, group_date, group_time, f"doc:{todo_type}:{preferred_kind or kind}:{identity}")
                members = groups.setdefault(group_key, [])
                for candidate in rows_only:
                    if not any(int(m.get("id") or 0) == int(candidate.get("id") or 0) for m in members):
                        members.append(candidate)

        # A PDF scanner bug or old Google import can create two hearing rows from
        # the same court notice (same case/date/source file) with different times.
        # Keep the richer OSC/PDF row and mark the older row as a DB duplicate so
        # it cannot keep overwriting the calendar event.
        source_hearing_buckets: Dict[tuple[str, str, str, str, str], list[Dict[str, Any]]] = {}
        for row in active_rows:
            kind = str(row.get("_calendar_kind") or "")
            if kind != "hearing":
                continue
            case_no = str(row.get("case_number") or row.get("_resolved_case_number") or "").strip()
            source = str(row.get("source_file") or "").strip()
            if not case_no or not source or source.startswith("gcal_import:") or source.startswith("gcal_mirror:"):
                continue
            source_hearing_buckets.setdefault(
                (
                    case_no,
                    str(row.get("todo_date") or "").strip(),
                    kind,
                    str(row.get("todo_type") or "").strip(),
                    source,
                ),
                [],
            ).append(row)

        for (case_no, todo_date, kind, todo_type, source), rows_only in source_hearing_buckets.items():
            if len(rows_only) < 2:
                continue
            preferred = sorted(rows_only, key=lambda r: (-_calendar_source_specificity(r), int(r.get("id") or 0)))[0]
            group_time = str(preferred.get("todo_time") or "").strip()
            group_key = (case_no, todo_date, group_time, f"source:{kind}:{todo_type}:{Path(source).name}")
            members = groups.setdefault(group_key, [])
            for candidate in rows_only:
                if not any(int(m.get("id") or 0) == int(candidate.get("id") or 0) for m in members):
                    members.append(candidate)

        def _canonical_sort_key(row: Dict[str, Any]) -> tuple[int, int, int, int, int]:
            source = str(row.get("source_file") or "")
            gid = str(row.get("google_calendar_id") or "").strip()
            status = str(row.get("status") or "").strip()
            row_id = int(row.get("id") or 0)
            inactive = 1 if status == "calendar_deduped" else 0
            priority = -_calendar_todo_type_priority(str(row.get("todo_type") or ""))
            specificity = -_calendar_source_specificity(row)
            if not source.startswith("gcal_import:") and gid:
                return (inactive, 0, priority, specificity, row_id)
            if not source.startswith("gcal_import:"):
                return (inactive, 1, priority, specificity, row_id)
            if gid:
                return (inactive, 2, priority, specificity, row_id)
            return (inactive, 3, priority, specificity, row_id)

        processed_duplicate_ids: set[int] = set()
        for key, members in groups.items():
            members = [m for m in members if int(m.get("id") or 0) not in processed_duplicate_ids]
            if len(members) < 2:
                continue
            # Same case/date/time/kind is already a same obligation. Collapse it
            # before sync so a DB-only duplicate cannot be pushed into Google
            # later. The kind is intentionally narrow (payment/correction/
            # response/challenge) to avoid merging separate duties.
            out["groups"] += 1
            canonical = sorted(members, key=_canonical_sort_key)[0]
            canonical_id = int(canonical.get("id") or 0)
            canonical_gid = str(canonical.get("google_calendar_id") or "").strip()
            for row in members:
                row_id = int(row.get("id") or 0)
                if not row_id or row_id == canonical_id:
                    continue
                source = str(row.get("source_file") or "")
                gid = str(row.get("google_calendar_id") or "").strip()
                event_calendar_id = _event_calendar_for(row)
                should_delete_event = bool(gid) and bool(event_calendar_id) and gid != canonical_gid
                delete_ok = True
                if should_delete_event:
                    try:
                        service.events().delete(calendarId=event_calendar_id, eventId=gid).execute()
                        out["deleted_events"] += 1
                    except Exception as exc:
                        if _is_google_gone_error(exc):
                            out["deleted_events"] += 1
                        else:
                            delete_ok = False
                            out["delete_failed"] += 1
                            if len(out["items"]) < 20:
                                out["items"].append({
                                    "id": row_id,
                                    "case_number": key[0],
                                    "todo_date": key[1],
                                    "todo_time": key[2],
                                    "kind": key[3],
                                    "action": "delete_failed",
                                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                                })
                if not delete_ok:
                    continue
                cur.execute(
                    """
                    UPDATE case_todos
                    SET status='calendar_deduped',
                        google_calendar_id=''
                    WHERE id=%s
                    """,
                    (row_id,),
                )
                out["marked"] += int(getattr(cur, "rowcount", 0) or 0)
                processed_duplicate_ids.add(row_id)
                if not should_delete_event:
                    out["db_only_marked"] += 1
                if len(out["items"]) < 20:
                    out["items"].append({
                        "id": row_id,
                        "kept_id": canonical_id,
                        "case_number": key[0],
                        "resolved_case_number": str(row.get("_resolved_case_number") or ""),
                        "todo_date": key[1],
                        "todo_time": key[2],
                        "kind": key[3],
                        "source_file": source,
                        "google_calendar_id": gid,
                        "calendar_id": event_calendar_id,
                        "action": "calendar_deduped",
                    })
        if out["marked"] or out["purged_deleted_events"]:
            conn.commit()
    finally:
        cur.close()
    return out


def task_gcal_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sync unsynced case_todos into Google Calendar, then store google_calendar_id back to DB.
    """
    try:
        from osc_headless.db import (  # type: ignore
            connect_mysql,
            db_config_from_env,
            ensure_osc_min_schema,
            ensure_cases_schema,
            list_unsynced_todos_with_case_info,
            set_todo_google_calendar_id,
        )
    except Exception as e:
        return {"ok": False, "error": f"missing_db_helpers:{type(e).__name__}"}

    limit = int((payload or {}).get("limit") or 60)
    dry_run = bool((payload or {}).get("dry_run", False))
    calendar_id = str((payload or {}).get("calendar_id") or "").strip()
    tz = (payload or {}).get("time_zone") or os.environ.get("MAGI_TIME_ZONE") or "Asia/Taipei"
    dedup_enabled = _env_bool("MAGI_GCAL_DEDUP_ENABLED", False)
    dedup_dry_run = _env_bool("MAGI_GCAL_DEDUP_DRY_RUN", True)
    repair_existing = bool((payload or {}).get("repair_existing")) or _env_bool("MAGI_GCAL_REPAIR_EXISTING", False)
    mirror_imported = bool((payload or {}).get("mirror_imported", False)) or _env_bool("MAGI_GCAL_MIRROR_IMPORTED", False)
    history_cutoff = _gcal_history_cutoff_date(payload or {})
    try:
        repair_lookback_days = int((payload or {}).get("repair_lookback_days") or os.environ.get("MAGI_GCAL_REPAIR_LOOKBACK_DAYS") or 30)
    except Exception:
        repair_lookback_days = 30
    repair_lookback_days = max(0, min(repair_lookback_days, 365))
    repair_cutoff = max(history_cutoff, datetime.now() - timedelta(days=repair_lookback_days))
    repair_todo_ids: list[int] = []
    for raw_id in (payload or {}).get("repair_todo_ids") or []:
        try:
            todo_id = int(raw_id)
        except Exception:
            continue
        if todo_id > 0 and todo_id not in repair_todo_ids:
            repair_todo_ids.append(todo_id)
        if len(repair_todo_ids) >= 400:
            break

    credentials_path = ((payload or {}).get("credentials_path") or os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH") or "").strip()
    token_path = ((payload or {}).get("token_path") or os.environ.get("MAGI_GOOGLE_CALENDAR_TOKEN_PATH") or "").strip()
    if not credentials_path:
        credentials_path = _default_gcal_credentials_path()
    if not token_path:
        token_path = _default_gcal_token_path()

    svc = _build_google_calendar_service(credentials_path, token_path, interactive=False)
    if not svc.get("ok"):
        out = {"ok": False, "error": svc.get("error", "gcal_service_failed")}
        if svc.get("need_interactive_oauth"):
            out["need_interactive_oauth"] = True
            out["token_path"] = svc.get("token_path", token_path)
            out["credentials_path"] = credentials_path
        _eventlog("osc:gcal_sync", ok=False, payload={"error": out.get("error", "")[:240]})
        return out

    service = svc.get("service")
    if not service:
        return {"ok": False, "error": "gcal_service_missing"}

    # DB: fetch unsynced todos
    cfg = db_config_from_env(prefix="OSC_DB_")
    conn = None
    mirrored_imports: Dict[str, Any] = {"inserted": 0, "skipped": 0, "items": []}
    annotated_imports: Dict[str, Any] = {"updated": 0, "unresolved": 0, "items": []}
    duplicate_cleanup: Dict[str, Any] = {"groups": 0, "marked": 0, "deleted_events": 0, "delete_failed": 0, "items": []}
    try:
        conn = connect_mysql(cfg)
        ensure_osc_min_schema(conn)
        ensure_cases_schema(conn)
        if not calendar_id:
            try:
                cur = conn.cursor(dictionary=True)
                try:
                    cur.execute("SELECT value FROM settings WHERE `key`=%s LIMIT 1", ("gcal_calendar_id",))
                    row = cur.fetchone()
                    if isinstance(row, dict):
                        calendar_id = str(row.get("value") or "").strip()
                    elif row:
                        calendar_id = str(row[0] or "").strip()
                finally:
                    cur.close()
            except Exception:
                calendar_id = ""
        if not calendar_id:
            calendar_id = "primary"
        target_calendar_ids = _target_calendar_aliases(service, calendar_id)
        if mirror_imported and not dry_run:
            try:
                cal_items = service.calendarList().list().execute().get("items", [])
                visible_calendar_ids = [str(c.get("id") or "") for c in cal_items if c.get("id")]
            except Exception:
                visible_calendar_ids = [calendar_id]
            annotated_imports = _annotate_unknown_import_calendar_sources(
                conn,
                service,
                calendar_ids=visible_calendar_ids,
                target_calendar_ids=target_calendar_ids,
                limit=limit,
            )
            mirrored_imports = _materialize_imported_calendar_mirrors(
                conn,
                limit=limit,
                target_calendar_ids=target_calendar_ids,
            )
        if not dry_run:
            duplicate_cleanup = _cleanup_duplicate_calendar_todos(
                conn,
                service,
                calendar_id=calendar_id,
                target_calendar_ids=target_calendar_ids,
                limit=max(limit * 4, 300),
            )
        todos = list_unsynced_todos_with_case_info(conn, limit=limit)
        if repair_existing:
            repair_limit = max(1, min(int((payload or {}).get("repair_limit") or limit), 400))
            cur = conn.cursor(dictionary=True)
            try:
                cur.execute(
                    """
                    SELECT
                        ct.id,
                        ct.case_number,
                        ct.client_name,
                        ct.todo_type,
                        ct.todo_date,
                        ct.todo_time,
                        ct.description,
                        ct.source_file,
                        ct.google_calendar_id,
                        COALESCE(c.court_name, '') AS court_name,
                        COALESCE(NULLIF(c.court_case_no, ''), c.court_case_number, '') AS court_case_number
                    FROM case_todos ct
                    LEFT JOIN cases c
                      ON c.case_number COLLATE utf8mb4_unicode_ci
                       = ct.case_number COLLATE utf8mb4_unicode_ci
                    WHERE ct.google_calendar_id IS NOT NULL
                      AND ct.google_calendar_id <> ''
                      AND ct.todo_date IS NOT NULL
                      AND ct.todo_date >= %s
                      AND ct.todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
                      AND (ct.status IS NULL OR ct.status = '' OR ct.status IN ('pending', 'calendar_deduped'))
                      AND (ct.source_file IS NULL OR ct.source_file = '' OR ct.source_file NOT LIKE 'gcal_import%%')
                    ORDER BY ct.todo_date ASC, ct.todo_time ASC, ct.id ASC
                    LIMIT %s
                    """,
                    (repair_cutoff.strftime("%Y-%m-%d"), repair_limit),
                )
                seen_ids = {int(row.get("id") or 0) for row in todos if isinstance(row, dict)}
                for row in cur.fetchall() or []:
                    rid = int((row or {}).get("id") or 0)
                    if rid and rid not in seen_ids:
                        todos.append(dict(row))
                        seen_ids.add(rid)
                if repair_todo_ids:
                    wanted_ids = [x for x in repair_todo_ids if x not in seen_ids]
                    if wanted_ids:
                        placeholders = ",".join(["%s"] * len(wanted_ids))
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
                                ct.google_calendar_id,
                                COALESCE(c.court_name, '') AS court_name,
                                COALESCE(NULLIF(c.court_case_no, ''), c.court_case_number, '') AS court_case_number
                            FROM case_todos ct
                            LEFT JOIN cases c
                              ON c.case_number COLLATE utf8mb4_unicode_ci
                               = ct.case_number COLLATE utf8mb4_unicode_ci
                            WHERE ct.id IN ({placeholders})
                              AND ct.google_calendar_id IS NOT NULL
                              AND ct.google_calendar_id <> ''
                              AND ct.todo_date IS NOT NULL
                              AND (ct.status IS NULL OR ct.status = '' OR ct.status IN ('pending', 'calendar_deduped'))
                            ORDER BY ct.todo_date ASC, ct.todo_time ASC, ct.id ASC
                            """,
                            tuple(wanted_ids),
                        )
                        for row in cur.fetchall() or []:
                            rid = int((row or {}).get("id") or 0)
                            if rid and rid not in seen_ids:
                                todos.append(dict(row))
                                seen_ids.add(rid)
            finally:
                cur.close()
        filtered_todos = []
        for row in todos or []:
            try:
                row_date = datetime.strptime(str(row.get("todo_date") or "")[:10], "%Y-%m-%d")
            except Exception:
                continue
            if row_date >= history_cutoff:
                filtered_todos.append(row)
        todos = filtered_todos
    except Exception as e:
        return {"ok": False, "error": f"db_failed:{type(e).__name__}: {str(e)[:220]}"}
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1483, exc_info=True)

    if not calendar_id:
        calendar_id = "primary"

    if dry_run:
        preview_items = []
        skipped_implausible = 0
        today = datetime.now().date()
        for td in todos or []:
            try:
                parsed_todo_date = datetime.strptime(str(td.get("todo_date") or ""), "%Y-%m-%d").date()
                min_date = repair_cutoff.date() if str(td.get("google_calendar_id") or "").strip() else history_cutoff.date()
                if parsed_todo_date < min_date or parsed_todo_date > today + timedelta(days=730):
                    skipped_implausible += 1
                    continue
            except Exception:
                skipped_implausible += 1
                continue
            if len(preview_items) < 25:
                preview_items.append(
                    {
                        "todo_id": td.get("id"),
                        "case_number": td.get("case_number"),
                        "client_name": td.get("client_name"),
                        "court_case_number": td.get("court_case_number"),
                        "todo_type": td.get("todo_type"),
                        "todo_date": str(td.get("todo_date") or ""),
                        "todo_time": str(td.get("todo_time") or ""),
                        "dry_run": True,
                    }
                )
        return {
            "ok": True,
            "dry_run": True,
            "limit": limit,
            "fetched": len(todos or []),
            "would_insert": max(0, len(todos or []) - skipped_implausible),
            "inserted": 0,
            "patched": 0,
            "failed": 0,
            "repair_existing": bool(repair_existing),
            "mirror_imported": bool(mirror_imported),
            "calendar_duplicate_cleanup": duplicate_cleanup,
            "skipped_implausible": skipped_implausible,
            "items": preview_items,
        }

    retry_max_attempts = int((payload or {}).get("retry_max_attempts") or os.environ.get("OSC_GCAL_RETRY_MAX_ATTEMPTS") or 2)
    retry_sleep_sec = float((payload or {}).get("retry_sleep_sec") or os.environ.get("OSC_GCAL_RETRY_SLEEP_SEC") or 0.8)
    retry_max_attempts = max(1, min(retry_max_attempts, 4))
    retry_sleep_sec = max(0.0, min(retry_sleep_sec, 5.0))

    def _is_oauth_err(err: str) -> bool:
        s = (err or "").lower()
        return (
            "invalid_grant" in s
            or "invalid_scope" in s
            or "unauthorized_client" in s
            or "access_denied" in s
            or "reauth" in s
            or "need_interactive_oauth" in s
            or "invalidcredentials" in s
            or "credentials_failed" in s
            or "insufficientpermissions" in s
            or "insufficient authentication scopes" in s
            or "token has been expired or revoked" in s
        )

    inserted = 0
    patched = 0
    failed = 0
    dedup_matched = 0
    dedup_would_match = 0
    would_insert = 0
    replaced_stale = 0
    skipped_implausible = 0
    items: List[Dict[str, Any]] = []
    failed_items: List[Dict[str, Any]] = []
    oauth_blocked = False
    oauth_error = ""
    connw = None
    def _is_stale_gcal_err(exc: Exception) -> bool:
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except Exception:
            HttpError = ()  # type: ignore[assignment]
        if HttpError and not isinstance(exc, HttpError):
            return False
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            return int(status) in {403, 404, 410}
        except Exception:
            return False

    def _write_google_calendar_id(todo_id: int, event_id: str, *, overwrite: bool = False) -> None:
        nonlocal connw
        if not event_id:
            return
        if connw is None:
            connw = connect_mysql(cfg)
            ensure_osc_min_schema(connw)
        if overwrite:
            curw = connw.cursor()
            try:
                curw.execute("UPDATE case_todos SET google_calendar_id=%s WHERE id=%s", (event_id, int(todo_id)))
                connw.commit()
            finally:
                curw.close()
        else:
            set_todo_google_calendar_id(connw, todo_id=int(todo_id), google_calendar_id=event_id)

    for td in (todos or []):
        try:
            parsed_todo_date = datetime.strptime(str(td.get("todo_date") or ""), "%Y-%m-%d").date()
            today = datetime.now().date()
            min_date = repair_cutoff.date() if str(td.get("google_calendar_id") or "").strip() else history_cutoff.date()
            if parsed_todo_date < min_date or parsed_todo_date > today + timedelta(days=730):
                skipped_implausible += 1
                continue
        except Exception:
            skipped_implausible += 1
            continue
        attempts = 0
        last_err = ""
        synced = False
        while attempts < retry_max_attempts:
            attempts += 1
            try:
                body = _todo_to_gcal_event(td, tz=str(tz))
                private = ((body.get("extendedProperties") or {}).get("private") or {})
                dedup_key = str(private.get("magi_dedup_key") or "").strip()
                existing_gid = str(td.get("google_calendar_id") or "").strip()
                stale_existing_gid = False

                if existing_gid:
                    try:
                        res = service.events().patch(calendarId=calendar_id, eventId=existing_gid, body=body).execute()
                        if str((res or {}).get("status") or "").lower() == "cancelled":
                            stale_existing_gid = True
                        else:
                            event_id = str((res or {}).get("id") or existing_gid)
                            if event_id and event_id != existing_gid:
                                _write_google_calendar_id(int(td.get("id") or 0), event_id, overwrite=True)
                            patched += 1
                            synced = True
                            if len(items) < 25:
                                items.append(
                                    {
                                        "todo_id": td.get("id"),
                                        "case_number": td.get("case_number"),
                                        "client_name": td.get("client_name"),
                                        "court_case_number": td.get("court_case_number"),
                                        "todo_type": td.get("todo_type"),
                                        "todo_date": str(td.get("todo_date") or ""),
                                        "todo_time": str(td.get("todo_time") or ""),
                                        "google_calendar_id": event_id,
                                        "attempts": attempts,
                                        "dedup_key": dedup_key,
                                        "patched_existing": True,
                                    }
                                )
                            break
                    except Exception as e:
                        last_err = f"{type(e).__name__}: {str(e)[:220]}"
                        if _is_oauth_err(last_err):
                            oauth_blocked = True
                            oauth_error = last_err
                            break
                        if not _is_stale_gcal_err(e):
                            raise
                        stale_existing_gid = True

                if dedup_enabled:
                    existing = _find_existing_gcal_event(
                        service,
                        calendar_id=calendar_id,
                        body=body,
                        dedup_key=dedup_key,
                        tz=str(tz),
                    )
                    if existing and existing.get("id"):
                        event_id = str(existing.get("id") or "").strip()
                        if dedup_dry_run:
                            dedup_would_match += 1
                        else:
                            _write_google_calendar_id(
                                int(td.get("id") or 0),
                                event_id,
                                overwrite=bool(existing_gid),
                            )
                            dedup_matched += 1
                            if stale_existing_gid:
                                replaced_stale += 1
                        synced = True
                        if len(items) < 25:
                            items.append(
                                {
                                    "todo_id": td.get("id"),
                                    "case_number": td.get("case_number"),
                                    "client_name": td.get("client_name"),
                                    "court_case_number": td.get("court_case_number"),
                                    "todo_type": td.get("todo_type"),
                                    "todo_date": str(td.get("todo_date") or ""),
                                    "todo_time": str(td.get("todo_time") or ""),
                                    "google_calendar_id": event_id,
                                    "attempts": attempts,
                                    "dedup_key": dedup_key,
                                    "matched_existing": True,
                                    "dry_run": bool(dedup_dry_run),
                                    "replaced_stale": bool(stale_existing_gid),
                                }
                            )
                        break

                    if dedup_dry_run:
                        would_insert += 1
                        synced = True
                        if len(items) < 25:
                            items.append(
                                {
                                    "todo_id": td.get("id"),
                                    "case_number": td.get("case_number"),
                                    "client_name": td.get("client_name"),
                                    "court_case_number": td.get("court_case_number"),
                                    "todo_type": td.get("todo_type"),
                                    "todo_date": str(td.get("todo_date") or ""),
                                    "todo_time": str(td.get("todo_time") or ""),
                                    "attempts": attempts,
                                    "dedup_key": dedup_key,
                                    "would_insert": True,
                                    "dry_run": True,
                                }
                            )
                        break

                res = service.events().insert(calendarId=calendar_id, body=body).execute()
                event_id = (res or {}).get("id", "") or ""
                if not event_id:
                    raise RuntimeError("gcal_insert_no_event_id")
                # Update DB: record google_calendar_id
                _write_google_calendar_id(
                    int(td.get("id") or 0),
                    event_id,
                    overwrite=bool(existing_gid),
                )
                inserted += 1
                if stale_existing_gid:
                    replaced_stale += 1
                synced = True
                if len(items) < 25:
                    items.append(
                        {
                            "todo_id": td.get("id"),
                            "case_number": td.get("case_number"),
                            "client_name": td.get("client_name"),
                            "court_case_number": td.get("court_case_number"),
                            "todo_type": td.get("todo_type"),
                            "todo_date": str(td.get("todo_date") or ""),
                            "todo_time": str(td.get("todo_time") or ""),
                            "google_calendar_id": event_id,
                            "attempts": attempts,
                            "dedup_key": dedup_key,
                            "replaced_stale": bool(stale_existing_gid),
                        }
                    )
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:220]}"
                if _is_oauth_err(last_err):
                    oauth_blocked = True
                    oauth_error = last_err
                    break
                if attempts < retry_max_attempts and retry_sleep_sec > 0:
                    time.sleep(retry_sleep_sec)
                continue

        if not synced:
            failed += 1
            if len(failed_items) < 25:
                failed_items.append(
                    {
                        "todo_id": td.get("id"),
                        "case_number": td.get("case_number"),
                        "client_name": td.get("client_name"),
                        "court_case_number": td.get("court_case_number"),
                        "todo_type": td.get("todo_type"),
                        "attempts": attempts,
                        "error": last_err,
                    }
                )
            if oauth_blocked:
                break
    try:
        if connw:
            connw.close()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1577, exc_info=True)

    if oauth_blocked:
        out = {
            "ok": False,
            "error": "need_interactive_oauth",
            "need_interactive_oauth": True,
            "token_path": token_path,
            "credentials_path": credentials_path,
            "limit": limit,
            "fetched": len(todos or []),
            "inserted": inserted,
            "patched": patched,
            "failed": failed,
            "repair_existing": bool(repair_existing),
            "mirror_imported": bool(mirror_imported),
            "annotated_imports": annotated_imports,
            "mirrored_imports": mirrored_imports,
            "calendar_duplicate_cleanup": duplicate_cleanup,
            "dedup_enabled": bool(dedup_enabled),
            "dedup_dry_run": bool(dedup_dry_run),
            "dedup_matched": dedup_matched,
            "dedup_would_match": dedup_would_match,
            "would_insert": would_insert,
            "replaced_stale": replaced_stale,
            "skipped_implausible": skipped_implausible,
            "items": items,
            "failed_items": failed_items,
            "retry_max_attempts": retry_max_attempts,
            "oauth_error": oauth_error,
        }
        _eventlog(
            "osc:gcal_sync",
            ok=False,
            payload={"fetched": len(todos or []), "inserted": inserted, "failed": failed, "error": "need_interactive_oauth"},
        )
        return out

    sync_ok = failed == 0
    out = {
        "ok": sync_ok,
        "limit": limit,
        "fetched": len(todos or []),
        "inserted": inserted,
        "patched": patched,
        "failed": failed,
        "repair_existing": bool(repair_existing),
        "mirror_imported": bool(mirror_imported),
        "annotated_imports": annotated_imports,
        "mirrored_imports": mirrored_imports,
        "calendar_duplicate_cleanup": duplicate_cleanup,
        "dedup_enabled": bool(dedup_enabled),
        "dedup_dry_run": bool(dedup_dry_run),
        "dedup_matched": dedup_matched,
        "dedup_would_match": dedup_would_match,
        "would_insert": would_insert,
        "replaced_stale": replaced_stale,
        "skipped_implausible": skipped_implausible,
        "items": items,
        "failed_items": failed_items,
        "retry_max_attempts": retry_max_attempts,
    }
    if not sync_ok:
        out["error"] = "partial_gcal_sync_failure"
    _eventlog("osc:gcal_sync", ok=sync_ok, payload={"fetched": len(todos or []), "inserted": inserted, "failed": failed})
    return out


def task_gcal_integrity_audit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Audit OSC/Google Calendar consistency after import + push.

    This catches the failure modes that cause missed calendar entries:
    - OSC/PDF-created todos that have no primary-calendar event id;
    - stale primary-calendar ids;
    - imported non-primary calendar events that were not mirrored to primary;
    - imported rows whose source calendar is still unknown;
    - mirrors that accidentally point back to the target calendar;
    - source-calendar imported events that are not visible through events.list.
    """

    try:
        from osc_headless.db import connect_mysql, db_config_from_env, ensure_osc_min_schema, ensure_cases_schema  # type: ignore
    except Exception as e:
        return {"ok": False, "error": f"missing_db_helpers:{type(e).__name__}"}

    p = payload or {}
    limit = max(1, min(int(p.get("limit") or 500), 1000))
    tz = p.get("time_zone") or os.environ.get("MAGI_TIME_ZONE") or "Asia/Taipei"
    calendar_id = str(p.get("calendar_id") or "").strip()
    check_google = bool(p.get("check_google", True))
    expect_import_mirror = bool(p.get("expect_import_mirror", False)) or _env_bool("MAGI_GCAL_MIRROR_IMPORTED", False)

    credentials_path = (p.get("credentials_path") or os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH") or "").strip()
    token_path = (p.get("token_path") or os.environ.get("MAGI_GOOGLE_CALENDAR_TOKEN_PATH") or "").strip()
    if not credentials_path:
        credentials_path = _default_gcal_credentials_path()
    if not token_path:
        token_path = _default_gcal_token_path()

    service = None
    target_calendar_ids: set[str] = {calendar_id.lower()} if calendar_id else set()
    if check_google:
        svc = _build_google_calendar_service(credentials_path, token_path, interactive=False)
        if not svc.get("ok"):
            out = {"ok": False, "error": svc.get("error", "gcal_service_failed")}
            if svc.get("need_interactive_oauth"):
                out["need_interactive_oauth"] = True
                out["token_path"] = svc.get("token_path", token_path)
            return out
        service = svc.get("service")

    def _brief(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id"),
            "case_number": str(row.get("case_number") or ""),
            "client_name": str(row.get("client_name") or ""),
            "todo_type": str(row.get("todo_type") or ""),
            "todo_date": str(row.get("todo_date") or ""),
            "todo_time": str(row.get("todo_time") or ""),
            "source_file": str(row.get("source_file") or ""),
            "google_calendar_id": str(row.get("google_calendar_id") or ""),
            "description": str(row.get("description") or "")[:140],
        }

    conn = None
    cur = None
    try:
        conn = connect_mysql(db_config_from_env(prefix="OSC_DB_"))
        ensure_osc_min_schema(conn)
        ensure_cases_schema(conn)
        cur = conn.cursor(dictionary=True)

        if not calendar_id:
            cur.execute("SELECT value FROM settings WHERE `key`=%s LIMIT 1", ("gcal_calendar_id",))
            row = cur.fetchone()
            if isinstance(row, dict):
                calendar_id = str(row.get("value") or "").strip()
            elif row:
                calendar_id = str(row[0] or "").strip()
        if not calendar_id:
            calendar_id = "primary"
        if service:
            target_calendar_ids = _target_calendar_aliases(service, calendar_id)
        if not target_calendar_ids:
            target_calendar_ids = {calendar_id.lower(), "primary"}

        cur.execute(
            """
            SELECT id, case_number, client_name, todo_type, todo_date, todo_time,
                   description, source_file, google_calendar_id
            FROM case_todos
            WHERE todo_date IS NOT NULL
              AND todo_date >= CURDATE()
              AND todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
              AND (status IS NULL OR status = '' OR status = 'pending')
              AND (source_file IS NULL OR source_file = '' OR source_file NOT LIKE 'gcal_import%%')
              AND (google_calendar_id IS NULL OR google_calendar_id = '')
            ORDER BY todo_date ASC, todo_time ASC, id ASC
            LIMIT %s
            """,
            (limit,),
        )
        missing_google_id = [_brief(r) for r in (cur.fetchall() or [])]

        cur.execute(
            """
            SELECT id, case_number, client_name, todo_type, todo_date, todo_time,
                   description, source_file, google_calendar_id
            FROM case_todos
            WHERE source_file = 'gcal_import'
              AND todo_date IS NOT NULL
              AND todo_date >= CURDATE()
              AND todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
              AND (status IS NULL OR status = '' OR status = 'pending')
              AND COALESCE(case_number, '') <> ''
            ORDER BY todo_date ASC, todo_time ASC, id ASC
            LIMIT %s
            """,
            (limit,),
        )
        unknown_import_source = [_brief(r) for r in (cur.fetchall() or [])]

        external_import_without_primary: list[Dict[str, Any]] = []
        if expect_import_mirror:
            cur.execute(
                """
                SELECT ct.id, ct.case_number, COALESCE(NULLIF(ct.client_name, ''), c.client_name, '') AS client_name,
                       ct.todo_type, ct.todo_date, ct.todo_time, ct.description,
                       ct.source_file, ct.google_calendar_id
                FROM case_todos ct
                LEFT JOIN cases c
                  ON c.case_number COLLATE utf8mb4_unicode_ci
                   = ct.case_number COLLATE utf8mb4_unicode_ci
                WHERE ct.source_file LIKE 'gcal_import:%%'
                  AND ct.todo_date IS NOT NULL
                  AND ct.todo_date >= CURDATE()
                  AND ct.todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
                  AND (ct.status IS NULL OR ct.status = '' OR ct.status = 'pending')
                  AND COALESCE(ct.case_number, '') <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM case_todos m
                      WHERE m.case_number COLLATE utf8mb4_unicode_ci
                            = ct.case_number COLLATE utf8mb4_unicode_ci
                        AND COALESCE(m.todo_type, '') = COALESCE(ct.todo_type, '')
                        AND m.todo_date = ct.todo_date
                        AND COALESCE(m.todo_time, '') = COALESCE(ct.todo_time, '')
                        AND COALESCE(m.status, '') <> 'deleted'
                        AND m.source_file LIKE 'gcal_mirror:%%'
                      LIMIT 1
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM case_todos o
                      WHERE o.case_number COLLATE utf8mb4_unicode_ci
                            = ct.case_number COLLATE utf8mb4_unicode_ci
                        AND COALESCE(o.todo_type, '') = COALESCE(ct.todo_type, '')
                        AND o.todo_date = ct.todo_date
                        AND COALESCE(o.todo_time, '') = COALESCE(ct.todo_time, '')
                        AND COALESCE(o.status, '') <> 'deleted'
                        AND (o.source_file IS NULL OR o.source_file = ''
                             OR (o.source_file NOT LIKE 'gcal_import%%'
                                 AND o.source_file NOT LIKE 'gcal_mirror:%%'))
                      LIMIT 1
                  )
                ORDER BY ct.todo_date ASC, ct.todo_time ASC, ct.id ASC
                LIMIT %s
                """,
                (limit,),
            )
            for row in cur.fetchall() or []:
                source = str(row.get("source_file") or "")
                source_calendar = source.split(":", 1)[1].strip().lower() if ":" in source else ""
                if source_calendar and source_calendar not in target_calendar_ids and source_calendar != "primary":
                    external_import_without_primary.append(_brief(row))

        cur.execute(
            """
            SELECT m.id, m.case_number, m.client_name, m.todo_type, m.todo_date, m.todo_time,
                   m.description, m.source_file, m.google_calendar_id
            FROM case_todos m
            WHERE m.source_file LIKE 'gcal_mirror:%%'
              AND COALESCE(m.status, '') <> 'deleted'
              AND m.todo_date IS NOT NULL
              AND m.todo_date >= CURDATE()
              AND m.todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
            ORDER BY m.todo_date ASC, m.todo_time ASC, m.id ASC
            LIMIT %s
            """,
            (limit,),
        )
        target_calendar_mirrors: list[Dict[str, Any]] = []
        unknown_mirrors: list[Dict[str, Any]] = []
        for row in cur.fetchall() or []:
            source = str(row.get("source_file") or "")
            source_calendar = source.split(":", 1)[1].strip().lower() if ":" in source else ""
            if not source_calendar or source_calendar == "unknown":
                unknown_mirrors.append(_brief(row))
            elif source_calendar in target_calendar_ids or source_calendar == "primary":
                target_calendar_mirrors.append(_brief(row))

        cur.execute(
            """
            SELECT case_number, todo_type, todo_date, COALESCE(todo_time, '') AS todo_time,
                   source_file, COUNT(*) AS cnt
            FROM case_todos
            WHERE source_file LIKE 'gcal_mirror:%%'
              AND COALESCE(status, '') <> 'deleted'
              AND todo_date IS NOT NULL
              AND todo_date >= CURDATE()
              AND todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
            GROUP BY case_number, todo_type, todo_date, COALESCE(todo_time, ''), source_file
            HAVING COUNT(*) > 1
            LIMIT %s
            """,
            (limit,),
        )
        duplicate_mirrors = [dict(r) for r in (cur.fetchall() or [])]

        cur.execute(
            """
            SELECT id, case_number, client_name, todo_type, todo_date, todo_time,
                   description, source_file, google_calendar_id
            FROM case_todos
            WHERE todo_date IS NOT NULL
              AND todo_date >= CURDATE()
              AND todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
              AND (status IS NULL OR status = '' OR status = 'pending')
              AND (source_file IS NULL OR source_file = '' OR source_file NOT LIKE 'gcal_import%%')
              AND google_calendar_id IS NOT NULL
              AND google_calendar_id <> ''
            ORDER BY todo_date ASC, todo_time ASC, id ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows_to_check = cur.fetchall() or []

        cur.execute(
            """
            SELECT id, case_number, client_name, todo_type, todo_date, todo_time,
                   description, source_file, google_calendar_id
            FROM case_todos
            WHERE source_file LIKE 'gcal_import:%%'
              AND todo_date IS NOT NULL
              AND todo_date >= CURDATE()
              AND todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)
              AND (status IS NULL OR status = '' OR status = 'pending')
              AND COALESCE(case_number, '') <> ''
              AND google_calendar_id IS NOT NULL
              AND google_calendar_id <> ''
            ORDER BY todo_date ASC, todo_time ASC, id ASC
            LIMIT %s
            """,
            (limit,),
        )
        source_rows_to_list_check = cur.fetchall() or []
    except Exception as e:
        return {"ok": False, "error": f"audit_db_failed:{type(e).__name__}: {str(e)[:220]}"}
    finally:
        try:
            if cur:
                cur.close()
            if conn:
                conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2273, exc_info=True)

    stale_google_events: list[Dict[str, Any]] = []
    primary_ok = 0
    source_calendar_not_listed: list[Dict[str, Any]] = []
    source_calendar_errors: list[Dict[str, Any]] = []
    cancelled_source_imports: list[Dict[str, Any]] = []
    source_list_ok = 0
    if check_google and service:
        for row in rows_to_check:
            gid = str(row.get("google_calendar_id") or "").strip()
            if not gid:
                continue
            brief = {
                "id": row.get("id"),
                "case_number": str(row.get("case_number") or ""),
                "client_name": str(row.get("client_name") or ""),
                "todo_type": str(row.get("todo_type") or ""),
                "todo_date": str(row.get("todo_date") or ""),
                "todo_time": str(row.get("todo_time") or ""),
                "source_file": str(row.get("source_file") or ""),
                "google_calendar_id": gid,
            }
            try:
                ev = service.events().get(calendarId=calendar_id, eventId=gid).execute()
                if str((ev or {}).get("status") or "").lower() == "cancelled":
                    stale_google_events.append({**brief, "status": "cancelled"})
                else:
                    primary_ok += 1
            except Exception as e:
                stale_google_events.append({**brief, "error": f"{type(e).__name__}: {str(e)[:160]}"})

        source_rows_by_calendar: Dict[str, list[Dict[str, Any]]] = {}
        for row in source_rows_to_list_check:
            source = str(row.get("source_file") or "")
            source_calendar = source.split(":", 1)[1].strip() if ":" in source else ""
            if source_calendar:
                source_rows_by_calendar.setdefault(source_calendar, []).append(row)

        now_utc = datetime.now(timezone.utc)
        time_min = (now_utc - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        time_max = (now_utc + timedelta(days=730)).isoformat().replace("+00:00", "Z")
        for source_calendar, rows in sorted(source_rows_by_calendar.items()):
            listed_ids: set[str] = set()
            listed_ok = False
            try:
                page_token = None
                while True:
                    resp = service.events().list(
                        calendarId=source_calendar,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        showDeleted=False,
                        maxResults=2500,
                        pageToken=page_token,
                    ).execute()
                    for ev in (resp or {}).get("items", []) or []:
                        ev_id = str((ev or {}).get("id") or "").strip()
                        if ev_id:
                            listed_ids.add(ev_id)
                    page_token = (resp or {}).get("nextPageToken")
                    if not page_token:
                        break
                listed_ok = True
            except Exception as e:
                for row in rows[:20]:
                    source_calendar_errors.append({
                        **_brief(row),
                        "source_calendar": source_calendar,
                        "error": f"{type(e).__name__}: {str(e)[:160]}",
                    })

            if not listed_ok:
                continue
            for row in rows:
                gid = str(row.get("google_calendar_id") or "").strip()
                if gid in listed_ids:
                    source_list_ok += 1
                    continue
                try:
                    ev = service.events().get(calendarId=source_calendar, eventId=gid).execute()
                    status = str((ev or {}).get("status") or "").lower()
                    if status == "cancelled":
                        cancelled_source_imports.append({
                            **_brief(row),
                            "source_calendar": source_calendar,
                            "status": "cancelled",
                        })
                        continue
                    source_list_ok += 1
                    continue
                except Exception:
                    pass
                source_calendar_not_listed.append({
                    **_brief(row),
                    "source_calendar": source_calendar,
                })

    cancelled_source_imports_marked = 0
    if cancelled_source_imports:
        try:
            conn2 = connect_mysql(db_config_from_env(prefix="OSC_DB_"))
            cur2 = conn2.cursor()
            try:
                for row in cancelled_source_imports:
                    cur2.execute(
                        """
                        UPDATE case_todos
                        SET status='deleted', google_calendar_id=''
                        WHERE id=%s
                          AND source_file LIKE 'gcal_import%%'
                          AND (status IS NULL OR status='' OR status!='deleted')
                        """,
                        (row.get("id"),),
                    )
                    cancelled_source_imports_marked += int(getattr(cur2, "rowcount", 0) or 0)
                conn2.commit()
            finally:
                cur2.close()
                conn2.close()
        except Exception as exc:
            source_calendar_errors.append({
                "error": f"cancelled_source_mark_failed:{type(exc).__name__}: {str(exc)[:160]}",
            })

    findings = {
        "missing_google_id": missing_google_id,
        "unknown_import_source": unknown_import_source,
        "external_import_without_primary": external_import_without_primary,
        "target_calendar_mirrors": target_calendar_mirrors,
        "unknown_mirrors": unknown_mirrors,
        "duplicate_mirrors": duplicate_mirrors,
        "stale_google_events": stale_google_events,
        "source_calendar_not_listed": source_calendar_not_listed,
        "source_calendar_errors": source_calendar_errors,
        "cancelled_source_imports": cancelled_source_imports,
    }
    summary = {
        "missing_google_id": len(missing_google_id),
        "unknown_import_source": len(unknown_import_source),
        "external_import_without_primary": len(external_import_without_primary),
        "target_calendar_mirrors": len(target_calendar_mirrors),
        "unknown_mirrors": len(unknown_mirrors),
        "duplicate_mirrors": len(duplicate_mirrors),
        "stale_google_events": len(stale_google_events),
        "checked_primary_events": len(rows_to_check),
        "primary_ok": primary_ok,
        "checked_source_events": len(source_rows_to_list_check),
        "source_list_ok": source_list_ok,
        "source_calendar_not_listed": len(source_calendar_not_listed),
        "source_calendar_errors": len(source_calendar_errors),
        "cancelled_source_imports_marked": cancelled_source_imports_marked,
    }
    ok = all(
        v == 0
        for k, v in summary.items()
        if k not in {"checked_primary_events", "primary_ok", "checked_source_events", "source_list_ok", "cancelled_source_imports_marked"}
    )
    out = {
        "ok": bool(ok),
        "calendar_id": calendar_id,
        "time_zone": str(tz),
        "target_calendar_ids": sorted(target_calendar_ids),
        "expect_import_mirror": bool(expect_import_mirror),
        "summary": summary,
        "findings": findings,
    }
    _eventlog("osc:gcal_integrity_audit", ok=bool(ok), payload={"summary": summary, "calendar_id": calendar_id})
    return out


def task_gcal_import(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Google Calendar → DB 雙向同步：拉取 Google Calendar 手動建立的事件，
    若 google_calendar_id 不在 case_todos，則建立新 todo 記錄。
    不刪除任何現有資料。
    """
    import re as _re
    try:
        from osc_headless.db import (
            connect_mysql,
            db_config_from_env,
            ensure_osc_min_schema,
        )
    except Exception as e:
        return {"ok": False, "error": f"missing_db_helpers:{type(e).__name__}"}

    p = payload or {}
    calendar_id = p.get("calendar_id") or ""  # 空字串 → 查所有日曆
    tz = p.get("time_zone") or os.environ.get("MAGI_TIME_ZONE") or "Asia/Taipei"
    lookback_days = int(p.get("lookback_days") or 30)
    lookahead_days = int(p.get("lookahead_days") or 180)
    limit = int(p.get("limit") or 250)
    dedup_enabled = _env_bool("MAGI_GCAL_DEDUP_ENABLED", False)
    incremental = bool(p.get("incremental")) or _env_bool("MAGI_GCAL_INCREMENTAL_IMPORT", False)
    history_cutoff = _gcal_history_cutoff_date(p)

    credentials_path = (p.get("credentials_path") or os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH") or "").strip()
    token_path = (p.get("token_path") or os.environ.get("MAGI_GOOGLE_CALENDAR_TOKEN_PATH") or "").strip()
    if not credentials_path:
        credentials_path = _default_gcal_credentials_path()
    if not token_path:
        token_path = _default_gcal_token_path()

    svc = _build_google_calendar_service(credentials_path, token_path, interactive=False)
    if not svc.get("ok"):
        out = {"ok": False, "error": svc.get("error", "gcal_service_failed")}
        if svc.get("need_interactive_oauth"):
            out["need_interactive_oauth"] = True
            out["hint"] = "請先執行 gcal_authorize 完成一次互動授權"
        return out

    service = svc.get("service")

    # Build time window
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    requested_time_min = now - _dt.timedelta(days=lookback_days)
    cutoff_time_min = history_cutoff.replace(tzinfo=_dt.timezone.utc)
    time_min = max(requested_time_min, cutoff_time_min).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + _dt.timedelta(days=lookahead_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 查所有日曆（除非指定了特定 calendar_id）
    if calendar_id:
        cal_ids = [calendar_id]
    else:
        try:
            cal_list = service.calendarList().list().execute().get("items", [])
            cal_ids = [c["id"] for c in cal_list if c.get("id")]
        except Exception:
            cal_ids = ["primary"]
        if not cal_ids:
            cal_ids = ["primary"]

    def _sync_state_path() -> Path:
        try:
            from api.platforms import runtime_dir
            return runtime_dir.root() / "gcal_import_sync_tokens.json"
        except Exception:
            return _MAGI_ROOT / ".runtime" / "gcal_import_sync_tokens.json"

    def _load_sync_state() -> Dict[str, Any]:
        path = _sync_state_path()
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            logging.getLogger(__name__).debug("gcal_import sync state load failed", exc_info=True)
        return {}

    def _save_sync_state(state: Dict[str, Any]) -> None:
        path = _sync_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            logging.getLogger(__name__).debug("gcal_import sync state save failed", exc_info=True)

    def _is_http_410(exc: Exception) -> bool:
        resp = getattr(exc, "resp", None)
        status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
        try:
            if int(status) == 410:
                return True
        except Exception:
            pass
        s = str(exc)
        return "410" in s and ("Gone" in s or "gone" in s or "Sync token" in s or "syncToken" in s)

    sync_state = _load_sync_state() if incremental else {}
    sync_token_resets = 0
    incremental_used = False
    events = []
    for _cid in cal_ids:
        token = str(sync_state.get(_cid) or "") if incremental else ""
        attempt_token = token
        for _attempt in range(2):
            try:
                page_token = None
                fetched_for_calendar = 0
                while True:
                    list_kwargs = {
                        "calendarId": _cid,
                        "maxResults": max(1, min(limit, 2500)),
                        "singleEvents": True,
                    }
                    if attempt_token:
                        list_kwargs["syncToken"] = attempt_token
                        incremental_used = True
                    else:
                        list_kwargs.update({
                            "timeMin": time_min,
                            "timeMax": time_max,
                            "orderBy": "startTime",
                        })
                    if page_token:
                        list_kwargs["pageToken"] = page_token
                    events_result = service.events().list(**list_kwargs).execute()
                    _items = events_result.get("items", [])
                    if _items:
                        for _event in _items:
                            if isinstance(_event, dict):
                                tagged = dict(_event)
                                tagged["_magi_calendar_id"] = _cid
                                events.append(tagged)
                            else:
                                events.append(_event)
                        fetched_for_calendar += len(_items)
                    page_token = events_result.get("nextPageToken")
                    next_sync_token = events_result.get("nextSyncToken")
                    if next_sync_token:
                        sync_state[_cid] = next_sync_token
                    if not page_token or fetched_for_calendar >= limit:
                        break
                if fetched_for_calendar:
                    logger.info("gcal_import: calendar '%s' → %d events", _cid[:40], fetched_for_calendar)
                break
            except Exception as e:
                if attempt_token and _is_http_410(e):
                    sync_token_resets += 1
                    sync_state.pop(_cid, None)
                    attempt_token = ""
                    logging.getLogger(__name__).warning(
                        "gcal_import: calendar '%s' syncToken expired; falling back to full window sync",
                        _cid[:30],
                    )
                    continue
                logging.getLogger(__name__).debug("gcal_import: calendar '%s' failed: %s", _cid[:30], e)
                break

    if incremental:
        _save_sync_state(sync_state)

    if not events:
        try:
            # 向下相容：fallback 到 primary
            events_result = service.events().list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=limit,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            events = events_result.get("items", [])
        except Exception as e:
            return {"ok": False, "error": f"gcal_list_failed:{type(e).__name__}:{e}"}

    if not events:
        return {
            "ok": True,
            "imported": 0,
            "skipped": 0,
            "message": "calendar_empty_or_no_events_in_range",
            "incremental": bool(incremental),
            "incremental_used": bool(incremental_used),
            "sync_token_resets": sync_token_resets,
        }

    # Connect DB
    try:
        db_cfg = db_config_from_env()
        conn = connect_mysql(db_cfg)
        ensure_osc_min_schema(conn)
    except Exception as e:
        return {"ok": False, "error": f"db_connect_failed:{type(e).__name__}"}

    # Fetch existing google_calendar_ids
    try:
        cur = conn.cursor()
        cur.execute("SELECT google_calendar_id FROM case_todos WHERE google_calendar_id IS NOT NULL AND google_calendar_id != ''")
        existing_gcal_ids = {row[0] for row in cur.fetchall()}
        cur.close()
    except Exception as e:
        conn.close()
        return {"ok": False, "error": f"db_read_failed:{e}"}

    # Case number regex patterns (e.g. 114訴123, 114年度訴字第123號)
    _CASE_NUM_RE = _re.compile(
        r"(\d{2,3})[年度]*(?:[度])?([民刑行簡易訴抗更上訴再聲家家事保保全速裁執附附帶非非訟調仲仲裁]+字第?)?(\d+)號?"
    )

    imported = 0
    skipped = 0
    cancelled_marked = 0
    dedup_skipped_in_batch = 0
    db_dedup_skipped = 0
    invalid_case_keys = 0
    historical_skipped = 0
    errors = []
    cur = conn.cursor()
    seen_dedup_keys: set[str] = set()
    try:
        from osc_headless.gcal_dedup import build_dedup_key_from_gcal_event, is_invalid_case_key, normalize_case_key
    except Exception:
        build_dedup_key_from_gcal_event = None  # type: ignore
        is_invalid_case_key = None  # type: ignore
        normalize_case_key = None  # type: ignore

    for event in events:
        event_start_dt = _gcal_event_start_date(event)
        if event_start_dt is not None and event_start_dt < history_cutoff:
            skipped += 1
            historical_skipped += 1
            continue
        gcal_id = event.get("id", "")
        if not gcal_id:
            continue
        if str(event.get("status") or "").lower() == "cancelled":
            try:
                cur.execute(
                    """
                    UPDATE case_todos
                       SET status='deleted'
                     WHERE google_calendar_id=%s
                       AND source_file LIKE 'gcal_import%%'
                    """,
                    (gcal_id,),
                )
                cancelled_marked += int(getattr(cur, "rowcount", 0) or 0)
            except Exception as e:
                errors.append(f"cancelled_mark_failed:{gcal_id[:20]}:{e}")
            skipped += 1
            continue
        if gcal_id in existing_gcal_ids:
            skipped += 1
            continue

        event_dedup_key = ""
        if dedup_enabled and callable(build_dedup_key_from_gcal_event):
            try:
                event_dedup_key = build_dedup_key_from_gcal_event(event, tz=str(tz))
            except Exception:
                event_dedup_key = ""
            if event_dedup_key:
                if event_dedup_key in seen_dedup_keys:
                    skipped += 1
                    dedup_skipped_in_batch += 1
                    continue
                seen_dedup_keys.add(event_dedup_key)

        summary = event.get("summary") or ""
        description = event.get("description") or ""
        start = event.get("start", {})
        start_date = start.get("date") or (start.get("dateTime") or "")[:10]
        start_time = ""
        if "dateTime" in start:
            dt_str = start["dateTime"]
            # Extract HH:MM from ISO datetime
            m = _re.search(r"T(\d{2}:\d{2})", dt_str)
            if m:
                start_time = m.group(1)

        # Try to extract case_number from summary/description
        case_number = ""
        client_name = ""
        if callable(normalize_case_key):
            ck, ck_source = normalize_case_key(
                {
                    "summary": summary,
                    "description": description,
                    "extendedProperties": event.get("extendedProperties") or {},
                }
            )
            if ck and not (callable(is_invalid_case_key) and is_invalid_case_key(ck)):
                case_number = ck
            elif ck and callable(is_invalid_case_key) and is_invalid_case_key(ck):
                invalid_case_keys += 1
                case_number = ""
        if not case_number:
            for text in [summary, description]:
                m = _CASE_NUM_RE.search(text)
                if m:
                    candidate = m.group(0).strip()
                    if callable(is_invalid_case_key) and is_invalid_case_key(candidate):
                        invalid_case_keys += 1
                        case_number = ""
                    else:
                        case_number = candidate
                    break
        if case_number:
            case_number, client_name = _lookup_gcal_case_by_number(conn, case_number)
        else:
            case_number, client_name = _resolve_gcal_event_case_identity(
                conn,
                summary,
                description,
                start_date or "",
            )

        todo_type = _classify_gcal_import_todo_type(summary, description)

        if dedup_enabled:
            try:
                if case_number:
                    cur.execute(
                        """
                        SELECT 1 FROM case_todos
                         WHERE case_number=%s
                           AND todo_type=%s
                           AND todo_date=%s
                           AND COALESCE(todo_time,'')=%s
                         LIMIT 1
                        """,
                        (case_number, todo_type, start_date or None, start_time or ""),
                    )
                else:
                    cur.execute(
                        """
                        SELECT 1 FROM case_todos
                         WHERE source_file='gcal_import'
                           AND todo_type=%s
                           AND todo_date=%s
                           AND COALESCE(todo_time,'')=%s
                           AND COALESCE(description,'')=%s
                         LIMIT 1
                        """,
                        (todo_type, start_date or None, start_time or "", (summary[:500] if summary else "")),
                    )
                if cur.fetchone():
                    skipped += 1
                    db_dedup_skipped += 1
                    continue
            except Exception:
                logging.getLogger(__name__).debug("gcal_import dedup pre-check failed", exc_info=True)

        try:
            source_calendar_id = str(event.get("_magi_calendar_id") or "").strip()
            cur.execute(
                """
                INSERT INTO case_todos
                  (case_number, client_name, todo_type, todo_date, todo_time,
                   description, source_file, status, google_calendar_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                """,
                (
                    case_number or "",
                    client_name or "",
                    todo_type,
                    start_date or None,
                    start_time or None,
                    summary[:500] if summary else "",
                    f"gcal_import:{source_calendar_id}" if source_calendar_id else "gcal_import",
                    gcal_id,
                ),
            )
            existing_gcal_ids.add(gcal_id)
            imported += 1
        except Exception as e:
            errors.append(f"insert_failed:{gcal_id[:20]}:{e}")

    resolved_existing = _backfill_gcal_import_case_identity(
        conn,
        lookback_days=lookback_days,
        lookahead_days=lookahead_days,
        limit=int(p.get("resolve_existing_limit") or 300),
    )
    conn.commit()
    cur.close()
    conn.close()

    _eventlog("osc:gcal_import", ok=True, payload={"imported": imported, "skipped": skipped, "historical_skipped": historical_skipped, "errors": len(errors), "resolved_existing": resolved_existing.get("updated", 0)})
    return {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "historical_skipped": historical_skipped,
        "history_cutoff_date": history_cutoff.strftime("%Y-%m-%d"),
        "cancelled_marked": cancelled_marked,
        "dedup_enabled": bool(dedup_enabled),
        "dedup_skipped_in_batch": dedup_skipped_in_batch,
        "db_dedup_skipped": db_dedup_skipped,
        "invalid_case_keys": invalid_case_keys,
        "incremental": bool(incremental),
        "incremental_used": bool(incremental_used),
        "sync_token_resets": sync_token_resets,
        "resolved_existing": resolved_existing,
        "errors": errors[:10],
    }


def task_laf_pending_scan(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Nightly scan for LAF cases that need action (開辦 or 報結).

    Queries the `cases` table for:
      - 待開辦: laf_case_no set AND status IN ('未開辦','待開辦','pending_open')
        AND created more than `open_grace_days` days ago (default 3)
      - 待報結: laf_case_no set AND status IN ('待報結','pending_report')
        AND created more than `report_grace_days` days ago (default 1)

    Sends a LINE/Discord notification listing actionable cases.
    """
    try:
        from osc_headless.db import connect_mysql, db_config_from_env, ensure_osc_min_schema  # type: ignore
    except Exception as e:
        return {"ok": False, "error": f"missing_db_helpers:{type(e).__name__}"}

    notify = bool((payload or {}).get("notify", True))
    limit = int((payload or {}).get("limit") or 100)

    try:
        cfg = db_config_from_env()
        conn = connect_mysql(cfg)
        ensure_osc_min_schema(conn)
        cur = conn.cursor()

        # --- 待開辦 (使用 code/osc.py 的正確邏輯) ---
        # 查 case_category='法律扶助案件' + legal_aid_status IS NULL 或 '未開辦'
        cur.execute(
            """
            SELECT `case_number`, `client_name`, `legal_aid_startup_deadline`,
                   `legal_aid_status`
            FROM `cases`
            WHERE `case_category` = '法律扶助案件'
              AND (`legal_aid_status` IS NULL OR `legal_aid_status` = '未開辦')
              AND `legal_aid_startup_deadline` IS NOT NULL
            ORDER BY `legal_aid_startup_deadline` ASC
            LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        open_cases = [dict(zip(cols, row)) for row in (cur.fetchall() or [])]

        # --- 待報結 ---
        cur.execute(
            """
            SELECT `case_number`, `client_name`, `legal_aid_startup_deadline`,
                   `legal_aid_status`
            FROM `cases`
            WHERE `case_category` = '法律扶助案件'
              AND `legal_aid_status` = '待報結'
            ORDER BY `legal_aid_startup_deadline` ASC
            LIMIT %s
            """,
            (limit,),
        )
        report_cases = [dict(zip(cols, row)) for row in (cur.fetchall() or [])]

        cur.close()
        conn.close()
    except Exception as e:
        return {"ok": False, "error": f"db_query_failed: {e}"}

    today = datetime.now().date()
    total = len(open_cases) + len(report_cases)

    def _deadline_info(c):
        dl = c.get("legal_aid_startup_deadline")
        if not dl:
            return "無期限"
        if hasattr(dl, "date"):
            dl = dl.date() if callable(dl.date) else dl
        days_left = (dl - today).days
        if days_left < 0:
            return f"已逾期 {abs(days_left)} 天"
        elif days_left == 0:
            return "今天到期"
        else:
            return f"剩 {days_left} 天"

    result: Dict[str, Any] = {
        "ok": True,
        "pending_open": len(open_cases),
        "pending_report": len(report_cases),
        "total": total,
        "open_cases": [
            {"case_number": c.get("case_number"), "client_name": c.get("client_name"),
             "deadline": str(c.get("legal_aid_startup_deadline") or ""),
             "deadline_info": _deadline_info(c)}
            for c in open_cases
        ],
        "report_cases": [
            {"case_number": c.get("case_number"), "client_name": c.get("client_name"),
             "deadline": str(c.get("legal_aid_startup_deadline") or ""),
             "deadline_info": _deadline_info(c)}
            for c in report_cases
        ],
    }

    _eventlog("osc:laf_pending_scan", ok=True, payload={
        "pending_open": len(open_cases), "pending_report": len(report_cases)
    })

    if not notify or total == 0:
        return result

    # --- Build notification message ---
    lines = [f"⚖️ 法扶待辦提醒（共 {total} 件）"]
    if open_cases:
        lines.append(f"\n📂 待開辦（{len(open_cases)} 件）：")
        for c in open_cases[:20]:
            dl = c.get("legal_aid_startup_deadline")
            info = _deadline_info(c)
            lines.append(
                f"  • {c.get('client_name','?')} [{c.get('case_number','?')}]"
                f" — 開辦末日 {dl or '?'}（{info}）"
            )
        if len(open_cases) > 20:
            lines.append(f"  ...還有 {len(open_cases) - 20} 件")
    if report_cases:
        lines.append(f"\n📋 待報結（{len(report_cases)} 件）：")
        for c in report_cases[:20]:
            lines.append(
                f"  • {c.get('client_name','?')} [{c.get('case_number','?')}]"
            )
        if len(report_cases) > 20:
            lines.append(f"  ...還有 {len(report_cases) - 20} 件")

    msg = "\n".join(lines)
    try:
        sys.path.insert(0, os.path.join(SKILL_DIR, "..", ".."))
        from skills.ops.red_phone import alert_admin
        alert_admin(msg, severity="info", topic_key="laf_general")
        result["notified"] = True
    except Exception as e:
        result["notified"] = False
        result["notify_error"] = str(e)

    return result


def task_gcal_authorize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Interactive OAuth helper to create/refresh Google Calendar token file.
    Does NOT create any events.
    """
    credentials_path = ((payload or {}).get("credentials_path") or os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH") or "").strip()
    token_path = ((payload or {}).get("token_path") or os.environ.get("MAGI_GOOGLE_CALENDAR_TOKEN_PATH") or "").strip()
    if not credentials_path:
        credentials_path = _default_gcal_credentials_path()
    if not token_path:
        token_path = _default_gcal_token_path()
    svc = _build_google_calendar_service(credentials_path, token_path, interactive=True)
    if svc.get("ok"):
        _eventlog("osc:gcal_authorize", ok=True, payload={"token_path": token_path})
        return {"ok": True, "authorized": True, "token_path": token_path}
    _eventlog("osc:gcal_authorize", ok=False, payload={"error": svc.get("error", "")[:240]})
    return {"ok": False, "authorized": False, "error": svc.get("error", "authorize_failed"), "token_path": token_path}


def main() -> None:
    _maybe_reexec_venv()
    ensure_orch_on_sys_path()

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    args = parser.parse_args()

    task = (args.task or "").strip()
    if task in ("help", "--help", "-h"):
        print(json.dumps({"ok": True, "tasks": ["help", "self_test", "db_smoke", "index_cases", "todo_preview", "todo_sync", "todo_list", "scan_folder", "scan_cases", "keyword_sanity", "keyword_fix", "queue_status", "queue_flush", "gcal_authorize", "gcal_sync", "gcal_import", "gcal_integrity_audit"]}, ensure_ascii=False))
        return

    # Support Taiwanese natural phrases
    if task.startswith("待辦預覽"):
        payload = _json_load_maybe(task[len("待辦預覽"):].strip())
        out = task_todo_preview(payload)
    elif task.startswith("待辦入庫"):
        payload = _json_load_maybe(task[len("待辦入庫"):].strip())
        out = task_todo_sync(payload)
    elif task.startswith("佇列補寫"):
        payload = _json_load_maybe(task[len("佇列補寫"):].strip())
        out = task_queue_flush(payload)
    elif task.startswith("佇列狀態"):
        payload = _json_load_maybe(task[len("佇列狀態"):].strip())
        out = task_queue_status(payload if isinstance(payload, dict) else {})
    elif task.startswith("待辦清單"):
        payload = _json_load_maybe(task[len("待辦清單"):].strip())
        out = task_todo_list(payload)
    elif task.startswith("關鍵詞健檢"):
        payload = _json_load_maybe(task[len("關鍵詞健檢"):].strip())
        out = task_keyword_sanity(payload if isinstance(payload, dict) else {})
    elif task.startswith("關鍵詞修補"):
        payload = _json_load_maybe(task[len("關鍵詞修補"):].strip())
        out = task_keyword_fix_escapes(payload if isinstance(payload, dict) else {})
    elif task.startswith("掃描資料夾待辦"):
        payload = _json_load_maybe(task[len("掃描資料夾待辦"):].strip())
        out = task_scan_folder(payload)
    elif task.startswith("掃描案件待辦"):
        payload = _json_load_maybe(task[len("掃描案件待辦"):].strip())
        out = task_scan_cases(payload)
    elif task.startswith("更新案件索引"):
        payload = _json_load_maybe(task[len("更新案件索引"):].strip())
        out = task_index_cases(payload if isinstance(payload, dict) else {})
    elif task.startswith("日曆同步"):
        payload = _json_load_maybe(task[len("日曆同步"):].strip())
        out = task_gcal_sync(payload if isinstance(payload, dict) else {})
    else:
        parts = task.split(" ", 1)
        cmd = parts[0]
        payload = _json_load_maybe(parts[1] if len(parts) > 1 else "")

        if cmd == "self_test":
            out = task_self_test(payload)
        elif cmd == "db_smoke":
            out = task_db_smoke(payload)
        elif cmd == "index_cases":
            out = task_index_cases(payload if isinstance(payload, dict) else {})
        elif cmd == "todo_preview":
            out = task_todo_preview(payload)
        elif cmd == "todo_sync":
            out = task_todo_sync(payload)
        elif cmd == "todo_list":
            out = task_todo_list(payload)
        elif cmd == "keyword_sanity":
            out = task_keyword_sanity(payload if isinstance(payload, dict) else {})
        elif cmd == "keyword_fix":
            out = task_keyword_fix_escapes(payload if isinstance(payload, dict) else {})
        elif cmd == "scan_folder":
            out = task_scan_folder(payload)
        elif cmd == "scan_cases":
            out = task_scan_cases(payload if isinstance(payload, dict) else {})
        elif cmd == "queue_status":
            out = task_queue_status(payload if isinstance(payload, dict) else {})
        elif cmd == "queue_flush":
            out = task_queue_flush(payload)
        elif cmd == "gcal_authorize":
            out = task_gcal_authorize(payload if isinstance(payload, dict) else {})
        elif cmd == "gcal_sync":
            out = task_gcal_sync(payload if isinstance(payload, dict) else {})
        elif cmd == "gcal_import":
            out = task_gcal_import(payload if isinstance(payload, dict) else {})
        elif cmd == "gcal_integrity_audit":
            out = task_gcal_integrity_audit(payload if isinstance(payload, dict) else {})
        elif cmd == "laf_pending_scan":
            out = task_laf_pending_scan(payload if isinstance(payload, dict) else {})
        else:
            out = {"ok": False, "error": f"未知 task: {task}"}

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
