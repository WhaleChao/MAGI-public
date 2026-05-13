#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trial-prep/action.py

開庭準備自動化技能
- 查詢未來開庭排程（Apple Calendar）
- 針對案號產生開庭準備備忘
- 產生開庭前確認清單
- 產生案件時間軸摘要
"""

from __future__ import annotations
import logging

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", str(Path(__file__).resolve().parents[2])))
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

def _resolve_case_base() -> str:
    explicit = os.environ.get("MAGI_CASE_BASE", "").strip()
    if explicit:
        return explicit
    try:
        from api.case_path_mapper import preferred_case_roots
        roots = preferred_case_roots(include_closed=False)
        if roots:
            return roots[0]
    except Exception:
        pass
    return "/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件"

CASE_BASE = Path(_resolve_case_base())

# ── Apple Calendar 查詢 ──────────────────────────────────────────


def _query_calendar_events(days: int = 7) -> List[Dict[str, str]]:
    """Query Apple Calendar for court hearing events in the next N days."""
    try:
        apple_skill = str(_MAGI_ROOT / "skills" / "apple" / "action.py")
        py = os.environ.get("MAGI_SKILL_PYTHON", "") or "python3"
        if not os.path.exists(apple_skill):
            return _query_calendar_osascript(days)
        result = subprocess.run(
            [py, apple_skill, "--task", "calendar_upcoming", "--text", str(days)],
            capture_output=True, timeout=30, text=True,
            cwd=str(_MAGI_ROOT),
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            if isinstance(data, list):
                return data
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 53, exc_info=True)
    return _query_calendar_osascript(days)


def _query_calendar_osascript(days: int = 7) -> List[Dict[str, str]]:
    """Fallback: use osascript to query Calendar.app directly."""
    now = datetime.now()
    end = now + timedelta(days=days)
    script = f'''
    tell application "Calendar"
        set output to ""
        repeat with cal in calendars
            set evts to (every event of cal whose start date >= (current date) and start date <= ((current date) + {days} * days))
            repeat with e in evts
                set t to summary of e
                if t contains "開庭" or t contains "庭期" or t contains "調解" or t contains "言詞辯論" then
                    set d to start date of e
                    set output to output & t & "|" & (year of d) & "-" & (month of d as integer) & "-" & (day of d) & linefeed
                end if
            end repeat
        end repeat
        return output
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=15, text=True,
        )
        events = []
        for line in (result.stdout or "").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 1)
            title = parts[0].strip()
            date_str = parts[1].strip() if len(parts) > 1 else ""
            case_no = _extract_case_number(title)
            events.append({
                "title": title,
                "date": date_str,
                "case_no": case_no,
            })
        return events
    except Exception:
        return []


# ── 案號解析 ──────────────────────────────────────────────────────


def _extract_case_number(text: str) -> str:
    """Extract Taiwan court case number from text."""
    # Pattern: 112年度勞訴字第XXX號, 113年度訴字第123號, etc.
    m = re.search(r"(\d{2,3})\s*年度?\s*[\u4e00-\u9fff]{1,6}\s*字?\s*第?\s*(\d+)\s*號?", text)
    if m:
        return m.group(0)
    # Simpler pattern: just digits+年+something+號
    m = re.search(r"\d{2,3}年[\u4e00-\u9fff\d]+號", text)
    if m:
        return m.group(0)
    return ""


def _find_case_folder(case_no: str) -> Optional[Path]:
    """Find case folder on NAS by case number."""
    if not case_no or not CASE_BASE.exists():
        return None
    # Walk first level of case base
    try:
        for entry in CASE_BASE.iterdir():
            if entry.is_dir() and case_no in entry.name:
                return entry
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 127, exc_info=True)
    # Fuzzy: extract year + number
    nums = re.findall(r"\d+", case_no)
    if len(nums) >= 2:
        try:
            for entry in CASE_BASE.iterdir():
                if entry.is_dir() and nums[0] in entry.name and nums[-1] in entry.name:
                    return entry
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 136, exc_info=True)
    return None


def _scan_case_folder(folder: Path) -> Dict[str, List[str]]:
    """Scan case folder and categorize files."""
    categories: Dict[str, List[str]] = {
        "briefs": [],      # 書狀
        "transcripts": [],  # 筆錄
        "evidence": [],     # 證據
        "rulings": [],      # 裁定/判決
        "other": [],
    }
    if not folder or not folder.exists():
        return categories
    try:
        for f in sorted(folder.rglob("*")):
            if not f.is_file():
                continue
            name = f.name.lower()
            rel = str(f.relative_to(folder))
            if any(kw in name for kw in ["書狀", "起訴", "答辯", "聲請", "準備", "辯論", "陳報", "上訴"]):
                categories["briefs"].append(rel)
            elif any(kw in name for kw in ["筆錄", "言詞", "準備程序", "調解"]):
                categories["transcripts"].append(rel)
            elif any(kw in name for kw in ["證據", "附件", "證物", "附證"]):
                categories["evidence"].append(rel)
            elif any(kw in name for kw in ["裁定", "判決"]):
                categories["rulings"].append(rel)
            elif f.suffix.lower() in {".pdf", ".docx", ".doc", ".jpg", ".png"}:
                categories["other"].append(rel)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 168, exc_info=True)
    return categories


# ── 跨 Skill 查詢 ────────────────────────────────────────────────


def _query_statutes(keywords: str) -> str:
    """Query statutes-vdb skill for relevant law articles."""
    try:
        skill_path = str(_MAGI_ROOT / "skills" / "statutes-vdb" / "action.py")
        py = os.environ.get("MAGI_SKILL_PYTHON", "") or "python3"
        if not os.path.exists(skill_path):
            return ""
        result = subprocess.run(
            [py, skill_path, "--task", "search", "--text", keywords],
            capture_output=True, timeout=30, text=True,
            cwd=str(_MAGI_ROOT),
        )
        return result.stdout.strip()[:2000] if result.returncode == 0 else ""
    except Exception:
        return ""


def _query_judgments(keywords: str) -> str:
    """Query judgment-collector skill for relevant rulings."""
    try:
        skill_path = str(_MAGI_ROOT / "skills" / "judgment-collector" / "action.py")
        py = os.environ.get("MAGI_SKILL_PYTHON", "") or "python3"
        if not os.path.exists(skill_path):
            return ""
        result = subprocess.run(
            [py, skill_path, "--task", "search", "--text", keywords],
            capture_output=True, timeout=30, text=True,
            cwd=str(_MAGI_ROOT),
        )
        return result.stdout.strip()[:2000] if result.returncode == 0 else ""
    except Exception:
        return ""


# ── 指令實作 ──────────────────────────────────────────────────────


def _cmd_upcoming(days: int = 7) -> str:
    """列出未來 N 天的開庭排程。"""
    events = _query_calendar_events(days)
    if not events:
        return f"📅 未來 {days} 天沒有找到開庭相關行程。\n（提示：請確認 Apple Calendar 中有包含「開庭」「庭期」「調解」「言詞辯論」等關鍵字的事件）"

    lines = [f"📅 未來 {days} 天開庭排程（共 {len(events)} 場）", ""]
    for i, ev in enumerate(events, 1):
        title = ev.get("title", "")
        date_str = ev.get("date", "")
        case_no = ev.get("case_no", "")
        line = f"{i}. [{date_str}] {title}"
        if case_no:
            folder = _find_case_folder(case_no)
            line += f"\n   案號：{case_no}"
            if folder:
                line += f"\n   資料夾：{folder.name}"
        lines.append(line)

    lines.append("")
    lines.append("使用 --task prepare --text \"案號\" 可產生詳細開庭備忘。")
    return "\n".join(lines)


def _cmd_prepare(text: str) -> str:
    """針對指定案號產生開庭準備備忘。"""
    if not text:
        return "⚠️ 請指定案號，例如：--task prepare --text \"113年度勞訴字第100號\""

    case_no = _extract_case_number(text) or text.strip()
    folder = _find_case_folder(case_no)

    lines = [
        f"📋 開庭準備備忘 — {case_no}",
        f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    # 1. 案件資料夾掃描
    if folder:
        lines.append(f"【案件資料夾】{folder}")
        cats = _scan_case_folder(folder)
        lines.append(f"  書狀：{len(cats['briefs'])} 份")
        if cats["briefs"]:
            for b in cats["briefs"][-5:]:
                lines.append(f"    - {b}")
        lines.append(f"  筆錄：{len(cats['transcripts'])} 份")
        if cats["transcripts"]:
            for t in cats["transcripts"][-5:]:
                lines.append(f"    - {t}")
        lines.append(f"  證據：{len(cats['evidence'])} 份")
        lines.append(f"  裁定/判決：{len(cats['rulings'])} 份")
        lines.append(f"  其他文件：{len(cats['other'])} 份")
    else:
        lines.append(f"【案件資料夾】未找到（搜尋路徑：{CASE_BASE}）")

    lines.append("")

    # 2. 相關法條查詢
    # Extract case type keywords for statute search
    case_keywords = _extract_case_type_keywords(case_no, text)
    if case_keywords:
        lines.append("【相關法條提示】")
        statutes = _query_statutes(case_keywords)
        if statutes:
            for sl in statutes.splitlines()[:10]:
                lines.append(f"  {sl}")
        else:
            lines.append("  （查詢中或無結果）")
        lines.append("")

    # 3. 相關判決見解
    if case_keywords:
        lines.append("【相關判決見解】")
        judgments = _query_judgments(case_keywords)
        if judgments:
            for jl in judgments.splitlines()[:10]:
                lines.append(f"  {jl}")
        else:
            lines.append("  （查詢中或無結果）")
        lines.append("")

    # 4. 開庭前確認事項
    lines.append("【開庭前確認事項】")
    lines.append("  □ 確認開庭時間、地點、法庭")
    lines.append("  □ 攜帶身分證件及律師證")
    lines.append("  □ 確認委任狀是否已提出")
    lines.append("  □ 確認本次庭期應提出之書狀")
    lines.append("  □ 確認證據原本是否備妥")
    lines.append("  □ 確認證人是否已通知出庭")
    lines.append("  □ 確認對造書狀是否已收到並閱畢")
    lines.append("  □ 複習上次筆錄重點")

    return "\n".join(lines)


def _cmd_checklist(text: str) -> str:
    """產生開庭前確認清單。"""
    if not text:
        return "⚠️ 請指定案號。"

    case_no = _extract_case_number(text) or text.strip()
    folder = _find_case_folder(case_no)
    cats = _scan_case_folder(folder) if folder else {}

    lines = [
        f"✅ 開庭確認清單 — {case_no}",
        "",
        "【文件狀態】",
    ]

    briefs = cats.get("briefs", [])
    transcripts = cats.get("transcripts", [])
    evidence = cats.get("evidence", [])

    if briefs:
        lines.append(f"  ✓ 書狀 {len(briefs)} 份（最新：{briefs[-1]}）")
    else:
        lines.append("  ✗ 尚未找到書狀檔案")

    if transcripts:
        lines.append(f"  ✓ 筆錄 {len(transcripts)} 份（最新：{transcripts[-1]}）")
    else:
        lines.append("  ○ 無筆錄（可能為首次開庭）")

    if evidence:
        lines.append(f"  ✓ 證據資料 {len(evidence)} 份")
    else:
        lines.append("  ○ 無獨立證據檔案")

    lines.append("")
    lines.append("【準備事項】")
    lines.append("  □ 閱讀對造最新書狀")
    lines.append("  □ 準備本次庭期之陳述要點")
    lines.append("  □ 確認爭點整理結果")
    lines.append("  □ 準備可能需要的法條條文")
    lines.append("  □ 通知當事人出庭時間地點")

    if not folder:
        lines.append("")
        lines.append(f"⚠️ 未找到案件資料夾，請確認 NAS 路徑：{CASE_BASE}")

    return "\n".join(lines)


def _cmd_timeline(text: str) -> str:
    """產生案件時間軸。"""
    if not text:
        return "⚠️ 請指定案號。"

    case_no = _extract_case_number(text) or text.strip()
    folder = _find_case_folder(case_no)

    if not folder:
        return f"⚠️ 找不到案號「{case_no}」的資料夾。"

    cats = _scan_case_folder(folder)
    all_files = []
    for cat, files in cats.items():
        for f in files:
            fp = folder / f
            try:
                mtime = fp.stat().st_mtime
                dt = datetime.fromtimestamp(mtime)
                all_files.append((dt, cat, f))
            except Exception:
                all_files.append((datetime.min, cat, f))

    all_files.sort(key=lambda x: x[0])

    cat_labels = {
        "briefs": "書狀",
        "transcripts": "筆錄",
        "evidence": "證據",
        "rulings": "裁判",
        "other": "其他",
    }

    lines = [
        f"📅 案件時間軸 — {case_no}",
        f"資料夾：{folder.name}",
        f"文件總數：{len(all_files)}",
        "",
    ]

    for dt, cat, f in all_files:
        date_str = dt.strftime("%Y-%m-%d") if dt != datetime.min else "日期不明"
        label = cat_labels.get(cat, cat)
        lines.append(f"  {date_str} [{label}] {f}")

    return "\n".join(lines)


# ── 輔助函式 ──────────────────────────────────────────────────────


def _extract_case_type_keywords(case_no: str, text: str) -> str:
    """Extract case type keywords for statute/judgment search."""
    combined = f"{case_no} {text}"
    keywords = []
    type_map = {
        "勞訴": "勞動基準法 勞動事件法",
        "勞簡": "勞動基準法 勞動事件法",
        "勞調": "勞動基準法 勞資爭議處理法",
        "訴": "民事訴訟法",
        "簡": "民事訴訟法 簡易訴訟",
        "家訴": "家事事件法",
        "家調": "家事事件法",
        "刑": "刑法 刑事訴訟法",
        "易": "刑法 刑事訴訟法",
        "聲": "民事訴訟法 強制執行法",
        "執": "強制執行法",
        "破": "破產法",
        "更": "消費者債務清理條例",
        "消債": "消費者債務清理條例",
    }
    for key, laws in type_map.items():
        if key in combined:
            keywords.append(laws)
            break
    # Add any specific keywords from text
    for kw in ["資遣", "加班", "退休", "職災", "離婚", "扶養", "繼承", "侵權", "契約", "借貸", "租賃"]:
        if kw in combined:
            keywords.append(kw)
    return " ".join(keywords) if keywords else ""


# ── 主程式 ────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="MAGI 開庭準備自動化")
    ap.add_argument("--task", default="upcoming", help="upcoming|prepare|checklist|timeline")
    ap.add_argument("--text", default="", help="案號或關鍵字")
    ap.add_argument("--days", default="7", help="upcoming 天數（預設 7）")
    args = ap.parse_args()

    task = str(args.task or "upcoming").strip().lower()
    text = str(args.text or "").strip()

    if task in {"upcoming", "schedule", "排程"}:
        days = 7
        try:
            days = int(args.days)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 457, exc_info=True)
        print(_cmd_upcoming(days))
        return 0
    if task in {"prepare", "prep", "準備", "備忘"}:
        print(_cmd_prepare(text))
        return 0
    if task in {"checklist", "check", "清單", "確認"}:
        print(_cmd_checklist(text))
        return 0
    if task in {"timeline", "時間軸"}:
        print(_cmd_timeline(text))
        return 0

    print("⚠️ 不支援的 task，請使用：upcoming|prepare|checklist|timeline")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
