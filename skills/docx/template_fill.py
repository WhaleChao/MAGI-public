#!/usr/bin/env python3
"""
docx template auto-fill — 將 .docx 模板中的 {{placeholder}} 替換為案件資料。

支援的佔位符：
  {{client_name}}     當事人姓名
  {{case_number}}     事務所案號
  {{court_name}}      法院
  {{court_case_no}}   法院案號
  {{case_reason}}     案由
  {{case_type}}       案件類型
  {{today}}           今天日期（中華民國格式）
  {{today_western}}   今天日期（西元格式）
  {{lawyer_name}}     律師姓名（從環境變數 MAGI_LAWYER_NAME）
  {{custom_key}}      自訂鍵值（透過 --data JSON 傳入）

用法：
  python template_fill.py --template 委任狀模板.docx --case-number 2025-0001 --output 委任狀.docx
  python template_fill.py --template template.docx --data '{"client_name":"王小明"}' --output out.docx
"""
import argparse
import json
import os
import re
import sys
import zipfile
import tempfile
import shutil
from datetime import date
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))


def _roc_date(d: date) -> str:
    """西元轉民國日期格式。"""
    return f"中華民國{d.year - 1911}年{d.month}月{d.day}日"


def _fetch_case_data(case_number: str) -> dict:
    """從 DB 取得案件資料。"""
    try:
        _osc_dir = str(MAGI_ROOT / "skills" / "osc-orchestrator")
        if _osc_dir not in sys.path:
            sys.path.insert(0, _osc_dir)
        from osc_headless.db import DBConfig, connect_mysql
        conn = connect_mysql(DBConfig())
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT case_number, client_name, case_type, case_reason,
                   court_name, status,
                   COALESCE(NULLIF(court_case_no, ''), court_case_number, '') AS court_case_no
            FROM cases
            WHERE case_number = %s
            LIMIT 1
            """,
            (case_number,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return {k: str(v or "") for k, v in row.items()}
    except Exception as e:
        print(f"⚠️ DB 查詢失敗（{e}），將使用提供的 data 參數。", file=sys.stderr)
    return {}


def fill_template(template_path: str, output_path: str, data: dict) -> dict:
    """
    讀取 .docx 模板，替換所有 {{key}} 佔位符，輸出新檔案。

    Returns:
        {"success": True/False, "replaced": [...], "not_found": [...]}
    """
    if not os.path.exists(template_path):
        return {"success": False, "error": f"模板檔案不存在：{template_path}"}

    # 準備替換資料（加入預設值）
    today = date.today()
    defaults = {
        "today": _roc_date(today),
        "today_western": today.strftime("%Y年%m月%d日"),
        "lawyer_name": os.environ.get("MAGI_LAWYER_NAME", ""),
    }
    merged = {**defaults, **data}

    replaced = []
    not_found = []

    with tempfile.TemporaryDirectory() as tmpdir:
        # 解壓模板
        try:
            with zipfile.ZipFile(template_path, "r") as zf:
                zf.extractall(tmpdir)
        except Exception as e:
            return {"success": False, "error": f"解壓失敗：{e}"}

        # 掃描所有 XML 檔，替換佔位符
        xml_files = list(Path(tmpdir).rglob("*.xml"))
        all_placeholders = set()

        for xml_file in xml_files:
            try:
                content = xml_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # 找出所有 {{key}}（可能被 XML tags 拆開，如 {{client<w:r>_name}}）
            # 先清理：合併被 XML run 拆開的佔位符
            cleaned = _merge_split_placeholders(content)

            # 找出所有佔位符
            found = set(re.findall(r"\{\{(\w+)\}\}", cleaned))
            all_placeholders.update(found)

            # 替換
            new_content = cleaned
            for key in found:
                if key in merged and merged[key]:
                    placeholder = "{{" + key + "}}"
                    new_content = new_content.replace(placeholder, _xml_escape(merged[key]))
                    if key not in replaced:
                        replaced.append(key)

            if new_content != content:
                xml_file.write_text(new_content, encoding="utf-8")

        not_found = [k for k in all_placeholders if k not in replaced]

        # 重新打包
        try:
            _repack_docx(tmpdir, output_path, template_path)
        except Exception as e:
            return {"success": False, "error": f"打包失敗：{e}"}

    return {
        "success": True,
        "output": output_path,
        "replaced": replaced,
        "not_found": not_found,
        "total_placeholders": len(all_placeholders),
    }


def _merge_split_placeholders(xml_content: str) -> str:
    """
    合併被 Word XML runs 拆開的 {{placeholder}}。

    Word 常把 {{client_name}} 拆成多個 <w:t> 元素：
    <w:t>{{</w:t></w:r><w:r><w:t>client_name</w:t></w:r><w:r><w:t>}}</w:t>

    這個函數找出被拆開的佔位符，合併到單一 <w:t> 中。
    """
    # 策略：找 {{ 後面到 }} 之間的所有內容，移除中間的 XML tags
    pattern = re.compile(
        r"(\{\{)"                           # 開始 {{
        r"((?:[^}]|(?:</w:t>.*?<w:t[^>]*>))*?)"  # 中間內容（可能穿插 XML tags）
        r"(\}\})",                           # 結束 }}
        re.DOTALL,
    )

    def _clean_match(m):
        middle = m.group(2)
        # 移除 XML tags，只保留文字
        clean = re.sub(r"</w:t>.*?<w:t[^>]*>", "", middle, flags=re.DOTALL)
        clean = re.sub(r"<[^>]+>", "", clean)
        return "{{" + clean.strip() + "}}"

    return pattern.sub(_clean_match, xml_content)


def _xml_escape(text: str) -> str:
    """Escape text for XML content."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _repack_docx(unpacked_dir: str, output_path: str, original_path: str):
    """Repack unpacked directory into a .docx file, preserving compression from original."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        base = Path(unpacked_dir)
        for file_path in sorted(base.rglob("*")):
            if file_path.is_file():
                arcname = str(file_path.relative_to(base))
                zout.write(str(file_path), arcname)


def main():
    ap = argparse.ArgumentParser(description="MAGI docx 模板自動填充")
    ap.add_argument("--template", required=True, help="模板 .docx 檔案路徑")
    ap.add_argument("--output", required=True, help="輸出 .docx 檔案路徑")
    ap.add_argument("--case-number", default="", help="事務所案號（自動從 DB 取資料）")
    ap.add_argument("--data", default="{}", help="自訂 JSON 資料（覆蓋 DB 資料）")
    args = ap.parse_args()

    # 合併資料來源
    case_data = {}
    if args.case_number:
        case_data = _fetch_case_data(args.case_number)

    try:
        custom = json.loads(args.data) if args.data else {}
    except json.JSONDecodeError:
        custom = {}

    merged = {**case_data, **custom}
    result = fill_template(args.template, args.output, merged)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
