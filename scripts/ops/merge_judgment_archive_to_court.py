#!/usr/bin/env python3
"""
merge_judgment_archive_to_court.py

將 `judgment_archive` 表與 `skills/judgment-collector/judgments.json` 合併進
`court_judgments`（OSC 面板可見的正式實務見解庫）。

背景（2026-04-21）：
  - 遠端 DB 已故障，所有判決資料現在只在本機 MariaDB
  - `court_judgments`（16,164 筆）是 OSC 讀取的正式來源
  - `judgment_archive`（2,348 筆）原為 `api/domains/judgment_flow.py` 的 fallback
    當 judgment-collector skill 失敗時才讀
  - `judgments.json`（295 筆，125 筆已 enriched）是 legacy file export
  - 使用者要求把 archive + json 合併進 court_judgments，統一成一個實務見解庫

邏輯：
  - archive 有 source_jid → jid 對應 court_judgments.jid
    * overlap：backfill court.full_text/summary 若 court 該欄空
    * archive-only：INSERT 新 row
  - archive 無 source_jid：試著從 judgment_title 正規化出 jid；失敗則 skip
  - full_text 優先順序：archive.full_text_path 檔案 > 無
  - summary 來源：archive.summary_text 且 is_degraded=0 才採用（is_degraded=1 是 stub 佔位）
  - judgments.json 有 jid（125 筆） → 套用相同 INSERT ON DUPLICATE KEY UPDATE
  - 全程 UTF-8；COLLATE 混淆問題以 python 端字串比對處理

使用：
  # dry-run 看 plan
  ./venv/bin/python3 scripts/ops/merge_judgment_archive_to_court.py

  # 實際執行
  ./venv/bin/python3 scripts/ops/merge_judgment_archive_to_court.py --apply

  # 只跑 archive 或 json
  ./venv/bin/python3 scripts/ops/merge_judgment_archive_to_court.py --scope archive
  ./venv/bin/python3 scripts/ops/merge_judgment_archive_to_court.py --scope json

安全：
  - 預設 --dry-run；不加 --apply 不會寫入
  - UPDATE 只 backfill 空欄位（不覆蓋 court_judgments 既有資料）
  - archive 原表不刪（merge 成功後可用 --drop-archive 另外刪表，不在本腳本處理）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure repo root on path
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.db_helper import get_cursor  # noqa: E402

logger = logging.getLogger("merge_judgments")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)


def _db_config() -> Dict[str, Any]:
    return {
        "host": os.environ.get("OSC_DB_HOST_LOCAL", "127.0.0.1"),
        "port": int(os.environ.get("OSC_DB_PORT_LOCAL", "3306")),
        "user": os.environ.get("OSC_DB_USER", "casper_service"),
        "password": os.environ.get("OSC_DB_PASSWORD", ""),
        "database": "law_firm_data",
        "use_pure": True,
        "connection_timeout": 10,
        "charset": "utf8mb4",
    }


# -------------------------------------------------------------------
# jid parsing / normalization
# -------------------------------------------------------------------

# jid 格式示例：CHDM,115,毒聲,79,20260304,1 或 TPHM,114,上訴,4682,20251128,1
_JID_RE = re.compile(r"^[A-Z]{2,8},\d+,[^,]+,\d+,\d{8},\d+$")

# 法院中文名 → 代碼 對照（常見，不完整，僅用於回退解析）
_COURT_ZH_TO_CODE = {
    "最高法院": "TPSM",
    "最高行政法院": "TPAM",
    "臺灣高等法院": "TPHM",
    "台灣高等法院": "TPHM",
    "臺北地方法院": "TPDM",
    "新北地方法院": "PCDM",
    "桃園地方法院": "TYDM",
    "新竹地方法院": "SCDM",
    "苗栗地方法院": "MLDM",
    "臺中地方法院": "TCDM",
    "彰化地方法院": "CHDM",
    "南投地方法院": "NTDM",
    "雲林地方法院": "ULDM",
    "嘉義地方法院": "CYDM",
    "臺南地方法院": "TNDM",
    "高雄地方法院": "KSDM",
    "花蓮地方法院": "HLDM",
    "宜蘭地方法院": "ILDM",
    "臺東地方法院": "TTDM",
    "基隆地方法院": "KLDM",
    "澎湖地方法院": "PHDM",
    "金門地方法院": "KMDM",
}


def _is_valid_jid(s: Optional[str]) -> bool:
    return bool(s) and bool(_JID_RE.match(s.strip()))


def _parse_jid_from_title(title: str, court_level: str = "") -> Optional[str]:
    """
    試著從 judgment_title 還原 jid。
    已知格式：
      "CHDM CHDM,115,毒聲,79,20260304,1"  → 第 2 段直接就是 jid
      "CHDV 114年度全字第54號定暫時狀態之處分"  → court+case 描述
      "最高法院  66,台上,3281  執行異議  民事  判決"  → 舊格式不規則
    回傳 None 時代表無法可靠解析。
    """
    if not title:
        return None
    s = title.strip()
    # 1) 直接掃 jid pattern
    m = re.search(r"\b([A-Z]{2,8},\d+,[^\s,]+,\d+,\d{8},\d+)\b", s)
    if m:
        return m.group(1)
    # 2) "<code> 114年度全字第54號..."  但通常沒日期，不構造
    # 3) 中文法院名 "XX地方法院 114年度..." — 同上，缺日期
    # 保守：不構造
    return None


# -------------------------------------------------------------------
# full_text file reader
# -------------------------------------------------------------------

_MAX_FULLTEXT_BYTES = 2_000_000  # 2MB 上限


def _read_full_text(path: str) -> Optional[str]:
    if not path:
        return None
    try:
        if not os.path.exists(path):
            return None
        sz = os.path.getsize(path)
        if sz < 200:
            # 可能只是 stub / placeholder
            return None
        if sz > _MAX_FULLTEXT_BYTES:
            logger.warning("full_text_path too large (%dB), skip: %s", sz, path)
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
        txt = txt.strip()
        if len(txt) < 100:
            return None
        return txt
    except Exception as exc:
        logger.warning("read full_text_path failed (%s): %s", path, exc)
        return None


# -------------------------------------------------------------------
# merge core
# -------------------------------------------------------------------


def _parse_judgment_date(raw: Any) -> Optional[_date]:
    """archive.judgment_date 是 varchar，格式 '2025-12-16' 或 '' 或 None"""
    if not raw:
        return None
    if isinstance(raw, _date):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    # 接受 ISO (YYYY-MM-DD) 或 YYYYMMDD
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            y, m, d = s.split("-")
            return _date(int(y), int(m), int(d))
        if re.match(r"^\d{8}$", s):
            return _date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except Exception:
        pass
    return None


def _court_name_from_level(court_level: str, title: str = "") -> str:
    """archive.court_level 存的是法院代碼（CHDM）或中文（最高法院）。
    回傳中文法院名（盡量），不成功就回原值。"""
    if not court_level:
        # 從 title 掃第一段
        if title:
            for zh, _code in _COURT_ZH_TO_CODE.items():
                if title.startswith(zh):
                    return zh
        return ""
    c = court_level.strip()
    # 如果本身就是中文
    if re.search(r"[\u4e00-\u9fff]", c):
        return c
    # 代碼反查 — 有對照才換
    for zh, code in _COURT_ZH_TO_CODE.items():
        if code == c:
            return zh
    # 沒對照就直接回代碼（court_judgments.court_name 裡也有代碼形式）
    return c


def _court_judgments_lookup(cur, jid: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT id, jid, full_text, summary, source_url FROM court_judgments WHERE jid = %s LIMIT 1",
        (jid,),
    )
    row = cur.fetchone()
    return row


def _upsert_court_judgment(
    cur,
    jid: str,
    *,
    court_name: str,
    case_number: str,
    case_type: str,
    judgment_date: Optional[_date],
    summary: Optional[str],
    full_text: Optional[str],
    source_url: Optional[str],
) -> str:
    """
    回傳動作：'inserted' / 'updated' / 'noop'
    只 backfill 空欄位 — 不覆寫 court_judgments 既有非空欄位。
    """
    existing = _court_judgments_lookup(cur, jid)
    if existing is None:
        # INSERT
        cur.execute(
            """
            INSERT INTO court_judgments
                (jid, court_name, case_number, case_type, judgment_date, summary, full_text, source_url, crawled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                jid,
                court_name or None,
                case_number or None,
                case_type or None,
                judgment_date,
                summary,
                full_text,
                source_url,
            ),
        )
        return "inserted"

    # UPDATE — 只填空
    sets: List[str] = []
    vals: List[Any] = []
    if full_text and not (existing.get("full_text") or "").strip():
        sets.append("full_text = %s")
        vals.append(full_text)
    if summary and not (existing.get("summary") or "").strip():
        sets.append("summary = %s")
        vals.append(summary)
    if source_url and not (existing.get("source_url") or "").strip():
        sets.append("source_url = %s")
        vals.append(source_url)
    if not sets:
        return "noop"
    vals.append(existing["id"])
    cur.execute(
        f"UPDATE court_judgments SET {', '.join(sets)} WHERE id = %s",
        tuple(vals),
    )
    return "updated"


# -------------------------------------------------------------------
# scope: archive → court
# -------------------------------------------------------------------


def merge_archive_to_court(
    *,
    apply: bool,
    limit: Optional[int] = None,
) -> Dict[str, int]:
    stats = {
        "scanned": 0,
        "inserted": 0,
        "updated": 0,
        "noop": 0,
        "skipped_no_jid": 0,
        "skipped_no_data": 0,
        "skipped_file_missing": 0,
    }
    with get_cursor(config=_db_config(), dictionary=True, buffered=True) as (conn, cur):
        sql = """
            SELECT id, case_number, case_reason, case_type, court_level,
                   judgment_title, judgment_url, judgment_date,
                   full_text_path, summary_text, is_degraded, source_jid
            FROM judgment_archive
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql)
        rows = cur.fetchall()
        logger.info("scanned %d archive rows", len(rows))
        stats["scanned"] = len(rows)

        for r in rows:
            aid = r["id"]
            jid = (r.get("source_jid") or "").strip()
            if not _is_valid_jid(jid):
                jid_alt = _parse_jid_from_title(r.get("judgment_title") or "", r.get("court_level") or "")
                if jid_alt and _is_valid_jid(jid_alt):
                    jid = jid_alt
                else:
                    stats["skipped_no_jid"] += 1
                    continue

            # full_text
            ft = _read_full_text(r.get("full_text_path") or "")
            if not ft and r.get("full_text_path"):
                # path set but file missing/short
                stats["skipped_file_missing"] += 0  # 不當硬錯；只是沒拿到 full_text

            # summary — is_degraded=1 代表是 stub 佔位，不採用
            summary = None
            stxt = (r.get("summary_text") or "").strip()
            if stxt and not int(r.get("is_degraded") or 0):
                summary = stxt
            # 若 court_judgments 裡也空，至少有降級 summary 比無還好，但 stub 內容多為「系統降級回覆」
            # 故保守不用 is_degraded=1 的 summary

            if not ft and not summary and not (r.get("case_number") or r.get("judgment_title")):
                stats["skipped_no_data"] += 1
                continue

            court_name = _court_name_from_level(r.get("court_level") or "", r.get("judgment_title") or "")
            case_number = (r.get("case_number") or "").strip()
            case_type = (r.get("case_type") or "").strip() or "一般"
            jd = _parse_judgment_date(r.get("judgment_date"))
            source_url = (r.get("judgment_url") or "").strip() or None

            if not apply:
                # dry-run：只 lookup 決定 action
                existing = _court_judgments_lookup(cur, jid)
                if existing is None:
                    stats["inserted"] += 1
                else:
                    if (ft and not (existing.get("full_text") or "").strip()) or \
                       (summary and not (existing.get("summary") or "").strip()) or \
                       (source_url and not (existing.get("source_url") or "").strip()):
                        stats["updated"] += 1
                    else:
                        stats["noop"] += 1
                continue

            action = _upsert_court_judgment(
                cur,
                jid,
                court_name=court_name,
                case_number=case_number,
                case_type=case_type,
                judgment_date=jd,
                summary=summary,
                full_text=ft,
                source_url=source_url,
            )
            stats[action] += 1
            # periodic commit
            if (stats["inserted"] + stats["updated"]) % 200 == 0:
                conn.commit()

        if apply:
            conn.commit()

    return stats


# -------------------------------------------------------------------
# scope: judgments.json → court
# -------------------------------------------------------------------

_JSON_PATH = ROOT / "skills" / "judgment-collector" / "judgments.json"


def merge_json_to_court(*, apply: bool, limit: Optional[int] = None) -> Dict[str, int]:
    stats = {
        "scanned": 0,
        "inserted": 0,
        "updated": 0,
        "noop": 0,
        "skipped_no_jid": 0,
    }
    if not _JSON_PATH.exists():
        logger.warning("judgments.json not found: %s", _JSON_PATH)
        return stats
    try:
        with open(_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.error("read judgments.json failed: %s", exc)
        return stats

    if not isinstance(data, list):
        logger.error("judgments.json root is not a list")
        return stats

    if limit:
        data = data[: int(limit)]

    with get_cursor(config=_db_config(), dictionary=True, buffered=True) as (conn, cur):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            stats["scanned"] += 1
            jid = (entry.get("jid") or "").strip()
            if not _is_valid_jid(jid):
                # 嘗試從 title / url 解析
                jid_alt = _parse_jid_from_title(entry.get("title") or "")
                if jid_alt and _is_valid_jid(jid_alt):
                    jid = jid_alt
                else:
                    stats["skipped_no_jid"] += 1
                    continue

            title = (entry.get("title") or "").strip()
            court_name = ""
            for zh in _COURT_ZH_TO_CODE:
                if title.startswith(zh):
                    court_name = zh
                    break
            # case_number 從 title 擷取
            case_number = ""
            m = re.search(r"(\d{1,3}年度[^第]+第\d+號[^\s]*)", title)
            if m:
                case_number = m.group(1).strip()

            full_text = (entry.get("full_text") or "").strip() or None
            summary = (entry.get("summary") or "").strip() or None
            source_url = (entry.get("data_url") or entry.get("url") or "").strip() or None

            if not apply:
                existing = _court_judgments_lookup(cur, jid)
                if existing is None:
                    stats["inserted"] += 1
                else:
                    if (full_text and not (existing.get("full_text") or "").strip()) or \
                       (summary and not (existing.get("summary") or "").strip()) or \
                       (source_url and not (existing.get("source_url") or "").strip()):
                        stats["updated"] += 1
                    else:
                        stats["noop"] += 1
                continue

            action = _upsert_court_judgment(
                cur,
                jid,
                court_name=court_name,
                case_number=case_number,
                case_type="一般",
                judgment_date=None,
                summary=summary,
                full_text=full_text,
                source_url=source_url,
            )
            stats[action] += 1
            if (stats["inserted"] + stats["updated"]) % 50 == 0:
                conn.commit()

        if apply:
            conn.commit()

    return stats


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Merge judgment_archive + judgments.json into court_judgments")
    p.add_argument("--apply", action="store_true", help="actually write to DB (default: dry-run)")
    p.add_argument("--scope", choices=["archive", "json", "all"], default="all")
    p.add_argument("--limit", type=int, default=None, help="limit rows for testing")
    args = p.parse_args(argv)

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("mode=%s scope=%s limit=%s", mode, args.scope, args.limit)

    t0 = time.time()
    all_stats: Dict[str, Dict[str, int]] = {}

    if args.scope in ("archive", "all"):
        logger.info("[archive] starting merge ...")
        s = merge_archive_to_court(apply=args.apply, limit=args.limit)
        all_stats["archive"] = s
        logger.info("[archive] %s", s)

    if args.scope in ("json", "all"):
        logger.info("[json] starting merge ...")
        s = merge_json_to_court(apply=args.apply, limit=args.limit)
        all_stats["json"] = s
        logger.info("[json] %s", s)

    logger.info("done in %.1fs mode=%s", time.time() - t0, mode)
    print(json.dumps({"mode": mode, "stats": all_stats}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
