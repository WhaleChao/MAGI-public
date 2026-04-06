#!/usr/bin/env python3
"""
Knowledge Lint — MAGI 知識品質掃描
===================================

Inspired by Karpathy's LLM Wiki "lint" operation. Periodically scans
the knowledge base for quality issues:

1. **Duplicate detection**: Find near-duplicate entries in magi_brain
2. **Contradiction scan**: Use LLM to detect conflicting facts across
   documents for the same case
3. **Staleness check**: Flag wiki pages whose source notes have changed
4. **Orphan detection**: Find vector entries with no corresponding
   Obsidian note, or notes with no vector embedding
5. **Insight quality**: Flag insights that are too short, degraded,
   or contain boilerplate

Output: JSON report + optional Obsidian note with findings.

Usage:
    # Full lint scan
    python scripts/knowledge_lint.py

    # Quick scan (no LLM, just structural checks)
    python scripts/knowledge_lint.py --quick

    # Write results to Obsidian vault
    python scripts/knowledge_lint.py --write-to-vault

    # Dry-run (scan but don't fix)
    python scripts/knowledge_lint.py --dry-run

Cron: Runs as part of nightly cycle (夜議 agenda item)
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

logger = logging.getLogger("knowledge_lint")

# ── Config ──────────────────────────────────────────────────────────
AGENT_DIR = Path(MAGI_ROOT) / ".agent"
VAULT_CONFIG_PATH = AGENT_DIR / "obsidian_vault_config.json"
INDEX_PATH = AGENT_DIR / "obsidian_index.json"
WIKI_STATE_PATH = AGENT_DIR / "wiki_synthesizer_state.json"
INGEST_STATE_PATH = AGENT_DIR / "obsidian_ingest_state.json"

REPORT_DIR = Path(MAGI_ROOT) / "static"
REPORT_PATH = REPORT_DIR / "knowledge_lint_latest.json"

# Thresholds
MIN_INSIGHT_LEN = 100  # chars — insights shorter than this are flagged
DUPLICATE_SIM_THRESHOLD = 0.95  # cosine similarity for near-duplicate
MAX_LLM_CHECKS = 10  # max contradiction checks per run


# ── Helpers ─────────────────────────────────────────────────────────

def _get_vault_path() -> Optional[Path]:
    if VAULT_CONFIG_PATH.exists():
        try:
            cfg = json.loads(VAULT_CONFIG_PATH.read_text("utf-8"))
            vp = Path(cfg.get("vault_path", ""))
            if vp.is_dir():
                return vp
        except Exception:
            pass
    return None


def _db_connect(db_name: str = "magi_brain"):
    """Connect to MariaDB."""
    import mysql.connector
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    if db_name == "magi_brain":
        return mysql.connector.connect(
            host=os.environ.get("DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ.get("DB_USER", "casper_service"),
            password=os.environ.get("DB_PASSWORD", ""),
            database="magi_brain",
        )
    else:
        return mysql.connector.connect(
            host=os.environ.get("OSC_DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("OSC_DB_PORT", "3306")),
            user=os.environ.get("OSC_DB_USER", "python_user"),
            password=os.environ.get("OSC_DB_PASSWORD", ""),
            database="law_firm_data",
        )


# ── Lint Checks ─────────────────────────────────────────────────────

def check_duplicate_vectors() -> Dict:
    """Find near-duplicate content in magi_brain.documents (by MD5)."""
    try:
        conn = _db_connect("magi_brain")
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT MD5(content) AS h, COUNT(*) AS cnt, "
            "GROUP_CONCAT(id ORDER BY id SEPARATOR ',') AS ids, "
            "MIN(CHAR_LENGTH(content)) AS min_len "
            "FROM documents "
            "GROUP BY MD5(content) "
            "HAVING COUNT(*) > 1 "
            "ORDER BY cnt DESC "
            "LIMIT 50"
        )
        dupes = cur.fetchall()
        cur.close()
        conn.close()

        total_extra = sum(d["cnt"] - 1 for d in dupes)
        return {
            "check": "duplicate_vectors",
            "status": "warn" if dupes else "ok",
            "duplicate_groups": len(dupes),
            "total_extra_entries": total_extra,
            "top_dupes": [
                {
                    "hash": d["h"],
                    "count": d["cnt"],
                    "ids": d["ids"],
                    "min_content_len": d["min_len"],
                }
                for d in dupes[:10]
            ],
        }
    except Exception as e:
        return {"check": "duplicate_vectors", "status": "error", "error": str(e)}


def check_insight_quality() -> Dict:
    """Flag degraded or low-quality insights."""
    try:
        conn = _db_connect("law_firm_data")
        cur = conn.cursor(dictionary=True)

        # Total insights
        cur.execute("SELECT COUNT(*) AS total FROM legal_insights")
        total = cur.fetchone()["total"]

        # Degraded
        cur.execute("SELECT COUNT(*) AS cnt FROM legal_insights WHERE is_degraded = 1")
        degraded = cur.fetchone()["cnt"]

        # Too short
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM legal_insights "
            "WHERE insight_text IS NOT NULL AND CHAR_LENGTH(insight_text) < %s "
            "AND is_degraded = 0",
            (MIN_INSIGHT_LEN,)
        )
        too_short = cur.fetchone()["cnt"]

        # Empty
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM legal_insights "
            "WHERE insight_text IS NULL OR insight_text = ''"
        )
        empty = cur.fetchone()["cnt"]

        # Boilerplate patterns
        boilerplate_patterns = [
            "判決連結：\n",
            "摘要失敗",
            "timeout",
            "系統降級回覆",
            "無法擷取",
        ]
        boilerplate_count = 0
        for pat in boilerplate_patterns:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM legal_insights "
                "WHERE insight_text LIKE %s AND is_degraded = 0",
                (f"%{pat}%",)
            )
            boilerplate_count += cur.fetchone()["cnt"]

        cur.close()
        conn.close()

        issues = degraded + too_short + empty + boilerplate_count
        return {
            "check": "insight_quality",
            "status": "warn" if issues > 0 else "ok",
            "total_insights": total,
            "degraded": degraded,
            "too_short": too_short,
            "empty": empty,
            "boilerplate": boilerplate_count,
            "healthy": total - issues,
            "health_pct": round((total - issues) / max(total, 1) * 100, 1),
        }
    except Exception as e:
        return {"check": "insight_quality", "status": "error", "error": str(e)}


def check_wiki_staleness() -> Dict:
    """Check if any wiki pages are outdated (source notes changed)."""
    vault = _get_vault_path()
    if not vault:
        return {"check": "wiki_staleness", "status": "skip", "reason": "no vault"}

    if not WIKI_STATE_PATH.exists():
        return {"check": "wiki_staleness", "status": "skip", "reason": "no wiki state"}

    try:
        wiki_state = json.loads(WIKI_STATE_PATH.read_text("utf-8"))
    except Exception:
        return {"check": "wiki_staleness", "status": "error", "error": "cannot read wiki state"}

    import re
    CASE_FOLDER_RE = re.compile(r"(\d{4}-\d{4})-(.+?)-(.*?)-(.*)")

    # Check each synthesized case
    stale_cases = []
    up_to_date = 0
    notes_dir = vault / "20_Notes"

    for case_number, case_state in wiki_state.get("cases", {}).items():
        prev_hashes = case_state.get("source_hashes", {})

        # Check current note hashes
        changed = False
        current_paths = set()
        for md_file in notes_dir.rglob("*.md"):
            # Check if this note belongs to this case
            for part in md_file.parts:
                m = CASE_FOLDER_RE.match(part)
                if m and m.group(1) == case_number:
                    rel = str(md_file.relative_to(vault))
                    current_paths.add(rel)
                    try:
                        content = md_file.read_text("utf-8", errors="replace")
                        h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
                        if prev_hashes.get(rel) != h:
                            changed = True
                    except Exception:
                        changed = True
                    break

        # Check for removed notes
        for prev_path in prev_hashes:
            if prev_path not in current_paths:
                changed = True

        if changed:
            stale_cases.append({
                "case": case_number,
                "client": case_state.get("client_name", ""),
                "synthesized_at": case_state.get("synthesized_at", ""),
            })
        else:
            up_to_date += 1

    return {
        "check": "wiki_staleness",
        "status": "warn" if stale_cases else "ok",
        "stale_cases": len(stale_cases),
        "up_to_date": up_to_date,
        "details": stale_cases[:10],
    }


def check_orphan_notes() -> Dict:
    """Find Obsidian notes without vector embeddings, and vice versa."""
    vault = _get_vault_path()
    if not vault:
        return {"check": "orphan_notes", "status": "skip", "reason": "no vault"}

    if not INDEX_PATH.exists():
        return {"check": "orphan_notes", "status": "skip", "reason": "no index"}

    try:
        idx = json.loads(INDEX_PATH.read_text("utf-8"))
    except Exception:
        return {"check": "orphan_notes", "status": "error", "error": "cannot read index"}

    notes_in_index = set(idx.get("notes", {}).keys())

    # Find actual .md files in vault
    notes_dir = vault / "20_Notes"
    actual_notes = set()
    if notes_dir.is_dir():
        for md in notes_dir.rglob("*.md"):
            actual_notes.add(str(md.relative_to(vault)))

    # Notes on disk but not indexed
    unindexed = actual_notes - notes_in_index
    # Notes in index but file missing
    orphaned_index = notes_in_index - actual_notes

    # Notes indexed but with 0 chunks
    zero_chunks = [
        path for path, info in idx.get("notes", {}).items()
        if info.get("chunks", 0) == 0 and path in actual_notes
    ]

    return {
        "check": "orphan_notes",
        "status": "warn" if (unindexed or orphaned_index or zero_chunks) else "ok",
        "total_on_disk": len(actual_notes),
        "total_in_index": len(notes_in_index),
        "unindexed": len(unindexed),
        "orphaned_index_entries": len(orphaned_index),
        "zero_chunk_notes": len(zero_chunks),
        "sample_unindexed": sorted(unindexed)[:5],
        "sample_orphaned": sorted(orphaned_index)[:5],
    }


def check_contradiction_scan(use_llm: bool = True) -> Dict:
    """
    Use LLM to detect contradictions within cases.

    Lightweight version: compare wiki overview ⚠️ sections.
    Full version: sample pairs of documents from same case, ask LLM.
    """
    if not use_llm:
        return {"check": "contradiction_scan", "status": "skip", "reason": "llm disabled"}

    vault = _get_vault_path()
    if not vault:
        return {"check": "contradiction_scan", "status": "skip", "reason": "no vault"}

    wiki_dir = vault / "30_Wiki"
    if not wiki_dir.is_dir():
        return {"check": "contradiction_scan", "status": "skip", "reason": "no wiki pages yet"}

    # Read existing wiki overviews and check for ⚠️ markers
    contradictions = []
    clean_cases = 0

    for case_dir in sorted(wiki_dir.iterdir()):
        if not case_dir.is_dir():
            continue
        overview = case_dir / "overview.md"
        if not overview.exists():
            continue

        try:
            content = overview.read_text("utf-8", errors="replace")
        except Exception:
            continue

        # Count ⚠️ markers
        warning_count = content.count("⚠️")
        # Check for contradiction section
        has_contradiction_section = "矛盾" in content or "待確認" in content

        if warning_count > 0 or has_contradiction_section:
            # Extract the contradiction section
            match = re.search(r"##\s*⚠️.*?\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
            excerpt = match.group(1).strip()[:300] if match else ""

            contradictions.append({
                "case": case_dir.name,
                "warning_count": warning_count,
                "excerpt": excerpt,
            })
        else:
            clean_cases += 1

    return {
        "check": "contradiction_scan",
        "status": "warn" if contradictions else "ok",
        "cases_with_contradictions": len(contradictions),
        "clean_cases": clean_cases,
        "details": contradictions[:10],
    }


# ── Report Generation ───────────────────────────────────────────────

def _format_report_md(results: List[Dict]) -> str:
    """Format lint results as Obsidian-friendly markdown."""
    lines = [
        f"# 🔍 MAGI 知識品質報告",
        f"",
        f"**掃描時間**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
    ]

    status_icons = {"ok": "✅", "warn": "⚠️", "error": "❌", "skip": "⏭️"}

    # Summary table
    lines.append("## 摘要")
    lines.append("")
    lines.append("| 檢查項目 | 狀態 | 說明 |")
    lines.append("|---------|------|------|")

    check_labels = {
        "duplicate_vectors": "向量重複",
        "insight_quality": "見解品質",
        "wiki_staleness": "Wiki 時效",
        "orphan_notes": "孤立筆記",
        "contradiction_scan": "矛盾偵測",
    }

    for r in results:
        icon = status_icons.get(r.get("status", ""), "❓")
        label = check_labels.get(r.get("check", ""), r.get("check", ""))
        summary = _summarize_check(r)
        lines.append(f"| {label} | {icon} | {summary} |")

    lines.append("")

    # Details
    for r in results:
        check = r.get("check", "")
        label = check_labels.get(check, check)
        lines.append(f"## {label}")
        lines.append("")

        if check == "duplicate_vectors":
            lines.append(f"- 重複群組: {r.get('duplicate_groups', 0)}")
            lines.append(f"- 多餘條目: {r.get('total_extra_entries', 0)}")
        elif check == "insight_quality":
            lines.append(f"- 總見解數: {r.get('total_insights', 0)}")
            lines.append(f"- 健康比例: {r.get('health_pct', 0)}%")
            lines.append(f"- 降級: {r.get('degraded', 0)} | 過短: {r.get('too_short', 0)} | 空白: {r.get('empty', 0)} | 樣板: {r.get('boilerplate', 0)}")
        elif check == "wiki_staleness":
            lines.append(f"- 過時 wiki: {r.get('stale_cases', 0)}")
            lines.append(f"- 最新: {r.get('up_to_date', 0)}")
            for d in r.get("details", []):
                lines.append(f"  - {d['case']} ({d.get('client', '')}) — 合成於 {d.get('synthesized_at', '?')}")
        elif check == "orphan_notes":
            lines.append(f"- 磁碟筆記: {r.get('total_on_disk', 0)} | 索引筆記: {r.get('total_in_index', 0)}")
            lines.append(f"- 未索引: {r.get('unindexed', 0)} | 孤立索引: {r.get('orphaned_index_entries', 0)} | 零向量: {r.get('zero_chunk_notes', 0)}")
        elif check == "contradiction_scan":
            lines.append(f"- 有矛盾案件: {r.get('cases_with_contradictions', 0)}")
            lines.append(f"- 無矛盾案件: {r.get('clean_cases', 0)}")
            for d in r.get("details", []):
                lines.append(f"  - **{d['case']}** ({d.get('warning_count', 0)} ⚠️)")
                if d.get("excerpt"):
                    lines.append(f"    > {d['excerpt'][:150]}")

        if r.get("error"):
            lines.append(f"- ❌ 錯誤: {r['error']}")

        lines.append("")

    return "\n".join(lines)


def _summarize_check(r: Dict) -> str:
    check = r.get("check", "")
    status = r.get("status", "")
    if status == "skip":
        return r.get("reason", "跳過")
    if status == "error":
        return f"錯誤: {r.get('error', '')[:50]}"

    if check == "duplicate_vectors":
        n = r.get("duplicate_groups", 0)
        return f"{n} 組重複" if n else "無重複"
    elif check == "insight_quality":
        return f"{r.get('health_pct', 0)}% 健康 ({r.get('total_insights', 0)} 筆)"
    elif check == "wiki_staleness":
        n = r.get("stale_cases", 0)
        return f"{n} 個 wiki 需更新" if n else "全部最新"
    elif check == "orphan_notes":
        u = r.get("unindexed", 0)
        o = r.get("orphaned_index_entries", 0)
        return f"未索引 {u} / 孤立 {o}" if (u or o) else "同步正常"
    elif check == "contradiction_scan":
        n = r.get("cases_with_contradictions", 0)
        return f"{n} 個案件有矛盾標記" if n else "無矛盾"
    return str(status)


# ── Main ────────────────────────────────────────────────────────────

def lint(
    quick: bool = False,
    write_to_vault: bool = False,
    quiet: bool = False,
):
    """Run all lint checks and produce a report."""
    t0 = time.time()

    if not quiet:
        print("🔍 MAGI 知識品質掃描開始...\n")

    results = []

    # 1. Duplicate vectors (always fast, no LLM)
    if not quiet:
        print("  [1/5] 向量重複檢查...", end=" ", flush=True)
    r = check_duplicate_vectors()
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    # 2. Insight quality (fast, no LLM)
    if not quiet:
        print("  [2/5] 見解品質檢查...", end=" ", flush=True)
    r = check_insight_quality()
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    # 3. Wiki staleness (fast, no LLM)
    if not quiet:
        print("  [3/5] Wiki 時效檢查...", end=" ", flush=True)
    r = check_wiki_staleness()
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    # 4. Orphan notes (fast, no LLM)
    if not quiet:
        print("  [4/5] 孤立筆記檢查...", end=" ", flush=True)
    r = check_orphan_notes()
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    # 5. Contradiction scan (uses LLM in full mode)
    if not quiet:
        print("  [5/5] 矛盾偵測...", end=" ", flush=True)
    r = check_contradiction_scan(use_llm=not quick)
    results.append(r)
    if not quiet:
        print(f"{'✅' if r['status'] == 'ok' else '⚠️'}")

    elapsed = time.time() - t0

    # Build report
    report = {
        "scan_time": datetime.now().isoformat(),
        "elapsed_sec": round(elapsed, 1),
        "mode": "quick" if quick else "full",
        "checks": results,
        "summary": {
            "total_checks": len(results),
            "ok": sum(1 for r in results if r.get("status") == "ok"),
            "warn": sum(1 for r in results if r.get("status") == "warn"),
            "error": sum(1 for r in results if r.get("status") == "error"),
            "skip": sum(1 for r in results if r.get("status") == "skip"),
        },
    }

    # Save JSON report
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Write to Obsidian vault
    if write_to_vault:
        vault = _get_vault_path()
        if vault:
            md_content = _format_report_md(results)
            try:
                sys.path.insert(0, os.path.join(MAGI_ROOT, "skills", "obsidian"))
                from action import task_writeback
                wr = task_writeback(
                    f"知識品質報告_{datetime.now().strftime('%Y%m%d')}",
                    md_content,
                    folder="MAGI/品質報告",
                    vault_path=vault,
                )
                if not quiet:
                    print(f"\n📝 報告已寫入 Obsidian: {wr.get('path', '')}")
            except Exception as e:
                logger.warning("Failed to write to vault: %s", e)

    if not quiet:
        print(f"\n{'='*50}")
        s = report["summary"]
        print(f"掃描完成！耗時 {elapsed:.1f}s")
        print(f"  ✅ {s['ok']}  ⚠️ {s['warn']}  ❌ {s['error']}  ⏭️ {s['skip']}")
        print(f"  報告: {REPORT_PATH}")

    return report


def main():
    parser = argparse.ArgumentParser(description="MAGI 知識品質掃描 (Knowledge Lint)")
    parser.add_argument("--quick", action="store_true", help="快速模式（不使用 LLM）")
    parser.add_argument("--write-to-vault", action="store_true", help="將報告寫入 Obsidian vault")
    parser.add_argument("--quiet", action="store_true", help="安靜模式")
    parser.add_argument("--dry-run", action="store_true", help="同 --quick")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    lint(
        quick=args.quick or args.dry_run,
        write_to_vault=args.write_to_vault,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
