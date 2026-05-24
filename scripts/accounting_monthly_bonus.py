#!/usr/bin/env python3
"""Run MAGI monthly accounting bonus settlement."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:
    pass

from api.osc.accounting_bonus import calculate_monthly_bonus, export_monthly_bonus_xlsx, record_monthly_bonus_xlsx_path  # noqa: E402
from api.osc.accounting_sheet_import import DEFAULT_ACCOUNT_HINT  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI 每月帳務獎金結算")
    parser.add_argument("--month", default=None, help="結算月份 YYYY-MM；未指定時依 24 日/補抓窗口判斷")
    parser.add_argument("--commit", action="store_true", help="正式登載獎金支出並記錄月結結果")
    parser.add_argument("--refresh-import", action="store_true", help="先補抓同事 Google 帳務表")
    parser.add_argument("--no-refresh-import", action="store_true", help="不補抓帳務表")
    parser.add_argument("--catch-up", action="store_true", help="月初 1~7 日仍可補算前一個結算月")
    parser.add_argument("--export-xlsx", action="store_true", help="輸出 XLSX 報表")
    parser.add_argument("--account-hint", default=DEFAULT_ACCOUNT_HINT)
    args = parser.parse_args(argv)

    refresh = True
    if args.no_refresh_import:
        refresh = False
    elif args.refresh_import:
        refresh = True

    try:
        result = calculate_monthly_bonus(
            month=args.month,
            commit=args.commit,
            refresh_import=refresh,
            catch_up=args.catch_up,
            account_hint=args.account_hint,
        )
        if args.export_xlsx and result.get("ok") and not result.get("skipped"):
            result["xlsx_path"] = export_monthly_bonus_xlsx(result)
            if args.commit:
                record_monthly_bonus_xlsx_path(str(result.get("month") or ""), result["xlsx_path"])
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
