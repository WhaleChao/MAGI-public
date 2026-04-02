#!/usr/bin/env python3
"""
快速入庫：將 judicial_api/raw/ 下的 JSON 直接寫入 court_judgments 表。
跳過 LLM 摘要，只存全文。摘要之後用 resummary 補。
"""
import sys, os, json, glob, hashlib, time

sys.stdout.reconfigure(line_buffering=True)
_MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _MAGI_ROOT)

import mysql.connector

DB_CONFIG = {
    "host": "100.121.61.74",
    "port": 3306,
    "user": "root",
    "password": "63181107",
    "database": "law_firm_data",
    "charset": "utf8mb4",
}

RAW_ROOT = "/Users/ai/.cache/judgment_collector/judicial_api/raw"
PROCESS_STATE = "/Users/ai/.cache/judgment_collector/judicial_api/process_state.json"

def load_state():
    try:
        with open(PROCESS_STATE, "r") as f:
            return json.load(f).get("processed", {})
    except Exception:
        return {}

def save_state(processed):
    with open(PROCESS_STATE, "w") as f:
        json.dump({"processed": processed}, f, ensure_ascii=False)

def extract_fields(payload):
    """Extract fields from JDoc payload."""
    jfullx = payload.get("JFULLX") or {}
    if isinstance(jfullx, list):
        jfullx = jfullx[0] if jfullx else {}
    return {
        "jid": str(payload.get("JID") or "").strip(),
        "jyear": str(payload.get("JYEAR") or "").strip(),
        "jcase": str(payload.get("JCASE") or "").strip(),
        "jno": str(payload.get("JNO") or "").strip(),
        "jdate": str(payload.get("JDATE") or "").strip(),
        "jtitle": str(payload.get("JTITLE") or "").strip(),
        "full_text": str(jfullx.get("JFULLCONTENT") or "").strip(),
    }

def court_name_from_jid(jid):
    """Extract court code from JID."""
    parts = jid.split(",")
    return parts[0] if parts else ""

def parse_jdate(jdate_str):
    """Parse YYYYMMDD to YYYY-MM-DD."""
    s = jdate_str.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None

def main():
    processed = load_state()
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # 確保表存在
    cursor.execute("SELECT COUNT(*) FROM court_judgments")
    existing = cursor.fetchone()[0]
    print(f"Current court_judgments: {existing}")

    files = sorted(glob.glob(os.path.join(RAW_ROOT, "**", "*.json"), recursive=True))
    print(f"Raw files: {len(files)}")

    inserted = 0
    updated = 0
    skipped = 0
    errors = 0

    for i, fpath in enumerate(files):
        rel = os.path.relpath(fpath, os.path.dirname(RAW_ROOT))

        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw_text = f.read()
        except Exception:
            errors += 1
            continue

        raw_hash = hashlib.sha1(raw_text.encode("utf-8", errors="ignore")).hexdigest()
        if processed.get(rel) == raw_hash:
            skipped += 1
            continue

        try:
            raw_obj = json.loads(raw_text)
        except Exception:
            errors += 1
            continue

        payload = raw_obj.get("payload") or {}
        if not isinstance(payload, dict):
            skipped += 1
            processed[rel] = raw_hash
            continue

        # 查無資料 / 錯誤
        if payload.get("error"):
            skipped += 1
            processed[rel] = raw_hash
            continue

        fields = extract_fields(payload)
        jid = fields["jid"] or str(raw_obj.get("jid") or "").strip()
        if not jid:
            skipped += 1
            processed[rel] = raw_hash
            continue

        full_text = fields["full_text"]
        if not full_text or len(full_text) < 50:
            skipped += 1
            processed[rel] = raw_hash
            continue

        court_name = court_name_from_jid(jid)
        case_number = ""
        if fields["jyear"] and fields["jcase"] and fields["jno"]:
            case_number = f"{fields['jyear']}年度{fields['jcase']}字第{fields['jno']}號"
        judgment_date = parse_jdate(fields["jdate"])
        case_type = "行政" if "行政" in court_name else "一般"

        # 低價值過濾 — 直接跳過不入庫（最高/高等法院全部保留）
        _is_upper_court = jid.startswith("TPS") or jid.startswith("TPH") or "最高" in court_name or "高等" in court_name
        _SKIP_CASE_PATTERNS = re.compile(
            r"司促字|促字第|司票字|票字第|補字第|附民字|續收字|司催字|司消債核字|"
            r"司執字|司繼字|司聲字|全字第|暫字第|拍字第|司拍字"
        )
        if (not _is_upper_court) and _SKIP_CASE_PATTERNS.search(case_number or ""):
            skipped += 1
            processed[rel] = raw_hash
            continue

        header = full_text[:500]
        if any(kw in header for kw in ["支付命令", "補費裁定"]):
            skipped += 1
            processed[rel] = raw_hash
            continue

        summary = ""  # 之後 resummary 會補

        try:
            cursor.execute(
                """INSERT INTO court_judgments (jid, court_name, case_number, case_type, judgment_date, summary, full_text, source_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    full_text = IF(VALUES(full_text) != '' AND VALUES(full_text) IS NOT NULL, VALUES(full_text), full_text),
                    court_name = IF(VALUES(court_name) != '', VALUES(court_name), court_name),
                    case_number = IF(VALUES(case_number) != '', VALUES(case_number), case_number)
                """,
                (jid, court_name, case_number or jid, case_type, judgment_date,
                 summary, full_text, "https://data.judicial.gov.tw/jdg/api/JDoc")
            )
            if cursor.rowcount == 1:
                inserted += 1
            elif cursor.rowcount == 2:
                updated += 1
            else:
                skipped += 1
            conn.commit()
        except Exception as e:
            errors += 1
            if errors < 5:
                print(f"  DB error: {e}")
            conn.rollback()

        processed[rel] = raw_hash

        if (inserted + updated) % 500 == 0 and (inserted + updated) > 0:
            save_state(processed)
            print(f"  [{i+1}/{len(files)}] inserted={inserted} updated={updated} skipped={skipped} errors={errors}")

    save_state(processed)
    cursor.close()
    conn.close()

    print(f"\n=== DONE ===")
    print(f"Inserted: {inserted}")
    print(f"Updated: {updated}")
    print(f"Skipped: {skipped}")
    print(f"Errors: {errors}")

if __name__ == "__main__":
    main()
