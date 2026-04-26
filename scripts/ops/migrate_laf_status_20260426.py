#!/usr/bin/env python3
"""
2026-04-26 LAF 狀態流轉一次性遷移：
  舊 legal_aid_status = '已報結'          → 新 legal_aid_status = '已結案'  + legal_aid_approval_status = '已轉入'
  舊 legal_aid_status = '已報結（待轉入）' → 新 legal_aid_status = '已結案'  + legal_aid_approval_status = '待轉入'
  舊 legal_aid_status = '已結案，待送出'   → 不改主狀態 + legal_aid_approval_status = '暫存'（補 approval）

用法：
  python scripts/ops/migrate_laf_status_20260426.py            # dry-run（預設）
  python scripts/ops/migrate_laf_status_20260426.py --apply    # 實際 UPDATE

前置條件：
  1. 必須先執行 migrate_laf_status_20260426_schema.sql (ALTER TABLE)
  2. 或在 `--apply` 前確認 SHOW COLUMNS FROM cases LIKE 'legal_aid_approval_status'

輸出：
  dry-run: /tmp/migrate_laf_status_dryrun.json
  apply:   .runtime/migrations/migrate_laf_status_20260426_<ts>.jsonl
"""

import sys
import os
import json
import argparse
import datetime

# 加入 MAGI_v2 根目錄到 path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)


MIGRATION_MAP = [
    {
        "old_status":      "已報結",
        "new_status":      "已結案",
        "approval_status": "已轉入",
        "reason":          "法扶已轉入，事務所工作完成",
    },
    {
        "old_status":      "已報結（待轉入）",
        "new_status":      "已結案",
        "approval_status": "待轉入",
        "reason":          "法扶審核中（待轉入），事務所工作完成",
    },
    {
        "old_status":      "已結案，待送出",
        "new_status":      "已結案，待送出",   # 主狀態不動
        "approval_status": "暫存",
        "reason":          "補填 approval_status=暫存",
    },
]


def _connect_db():
    """連接本地 DB，使用 legalbridge_config.json 設定。"""
    config_path = os.path.join(_REPO_ROOT, "casper_ecosystem", "law_firm_orchestrators", "legalbridge_config.json")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    db_cfg = config.get("database", {})
    import pymysql
    conn = pymysql.connect(
        host=db_cfg.get("host", "127.0.0.1"),
        port=int(db_cfg.get("port", 3306)),
        user=db_cfg.get("user", "root"),
        password=db_cfg.get("password", ""),
        database=db_cfg.get("database", "law_firm_data"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    return conn


def check_schema(conn):
    """確認 legal_aid_approval_status 欄位已存在。"""
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM `cases` LIKE 'legal_aid_approval_status'")
        row = cur.fetchone()
    if not row:
        print("ERROR: 請先執行 ALTER TABLE 加 legal_aid_approval_status 欄位：")
        print("  mysql -u <user> -p law_firm_data < scripts/ops/migrate_laf_status_20260426_schema.sql")
        sys.exit(1)
    print("✅ Schema 確認：legal_aid_approval_status 欄位已存在")


def collect_candidates(conn):
    """查詢所有需要遷移的 case。"""
    old_statuses = [m["old_status"] for m in MIGRATION_MAP]
    # deduplicate
    old_statuses = list(dict.fromkeys(old_statuses))
    placeholders = ", ".join(["%s"] * len(old_statuses))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT `id`, `case_number`, `client_name`, `laf_case_no`, `legal_aid_status`, "
            f"`legal_aid_approval_status` FROM `cases` "
            f"WHERE `legal_aid_status` IN ({placeholders})",
            old_statuses,
        )
        return cur.fetchall()


def plan_migrations(rows):
    """依照 MIGRATION_MAP 建立遷移計畫。"""
    plans = []
    for row in rows:
        old = (row.get("legal_aid_status") or "").strip()
        cur_approval = (row.get("legal_aid_approval_status") or "").strip()
        for m in MIGRATION_MAP:
            if m["old_status"] == old:
                new_main = m["new_status"]
                new_approval = m["approval_status"]
                # 若已是目標狀態，跳過（冪等）
                if old == new_main and cur_approval == new_approval:
                    continue
                plans.append({
                    "id":                 row["id"],
                    "case_number":        row.get("case_number") or "",
                    "client_name":        row.get("client_name") or "",
                    "laf_case_no":        row.get("laf_case_no") or "",
                    "old_status":         old,
                    "old_approval":       cur_approval,
                    "new_status":         new_main,
                    "new_approval":       new_approval,
                    "reason":             m["reason"],
                })
                break
    return plans


def run_dry(plans):
    """Dry-run：印出計畫並寫 /tmp/migrate_laf_status_dryrun.json。"""
    print(f"\n=== DRY-RUN：預期 UPDATE {len(plans)} 筆 ===")
    for p in plans:
        print(
            f"  id={p['id']} {p['client_name']} ({p['case_number']}/{p['laf_case_no']}): "
            f"legal_aid_status「{p['old_status']}」→「{p['new_status']}」, "
            f"approval「{p['old_approval']}」→「{p['new_approval']}」  # {p['reason']}"
        )
    out = "/tmp/migrate_laf_status_dryrun.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(plans, f, ensure_ascii=False, indent=2)
    print(f"\n✅ Dry-run 報告已寫入：{out}")
    print("執行遷移：python scripts/ops/migrate_laf_status_20260426.py --apply")


def run_apply(conn, plans):
    """實際 UPDATE + 寫 jsonl 審計日誌。"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_dir = os.path.join(_REPO_ROOT, ".runtime", "migrations")
    os.makedirs(audit_dir, exist_ok=True)
    audit_path = os.path.join(audit_dir, f"migrate_laf_status_20260426_{ts}.jsonl")

    updated = 0
    skipped = 0
    errors = 0

    with conn.cursor() as cur, open(audit_path, "w", encoding="utf-8") as audit_f:
        for p in plans:
            try:
                # SELECT 確認 row 存在且狀態未變
                cur.execute(
                    "SELECT `id`, `legal_aid_status`, `legal_aid_approval_status` FROM `cases` WHERE `id` = %s FOR UPDATE",
                    (p["id"],),
                )
                row = cur.fetchone()
                if not row:
                    print(f"  ⚠️ id={p['id']} 不存在，跳過")
                    skipped += 1
                    continue
                cur_main = (row.get("legal_aid_status") or "").strip()
                cur_approval = (row.get("legal_aid_approval_status") or "").strip()
                if cur_main == p["new_status"] and cur_approval == p["new_approval"]:
                    print(f"  ℹ️ id={p['id']} {p['client_name']} 已是目標狀態，跳過（冪等）")
                    skipped += 1
                    continue
                # UPDATE
                cur.execute(
                    "UPDATE `cases` SET `legal_aid_status` = %s, `legal_aid_approval_status` = %s, "
                    "`legal_aid_approval_checked_at` = NOW() WHERE `id` = %s",
                    (p["new_status"], p["new_approval"], p["id"]),
                )
                conn.commit()
                updated += 1
                print(
                    f"  ✅ id={p['id']} {p['client_name']} ({p['case_number']}): "
                    f"「{cur_main}/{cur_approval}」→「{p['new_status']}/{p['new_approval']}」"
                )
                entry = dict(p, applied_at=ts, result="updated")
                audit_f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                conn.rollback()
                print(f"  ❌ id={p['id']} 更新失敗: {e}")
                errors += 1
                entry = dict(p, applied_at=ts, result="error", error=str(e))
                audit_f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"\n=== 遷移完成：updated={updated}, skipped={skipped}, errors={errors} ===")
    print(f"審計日誌：{audit_path}")

    # 驗證
    with conn.cursor() as cur:
        cur.execute(
            "SELECT `legal_aid_status`, COUNT(*) as cnt FROM `cases` "
            "WHERE `legal_aid_status` IN ('已報結', '已報結（待轉入）') GROUP BY `legal_aid_status`"
        )
        remaining = cur.fetchall()
    if remaining:
        print(f"⚠️ 仍有 deprecated 狀態未遷移：{remaining}")
    else:
        print("✅ 驗證通過：無殘留 deprecated 狀態")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="實際執行 UPDATE（預設 dry-run）")
    args = parser.parse_args()

    try:
        conn = _connect_db()
    except Exception as e:
        print(f"ERROR: DB 連線失敗：{e}")
        sys.exit(1)

    check_schema(conn)

    rows = collect_candidates(conn)
    print(f"\n找到 {len(rows)} 筆候選案件：")
    for r in rows:
        print(f"  id={r['id']} {r.get('client_name','')} ({r.get('case_number','')}): "
              f"legal_aid_status={r.get('legal_aid_status','')} / approval={r.get('legal_aid_approval_status','')}")

    plans = plan_migrations(rows)

    if not plans:
        print("\n✅ 無需遷移（所有案件已是目標狀態）")
        return

    if args.apply:
        run_apply(conn, plans)
    else:
        run_dry(plans)

    conn.close()


if __name__ == "__main__":
    main()
