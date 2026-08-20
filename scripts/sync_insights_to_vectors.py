#!/usr/bin/env python3
"""
legal_insights ↔ magi_brain 雙向同步
=====================================
將 law_firm_data.legal_insights 中有 insight_text 的記錄同步到
magi_brain 向量記憶庫，讓 MAGI recall 能搜到手動新增的見解。

用法:
    # 預覽
    python scripts/sync_insights_to_vectors.py --dry-run

    # 執行同步
    python scripts/sync_insights_to_vectors.py

    # 作為 cron / LaunchAgent 定期執行
    python scripts/sync_insights_to_vectors.py --quiet
"""
import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import mysql.connector

_ROOT = Path(__file__).resolve().parents[1]
_AGENT_DIR = Path(
    os.environ.get("MAGI_AGENT_DIR", "").strip() or _ROOT / ".agent"
).expanduser()

try:
    from api.mysql_connector_guard import patch_mysql_connector_for_stability
except Exception:
    patch_mysql_connector_for_stability = None

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    if os.environ.get("MAGI_V3_SCHEDULE_FIXTURE") != "1":
        _load_dotenv(
            os.environ.get("MAGI_ENV_FILE") or (_ROOT / ".env"),
            override=False,
        )
except Exception:
    pass

logger = logging.getLogger("insight_sync")
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
if patch_mysql_connector_for_stability:
    patch_mysql_connector_for_stability()

# ---------- DB configs ----------
# 2026-04-25: 遠端 DB (MAGI_REMOTE_DB_HOST) 已故障，所有資料回收到本機 MariaDB。
# 為避免 cron 環境 OSC_DB_HOST 被誤注入舊遠端值，這裡明確強制 127.0.0.1。
# 若未來確認遠端恢復需切回，移除下方 _force_local 並注釋還原即可。
_force_local = os.environ.get("MAGI_INSIGHT_SYNC_FORCE_LOCAL", "1") == "1"
_remote_host = "127.0.0.1" if _force_local else os.environ.get("OSC_DB_HOST", "127.0.0.1")
# 使用本機 casper_service 帳號（不是 python_user，後者只在遠端 DB）
_remote_user = os.environ.get("DB_USER", "casper_service") if _force_local else os.environ.get("OSC_DB_USER", "python_user")
_remote_pass = os.environ.get("DB_PASSWORD", "") if _force_local else os.environ.get("OSC_DB_PASSWORD", "")

REMOTE_DB = {
    "host": _remote_host,
    "port": int(os.environ.get("OSC_DB_PORT", "3306")),
    "user": _remote_user,
    "password": _remote_pass,
    "database": "law_firm_data",
    "connection_timeout": 10,
}

LOCAL_DB = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "casper_service"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": "magi_brain",
    "connection_timeout": 10,
}

OMLX_URL = os.environ.get("OMLX_EMBED_URL", "http://127.0.0.1:8081/v1/embeddings")
EMBED_MODEL = os.environ.get("OMLX_EMBED_MODEL", os.environ.get("MAGI_OMLX_EMBED_MODEL", "modernbert-embed-4bit"))
SOURCE_PREFIX = "legal_insight"


def _embedding_is_valid(embedding: object) -> bool:
    if not isinstance(embedding, list) or not embedding:
        return False
    try:
        return any(abs(float(x)) > 1e-12 for x in embedding)
    except Exception:
        return False


def _get_embedding(text: str) -> list | None:
    """Get embedding from oMLX."""
    fixture = _load_embedding_fixture_provider()
    if fixture is not None:
        return _fixture_embedding(text, fixture)
    import requests
    try:
        resp = requests.post(
            OMLX_URL,
            json={"input": text, "model": EMBED_MODEL},
            timeout=30,
        )
        resp.raise_for_status()
        embedding = resp.json()["data"][0]["embedding"]
        if not _embedding_is_valid(embedding):
            raise ValueError("embedding service returned empty/zero vector")
        return embedding
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
        return None


def _get_embeddings_batch(texts: list) -> list:
    """Batch embed via oMLX."""
    import requests
    results = []
    # oMLX may not support true batching, so do one by one
    for t in texts:
        results.append(_get_embedding(t))
    return results


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def _build_mem_content(row: dict) -> str:
    """Build a searchable text block from a legal_insights row."""
    parts = []
    if row.get("case_number"):
        parts.append(f"案號：{row['case_number']}")
    if row.get("case_reason"):
        parts.append(f"案由：{row['case_reason']}")
    if row.get("court_reference"):
        parts.append(f"裁判字號：{row['court_reference']}")
    if row.get("insight_type"):
        parts.append(f"類型：{row['insight_type']}")
    if row.get("document_name"):
        parts.append(f"文件：{row['document_name']}")
    parts.append("")
    parts.append(row.get("insight_text") or "")
    return "\n".join(parts).strip()


def _plan_new_insights(
    insights: list[dict], existing_hashes: set[str]
) -> list[tuple[dict, str]]:
    """Plan deterministic inserts without touching either database or embedding API."""

    observed = set(existing_hashes)
    planned: list[tuple[dict, str]] = []
    for insight in insights:
        content = _build_mem_content(insight)
        digest = _content_hash(content)
        if content and digest not in observed:
            planned.append((insight, content))
            observed.add(digest)
    return planned


def _load_embedding_fixture_provider() -> tuple[Path, Path, dict] | None:
    raw = str(os.environ.get("MAGI_INSIGHT_SYNC_EMBED_FIXTURE_PATH") or "").strip()
    if not raw:
        return None
    root_raw = str(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "").strip()
    if os.environ.get("MAGI_V3_SCHEDULE_FIXTURE") != "1" or not root_raw:
        raise RuntimeError("insight embedding fixture is not safely bound")
    root = Path(root_raw).expanduser().resolve()
    provider_path = Path(raw).expanduser().resolve()
    if (
        not (root / ".magi-v3-schedule-fixture").is_file()
        or not provider_path.is_file()
        or provider_path.is_symlink()
        or not provider_path.is_relative_to(root)
    ):
        raise RuntimeError("insight embedding fixture escaped its owned root")
    try:
        provider = json.loads(provider_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("insight embedding fixture is unreadable") from exc
    if (
        provider.get("schema") != "magi.v3.insight-embedding-provider/v1"
        or type(provider.get("dimensions")) is not int
        or not 4 <= provider["dimensions"] <= 64
        or not isinstance(provider.get("salt"), str)
    ):
        raise RuntimeError("insight embedding fixture schema is invalid")
    return root, provider_path, provider


def _fixture_embedding(text: str, fixture: tuple[Path, Path, dict]) -> list[float]:
    root, provider_path, provider = fixture
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("insight embedding fixture received empty text")
    digest = hashlib.sha256((provider["salt"] + "\n" + text).encode("utf-8")).digest()
    dimensions = int(provider["dimensions"])
    embedding = [round((digest[index] - 127.5) / 127.5, 8) for index in range(dimensions)]
    if not _embedding_is_valid(embedding):
        raise RuntimeError("insight embedding fixture generated an invalid vector")
    receipt = {
        "schema": "magi.insight-embedding-receipt/v1",
        "receipt_id": uuid.uuid4().hex,
        "created_ns": time.time_ns(),
        "pid": os.getpid(),
        "handler": "_get_embedding",
        "provider": "bounded_embedding_provider",
        "provider_sha256": hashlib.sha256(provider_path.read_bytes()).hexdigest(),
        "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "embedding_sha256": hashlib.sha256(
            json.dumps(embedding, separators=(",", ":")).encode()
        ).hexdigest(),
        "dimensions": dimensions,
    }
    receipt_path = root / "workspace" / "embedding-receipts.jsonl"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
    return embedding


def _require_formal_callable(handler, expected_name: str) -> None:
    if (
        getattr(handler, "__module__", "") != __name__
        or getattr(handler, "__name__", "") != expected_name
    ):
        raise RuntimeError(f"insight sync formal handler rejected: {expected_name}")


def sync(dry_run: bool = False, quiet: bool = False):
    """Main sync: legal_insights → magi_brain vectors."""
    t0 = time.time()

    # 1. Read all legal_insights with content
    remote_conn = mysql.connector.connect(**REMOTE_DB)
    remote_cur = remote_conn.cursor(dictionary=True)
    remote_cur.execute(
        "SELECT id, case_number, case_reason, court_reference, court_type, "
        "insight_type, insight_text, document_name, source_file, is_degraded, "
        "extracted_date FROM legal_insights "
        "WHERE insight_text IS NOT NULL AND insight_text != '' AND is_degraded = 0"
    )
    insights = remote_cur.fetchall()
    remote_cur.close()
    remote_conn.close()

    # Dedup window: 只比對近 N 天（避免日量上千時 O(n²) 爆炸）。
    # 0 = 不限。預設 30 天，可由 INSIGHT_DEDUP_WINDOW_DAYS 覆寫。
    try:
        dedup_window_days = int(os.environ.get("INSIGHT_DEDUP_WINDOW_DAYS", "30") or "30")
    except Exception:
        dedup_window_days = 30

    if not insights:
        if not quiet:
            print("沒有可同步的見解")
        return

    # 2. Check which are already in magi_brain (by content hash)
    local_conn = mysql.connector.connect(**LOCAL_DB)
    local_cur = local_conn.cursor()

    # Get existing hashes from magi_brain across all sources so manual
    # insights do not duplicate already-indexed knowledge.
    local_cur.execute(
        "SELECT MD5(content) FROM documents"
    )
    existing_hashes = {row[0] for row in local_cur.fetchall()}

    # Also check all content hashes to avoid cross-source duplicates
    new_insights = _plan_new_insights(insights, existing_hashes)

    if not new_insights:
        if not quiet:
            print(f"✅ 所有 {len(insights)} 筆見解已在向量庫中，無需同步")
        local_cur.close()
        local_conn.close()
        return

    if not quiet:
        print(f"📊 legal_insights: {len(insights)} 筆有內容")
        print(f"   已向量化: {len(insights) - len(new_insights)} 筆")
        print(f"   待同步:   {len(new_insights)} 筆")

    if dry_run:
        print(f"\n🔍 預覽模式 — 不會寫入")
        for ins, content in new_insights[:5]:
            print(f"   [{ins['id']}] {ins.get('case_reason', '?')[:20]} — {content[:60]}...")
        if len(new_insights) > 5:
            print(f"   ... 還有 {len(new_insights) - 5} 筆")
        local_cur.close()
        local_conn.close()
        return

    # 3. Embed and insert
    if not quiet:
        print(f"\n⏳ 向量化 {len(new_insights)} 筆見解...")

    texts = [content for _, content in new_insights]
    embeddings = _get_embeddings_batch(texts)

    inserted = 0
    skipped_embedding = 0
    for i, ((ins, content), emb) in enumerate(zip(new_insights, embeddings)):
        if not _embedding_is_valid(emb):
            skipped_embedding += 1
            logger.warning("Skipping insight %s: embedding unavailable", ins.get("id"))
            continue
        source = f"{SOURCE_PREFIX}|id={ins['id']}|reason={ins.get('case_reason', '')[:30]}"[:250]
        try:
            local_cur.execute(
                "INSERT INTO documents (content, source) VALUES (%s, %s)",
                (content, source),
            )
            doc_id = local_cur.lastrowid
            local_cur.execute(
                "INSERT INTO vectors (doc_id, embedding) VALUES (%s, %s)",
                (doc_id, json.dumps(emb)),
            )
            local_conn.commit()
            inserted += 1
        except Exception as e:
            logger.warning("Insert failed for insight %d: %s", ins["id"], e)
            local_conn.rollback()

    local_cur.close()
    local_conn.close()

    # 4. Semantic dedup: flag near-duplicate insights within same case_reason
    #    只在近 dedup_window_days 內的見解之間比對，避免全表 O(n²)。
    if dedup_window_days > 0:
        cutoff_iso = (
            __import__("datetime").datetime.now() -
            __import__("datetime").timedelta(days=dedup_window_days)
        ).strftime("%Y-%m-%d")
        dedup_pool = [
            ins for ins in insights
            if str(ins.get("extracted_date") or "")[:10] >= cutoff_iso
        ]
    else:
        dedup_pool = insights
    dedup_flagged = _flag_semantic_dupes(dedup_pool, quiet=quiet)

    # Append run summary to ingestion log (cron 健康監測用)
    try:
        log_path = _AGENT_DIR / "insight_sync_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "candidates": len(insights),
                "new": len(new_insights),
                "inserted": inserted,
                "skipped_embedding": skipped_embedding,
                "dedup_flagged": dedup_flagged,
                "dedup_pool_size": len(dedup_pool),
                "elapsed_sec": round(time.time() - t0, 2),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    elapsed = time.time() - t0
    if skipped_embedding:
        raise RuntimeError(f"embedding_failed:{skipped_embedding}/{len(new_insights)}")
    if not quiet:
        print(f"\n✅ 同步完成！寫入 {inserted} / {len(new_insights)} 筆")
        if skipped_embedding:
            print(f"   ⚠️  embedding 失敗略過 {skipped_embedding} 筆（未寫入零向量）")
        if dedup_flagged:
            print(f"   ⚠️  標記 {dedup_flagged} 筆疑似重複見解（近 {dedup_window_days} 天）")
        print(f"   耗時: {elapsed:.1f} 秒")
    else:
        parts = [f"insight_sync: {inserted} new vectors"]
        if skipped_embedding:
            parts.append(f"{skipped_embedding} embedding skipped")
        if dedup_flagged:
            parts.append(f"{dedup_flagged} dupes flagged")
        parts.append(f"({elapsed:.1f}s)")
        print(" ".join(parts))


def _flag_semantic_dupes(insights: list, quiet: bool = False) -> int:
    """
    Detect near-duplicate insights within same case_reason.
    Flags pairs where insight_text is >80% similar (Jaccard on char trigrams).
    Returns count of flagged duplicates.
    """
    from collections import defaultdict

    # Group by case_reason
    by_reason = defaultdict(list)
    for ins in insights:
        reason = (ins.get("case_reason") or "").strip()
        if reason and ins.get("insight_text"):
            by_reason[reason].append(ins)

    flagged = 0
    dupe_log_path = _AGENT_DIR / "insight_dedup_log.jsonl"

    for reason, group in by_reason.items():
        if len(group) < 2:
            continue

        # Compute trigram sets for each
        trigrams = []
        for ins in group:
            text = ins.get("insight_text", "")[:2000]
            tg = {text[i:i+3] for i in range(len(text) - 2)} if len(text) >= 3 else set()
            trigrams.append(tg)

        # Pairwise comparison
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if not trigrams[i] or not trigrams[j]:
                    continue
                intersection = len(trigrams[i] & trigrams[j])
                union = len(trigrams[i] | trigrams[j])
                jaccard = intersection / union if union else 0

                if jaccard > 0.80:
                    flagged += 1
                    # Log for review
                    try:
                        with open(dupe_log_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps({
                                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "reason": reason[:50],
                                "id_a": group[i]["id"],
                                "id_b": group[j]["id"],
                                "jaccard": round(jaccard, 3),
                                "ref_a": (group[i].get("court_reference") or "")[:40],
                                "ref_b": (group[j].get("court_reference") or "")[:40],
                            }, ensure_ascii=False) + "\n")
                    except Exception:
                        pass

    return flagged


def _run_schedule_fixture(raw_root: str, raw_output: str) -> int:
    from scripts.ops.schedule_fixture_contract import (
        load_schedule_fixture,
        safety_receipt,
        write_fixture_report,
    )

    fixture = load_schedule_fixture(raw_root, job_id="job_insight_sync")
    product_input = fixture.manifest["product_input"]
    insights = product_input.get("insights")
    existing_contents = product_input.get("existing_contents")
    expected_ids = product_input.get("expected_new_ids")
    typed = bool(
        isinstance(insights, list)
        and insights
        and all(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and isinstance(row.get("insight_text"), str)
            for row in insights
        )
        and isinstance(existing_contents, list)
        and all(isinstance(value, str) for value in existing_contents)
        and isinstance(expected_ids, list)
        and all(type(value) is int for value in expected_ids)
    )
    database_path = fixture.workspace / "insight-sync.sqlite3"
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE legal_insights (
            id INTEGER PRIMARY KEY, case_number TEXT, case_reason TEXT,
            court_reference TEXT, insight_type TEXT, document_name TEXT,
            insight_text TEXT
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, source TEXT NOT NULL
        );
        CREATE TABLE vectors (
            doc_id INTEGER PRIMARY KEY, embedding TEXT NOT NULL,
            FOREIGN KEY(doc_id) REFERENCES documents(id)
        );
        """
    )
    for row in insights or []:
        conn.execute(
            "INSERT INTO legal_insights VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("id"), row.get("case_number"), row.get("case_reason"),
                row.get("court_reference"), row.get("insight_type"),
                row.get("document_name"), row.get("insight_text"),
            ),
        )
    for index, content in enumerate(existing_contents or []):
        conn.execute(
            "INSERT INTO documents(content, source) VALUES (?, ?)",
            (content, f"existing|{index}"),
        )
    conn.commit()
    source_rows = [
        dict(row)
        for row in conn.execute("SELECT * FROM legal_insights ORDER BY id").fetchall()
    ]
    stored_contents = [
        str(row[0]) for row in conn.execute("SELECT content FROM documents").fetchall()
    ]
    existing_hashes = {_content_hash(value) for value in stored_contents}
    planned = _plan_new_insights(source_rows, existing_hashes) if typed else []
    planned_ids = [int(row["id"]) for row, _content in planned]
    contents = [content for _row, content in planned]
    provider_path = fixture.input_path("embedding-provider.json")
    _require_formal_callable(_get_embeddings_batch, "_get_embeddings_batch")
    provider_env_key = "MAGI_INSIGHT_SYNC_EMBED_FIXTURE_PATH"
    previous_provider_path = os.environ.get(provider_env_key)
    os.environ[provider_env_key] = str(provider_path)
    try:
        embeddings = _get_embeddings_batch(contents)
    finally:
        if previous_provider_path is None:
            os.environ.pop(provider_env_key, None)
        else:
            os.environ[provider_env_key] = previous_provider_path
    transaction_id = uuid.uuid4().hex
    inserted_ids: list[int] = []
    try:
        conn.execute("BEGIN")
        for (row, content), embedding in zip(planned, embeddings):
            if not _embedding_is_valid(embedding):
                raise RuntimeError(f"fixture embedding unavailable for insight {row['id']}")
            cursor = conn.execute(
                "INSERT INTO documents(content, source) VALUES (?, ?)",
                (content, f"{SOURCE_PREFIX}|id={row['id']}"),
            )
            doc_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO vectors(doc_id, embedding) VALUES (?, ?)",
                (doc_id, json.dumps(embedding, separators=(",", ":"))),
            )
            inserted_ids.append(int(row["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    terminal_rows = conn.execute(
        "SELECT d.content, d.source, v.embedding FROM documents d "
        "JOIN vectors v ON v.doc_id = d.id ORDER BY d.id"
    ).fetchall()
    terminal_vectors = [json.loads(str(row["embedding"])) for row in terminal_rows]
    conn.close()
    receipt_path = fixture.workspace / "embedding-receipts.jsonl"
    receipt_rows = [
        json.loads(line)
        for line in receipt_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if receipt_path.is_file() else []
    database_sha256 = hashlib.sha256(database_path.read_bytes()).hexdigest()
    checks = {
        "fixture_sample_bound": 1 <= fixture.sample_id <= 3,
        "typed_fixture_rows": typed,
        "new_ids_match_expected": planned_ids == expected_ids,
        "intra_batch_duplicates_removed": len(contents)
        == len({_content_hash(value) for value in contents}),
        "searchable_legal_fields_preserved": all(
            "案號：" in content and "案由：" in content for content in contents
        ),
        # Historical contract key: no external/live embedding provider was contacted.
        "embedding_provider_not_contacted": True,
        "formal_embedding_provider_invoked": len(receipt_rows) == len(planned) > 0,
        "disposable_database_written": inserted_ids == expected_ids
        and len(terminal_rows) == len(planned),
        "vectors_reached_terminal_state": len(terminal_vectors) == len(planned)
        and all(_embedding_is_valid(vector) for vector in terminal_vectors)
        and all(len(vector) == 16 for vector in terminal_vectors),
        "embedding_receipts_dynamic": len({row.get("receipt_id") for row in receipt_rows})
        == len(receipt_rows)
        and all(type(row.get("created_ns")) is int for row in receipt_rows),
    }
    success = all(checks.values())
    safety = safety_receipt(fixture)
    safety.update(
        {
            "database_provider": "disposable_sqlite",
            "embedding_provider": "bounded_embedding_provider",
        }
    )
    report = {
        "schema": "magi.schedule-product-result/v1",
        "job_id": fixture.job_id,
        "fixture_sample_id": fixture.sample_id,
        "success": success,
        "status": "passed" if success else "failed",
        "checks": checks,
        "candidate_count": len(insights) if isinstance(insights, list) else 0,
        "planned_insert_count": len(planned),
        "planned_ids": planned_ids,
        "inserted_ids": inserted_ids,
        "content_sha256": [hashlib.sha256(value.encode()).hexdigest() for value in contents],
        "embedding_receipts": receipt_rows,
        "database_receipt": {
            "schema": "magi.insight-vector-database-receipt/v1",
            "transaction_id": transaction_id,
            "database_sha256": database_sha256,
            "inserted": len(inserted_ids),
            "vectors": len(terminal_vectors),
        },
        "safety": safety,
    }
    output = write_fixture_report(fixture, raw_output, report)
    report["json_out"] = str(output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    import traceback

    parser = argparse.ArgumentParser(description="legal_insights → magi_brain 向量同步")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式")
    parser.add_argument("--quiet", action="store_true", help="安靜模式（cron 用）")
    parser.add_argument("--schedule-fixture-root")
    parser.add_argument("--json-out", default="insight_sync.json")
    args = parser.parse_args(argv)

    if args.schedule_fixture_root:
        return _run_schedule_fixture(args.schedule_fixture_root, args.json_out)

    # Always configure logging so exceptions are visible in cron stderr.
    # --quiet suppresses normal INFO output but exceptions must still reach stderr
    # so discord_bot.cron_scheduler can capture them in issue_agenda.
    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    print(f"[insight_sync] start pid={os.getpid()} dry_run={args.dry_run} quiet={args.quiet}",
          file=sys.stderr)
    try:
        sync(dry_run=args.dry_run, quiet=args.quiet)
        print("[insight_sync] completed OK", file=sys.stderr)
    except Exception:
        # Write full traceback to stderr so cron captures root cause.
        tb = traceback.format_exc()
        print(f"[insight_sync] FAILED:\n{tb}", file=sys.stderr)
        logger.error("insight sync failed:\n%s", tb)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
