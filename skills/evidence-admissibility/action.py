#!/usr/bin/env python3
"""
evidence-admissibility/action.py

卷證索引證據能力分類工具 — 將法院卷證索引文字檔解析後，
依刑事訴訟法傳聞法則自動分類每筆證據的證據能力意見，產出格式化 Excel 表格。

支援兩種模式：
  A) 案號/當事人查詢：自動從 OSC DB 查案件 → 定位 NAS 案件資料夾 → 掃描卷證索引 .txt
  B) 互動引導：使用者直接貼文字或給檔案路徑

Usage (CLI):
    python action.py --task 'help'
    python action.py --task 'lookup {"case_number":"2025-0088"}'
    python action.py --task 'lookup {"client_name":"王大明"}'
    python action.py --task 'classify {"index_file":"/path/to/index.txt","defendant":"王大明"}'

Usage (Skill API via MAGI):
    POST /skills/run  { "skill": "evidence-admissibility", "task": "lookup {...}" }
"""
from __future__ import annotations

import argparse
import glob as _glob
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

_SKILL_DIR = Path(__file__).resolve().parent

logger = logging.getLogger("evidence-admissibility")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


# ── DB / Case Path helpers ──────────────────────────────────────────

def _get_db_conn():
    """取得 OSC (law_firm_data) 資料庫連線。"""
    _osc_dir = str(_MAGI_ROOT / "skills" / "osc-orchestrator")
    if _osc_dir not in sys.path:
        sys.path.insert(0, _osc_dir)
    from osc_headless.db import DBConfig, connect_mysql
    return connect_mysql(DBConfig())


def _query_cases(case_number: str = "", client_name: str = "") -> List[Dict[str, Any]]:
    """從 OSC DB 查詢案件，支援案號或當事人名（模糊搜尋）。"""
    try:
        conn = _get_db_conn()
    except Exception as e:
        logger.warning("DB connection failed: %s", e)
        return []

    results: List[Dict[str, Any]] = []
    try:
        cur = conn.cursor(dictionary=True)

        if case_number:
            # 精確 → LIKE
            cur.execute(
                "SELECT * FROM cases WHERE case_number = %s LIMIT 10",
                (case_number,),
            )
            results = cur.fetchall() or []
            if not results:
                cur.execute(
                    "SELECT * FROM cases WHERE case_number LIKE %s LIMIT 10",
                    (f"%{case_number}%",),
                )
                results = cur.fetchall() or []

        if not results and client_name:
            cur.execute(
                "SELECT * FROM cases WHERE client_name LIKE %s "
                "ORDER BY created_date DESC LIMIT 10",
                (f"%{client_name}%",),
            )
            results = cur.fetchall() or []

        cur.close()
    except Exception as e:
        logger.warning("DB query failed: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 94, exc_info=True)
    return results


def _resolve_case_folder(case: Dict[str, Any]) -> Optional[Path]:
    """將 DB 中的 folder_path 轉為本機路徑。"""
    from api.case_path_mapper import translate_case_path_to_local

    fp = case.get("folder_path", "") or ""
    if fp:
        local = translate_case_path_to_local(fp)
        if local:
            p = Path(local)
            if p.exists():
                return p

    # fallback: 用案號在 NAS 搜尋
    from api.case_path_mapper import preferred_case_roots
    case_no = case.get("case_number", "")
    if not case_no:
        return None

    for root in preferred_case_roots(include_closed=True):
        rp = Path(root)
        if not rp.exists():
            continue
        # 只搜尋兩層（案件類型 → 案件資料夾）
        for cat_dir in rp.iterdir():
            if not cat_dir.is_dir():
                continue
            for sub in cat_dir.iterdir():
                if not sub.is_dir():
                    continue
                for entry in sub.iterdir():
                    if entry.is_dir() and case_no in entry.name:
                        return entry
    return None


def _scan_index_files(folder: Path) -> List[Path]:
    """在案件資料夾中掃描可能的卷證索引 .txt 檔。"""
    candidates: List[Path] = []
    keywords = ["卷證索引", "證據索引", "證據清單", "卷證", "索引"]

    for txt in folder.rglob("*.txt"):
        name = txt.name
        if any(kw in name for kw in keywords):
            candidates.append(txt)

    # 如果沒有名稱符合的，列出所有 .txt 供使用者選擇
    if not candidates:
        all_txt = sorted(folder.rglob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        candidates = all_txt[:10]

    return candidates


# ── Help ────────────────────────────────────────────────────────────

HELP_TEXT = """\
【卷證索引證據能力分類工具】

可用指令：
  help       顯示本說明
  lookup     依案號或當事人名查詢案件，定位卷證索引檔案
  classify   分類證據能力（需提供卷證索引文字檔、被告姓名等資訊）
  rules      顯示傳聞法則分類規則摘要

使用方式 A（自動查詢）：
  → 「幫我做王大明案的證據能力」
  → lookup {"case_number":"2025-0088"} 或 {"client_name":"王大明"}
  → 系統自動查 DB、定位案件資料夾、掃描卷證索引 .txt

使用方式 B（手動提供）：
  → 直接貼卷證索引文字內容
  → 或提供 .txt 檔案路徑
  → 系統依 SKILL.md 流程引導

詳細規則請參閱：
  - SKILL.md（工作流程說明）
  - references/admissibility_rules.md（完整法條及分類規則）
  - scripts/build_evidence_xlsx.py（解析腳本模板）
"""


def cmd_help(**_kwargs: Any) -> str:
    return HELP_TEXT


# ── Rules ───────────────────────────────────────────────────────────

def cmd_rules(**_kwargs: Any) -> str:
    rules_path = _SKILL_DIR / "references" / "admissibility_rules.md"
    if rules_path.exists():
        return rules_path.read_text(encoding="utf-8")
    return "❌ 找不到 admissibility_rules.md"


# ── Lookup ──────────────────────────────────────────────────────────

def cmd_lookup(payload: str = "", **_kwargs: Any) -> str:
    """依案號或當事人名查詢案件，自動定位卷證索引檔案。"""
    params: Dict[str, Any] = {}
    if payload:
        try:
            params = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            # 非 JSON，當作當事人名或案號
            clean = payload.strip()
            if re.match(r"^\d{4}-\d+", clean):
                params = {"case_number": clean}
            else:
                params = {"client_name": clean}

    case_number = params.get("case_number", "")
    client_name = params.get("client_name", "")

    if not case_number and not client_name:
        return "⚠️ 請提供案號（case_number）或當事人名（client_name）"

    # 查 DB
    cases = _query_cases(case_number=case_number, client_name=client_name)
    if not cases:
        search_term = case_number or client_name
        return (
            f"❌ 在 OSC 資料庫中找不到「{search_term}」的案件。\n\n"
            f"你可以：\n"
            f"  1. 確認案號或當事人名是否正確\n"
            f"  2. 直接提供卷證索引 .txt 檔案路徑\n"
            f"  3. 直接貼上卷證索引文字內容"
        )

    lines: List[str] = []

    # 列出找到的案件
    if len(cases) == 1:
        c = cases[0]
        lines.append("✅ 找到案件：")
    else:
        lines.append(f"✅ 找到 {len(cases)} 筆案件：")

    for i, c in enumerate(cases, 1):
        cn = c.get("case_number", "?")
        cl = c.get("client_name", "?")
        ct = c.get("case_type", "")
        cr = c.get("case_reason", "")
        court = c.get("court_name", "")
        court_no = c.get("court_case_no") or c.get("court_case_number", "")
        status = c.get("status", "")

        prefix = f"  [{i}]" if len(cases) > 1 else "  "
        lines.append(f"{prefix} {cn} {cl}（{ct}{cr}）")
        if court:
            lines.append(f"      法院：{court}")
        if court_no:
            lines.append(f"      案號：{court_no}")
        if status:
            lines.append(f"      狀態：{status}")

        # 定位案件資料夾
        folder = _resolve_case_folder(c)
        if folder:
            lines.append(f"      📂 資料夾：{folder}")

            # 掃描卷證索引
            index_files = _scan_index_files(folder)
            if index_files:
                lines.append(f"      📄 找到可能的卷證索引檔案：")
                for f in index_files:
                    size_kb = f.stat().st_size / 1024
                    lines.append(f"         → {f.name} ({size_kb:.0f} KB)")
                    lines.append(f"           路徑：{f}")
            else:
                lines.append(f"      ⚠️ 資料夾中未找到卷證索引 .txt 檔案")
        else:
            lines.append(f"      ⚠️ 無法定位案件資料夾")

        lines.append("")

    lines.append("─" * 40)
    lines.append("接下來請提供：")
    lines.append("  1. 確認要使用哪個卷證索引檔案")
    lines.append("  2. 被告姓名（本案代表哪位被告）")
    lines.append("  3. 檢察官補充理由書（PDF）或檢察官援引的證人名單")
    lines.append("  4. 已於審判中作證的證人清單")
    lines.append("  5. 秘密證人名單（如有）")

    return "\n".join(lines)


# ── Classify ────────────────────────────────────────────────────────

def cmd_classify(payload: str = "", **_kwargs: Any) -> str:
    """
    解析卷證索引並分類證據能力。

    此指令為互動式引導流程：LLM 會依 SKILL.md 的工作流程，
    逐步向使用者收集必要資料，客製化 section_rules 等變數，
    然後呼叫 scripts/build_evidence_xlsx.py 產出 Excel。
    """
    params: Dict[str, Any] = {}
    if payload:
        try:
            params = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            pass

    index_file = params.get("index_file", "")
    defendant = params.get("defendant", "")

    if not index_file or not defendant:
        return (
            "⚠️ 需要以下資料才能開始分類：\n\n"
            "1. **卷證索引文字檔路徑**（.txt）\n"
            "2. **被告姓名**\n"
            "3. **檢察官補充理由書**（PDF）或手動提供檢察官援引的證人名單\n"
            "4. **已於審判中作證的證人清單**\n"
            "5. **秘密證人名單**（如有）\n\n"
            "💡 提示：可用 lookup 指令自動查詢案件並定位卷證索引檔案。\n"
            "   例如：lookup {\"case_number\":\"2025-0088\"}\n"
            "   或者：lookup {\"client_name\":\"王大明\"}"
        )

    # 檢查檔案是否存在
    if not Path(index_file).exists():
        return f"❌ 找不到卷證索引文字檔：{index_file}"

    # 回傳引導訊息（實際分類由 LLM 依 SKILL.md 流程執行）
    file_size = Path(index_file).stat().st_size / 1024
    return (
        f"✅ 收到資料：\n"
        f"  - 卷證索引：{index_file}（{file_size:.0f} KB）\n"
        f"  - 被告：{defendant}\n\n"
        f"請依 SKILL.md 的流程：\n"
        f"  1. 讀取卷證索引內容\n"
        f"  2. 辨識段落（section_rules）\n"
        f"  3. 客製化 scripts/build_evidence_xlsx.py\n"
        f"  4. 執行腳本產出 Excel\n\n"
        f"腳本模板位於：{_SKILL_DIR / 'scripts' / 'build_evidence_xlsx.py'}"
    )


# ── Dispatch ────────────────────────────────────────────────────────

_COMMANDS = {
    "help": cmd_help,
    "rules": cmd_rules,
    "lookup": cmd_lookup,
    "classify": cmd_classify,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="卷證索引證據能力分類工具")
    parser.add_argument("--task", type=str, default="help",
                        help="指令：help / rules / lookup / classify {JSON}")
    parser.add_argument("--text", type=str, default="",
                        help="附加文字（由 orchestrator 傳入）")
    args = parser.parse_args()

    # 分離指令與 payload
    task_str = args.task.strip()
    parts = task_str.split(None, 1)
    cmd_name = parts[0] if parts else "help"
    payload = parts[1] if len(parts) > 1 else ""

    handler = _COMMANDS.get(cmd_name, cmd_help)
    result = handler(payload=payload, text=args.text)
    print(result)


if __name__ == "__main__":
    main()
