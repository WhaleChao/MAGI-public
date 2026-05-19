#!/usr/bin/env python3
"""
Wiki Synthesizer — Karpathy-inspired pre-synthesis layer for MAGI
=================================================================

Takes ingested Obsidian notes (from ingest_source) and synthesizes
per-case "wiki pages" that merge information from multiple documents
into a coherent, pre-digested summary.

Unlike RAG (which retrieves raw chunks at query time), wiki synthesis
creates a persistent knowledge layer that is:
  - Pre-organized by case
  - Cross-referenced (detects overlaps/contradictions)
  - Incrementally updated (only re-synthesizes when source notes change)
  - Directly queryable (lives in Obsidian vault as .md)

Architecture:
  Source docs → Obsidian notes (existing ingest_source)
                 ↓
  Wiki Synthesizer reads all notes for a case → LLM merges them
                 ↓
  vault/30_Wiki/<case_number>/overview.md   (案件總覽)
  vault/30_Wiki/<case_number>/timeline.md   (事件時間軸)
  vault/30_Wiki/<case_number>/parties.md    (當事人關係)
  vault/30_Wiki/<case_number>/issues.md     (爭點清單)
  vault/30_Wiki/<case_number>/evidence.md   (證據清單)
                 ↓
  Vector memory (so RAG can retrieve pre-synthesized answers)

Usage:
    # Synthesize all cases with changed notes
    python scripts/wiki_synthesizer.py

    # Force re-synthesize a specific case
    python scripts/wiki_synthesizer.py --case 2025-0002 --force

    # Dry-run (preview which cases need updates)
    python scripts/wiki_synthesizer.py --dry-run

    # Limit LLM calls (useful for first run)
    python scripts/wiki_synthesizer.py --limit 5

Cron:  Added to nightly cycle (after ingest_source at 07:00)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

logger = logging.getLogger("wiki_synthesizer")

# ── Config ──────────────────────────────────────────────────────────
AGENT_DIR = Path(MAGI_ROOT) / ".agent"
AGENT_DIR.mkdir(exist_ok=True)

WIKI_STATE_PATH = AGENT_DIR / "wiki_synthesizer_state.json"
VAULT_CONFIG_PATH = AGENT_DIR / "obsidian_vault_config.json"
INGEST_STATE_PATH = AGENT_DIR / "obsidian_ingest_state.json"
INDEX_PATH = AGENT_DIR / "obsidian_index.json"

WIKI_FOLDER = "30_Wiki"  # Inside vault

# Max chars to feed LLM per synthesis call
# E4B on 6GB runs safely at ~3K chars; 26B can handle 8K. Default to 3K to protect E4B.
MAX_SOURCE_CHARS = int(os.environ.get("MAGI_WIKI_MAX_SOURCE_CHARS", "3000"))
# Max notes per case to synthesize in one pass
MAX_NOTES_PER_CASE = int(os.environ.get("MAGI_WIKI_MAX_NOTES", "50"))

CASE_FOLDER_RE = re.compile(r"(\d{4}-\d{4})-(.+?)-(.*?)-(.*)")


# ── State management ────────────────────────────────────────────────

def _load_state() -> Dict:
    if WIKI_STATE_PATH.exists():
        try:
            return json.loads(WIKI_STATE_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"cases": {}, "updated_at": ""}


def _save_state(state: Dict):
    state["updated_at"] = datetime.now().isoformat()
    WIKI_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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


# ── Gather notes per case ───────────────────────────────────────────

def _gather_case_notes(vault: Path) -> Dict[str, List[Dict]]:
    """
    Scan vault/20_Notes/ for notes, group by case_number.

    Returns: {case_number: [{path, case_info, mtime, content_hash}, ...]}
    """
    notes_dir = vault / "20_Notes"
    if not notes_dir.is_dir():
        return {}

    cases: Dict[str, List[Dict]] = {}

    for md_file in notes_dir.rglob("*.md"):
        # Extract case number from path
        case_number = None
        client_name = None
        for part in md_file.parts:
            m = CASE_FOLDER_RE.match(part)
            if m:
                case_number = m.group(1)
                client_name = m.group(2)
                break

        if not case_number:
            continue

        # Read frontmatter for metadata
        try:
            content = md_file.read_text("utf-8", errors="replace")
        except Exception:
            continue

        mtime = int(md_file.stat().st_mtime)
        content_hash = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]

        rel_path = str(md_file.relative_to(vault))

        cases.setdefault(case_number, []).append({
            "path": rel_path,
            "abs_path": str(md_file),
            "case_number": case_number,
            "client_name": client_name or "",
            "mtime": mtime,
            "content_hash": content_hash,
            "size": len(content),
        })

    # Sort notes within each case by path for deterministic ordering
    for cn in cases:
        cases[cn].sort(key=lambda n: n["path"])

    return cases


def _case_needs_update(case_number: str, notes: List[Dict], state: Dict) -> bool:
    """Check if any note in this case has changed since last synthesis,
    or if the previous synthesis used a retryable structural fallback.
    """
    prev = state.get("cases", {}).get(case_number, {})
    prev_hashes = prev.get("source_hashes", {})

    # Re-synthesize retryable fallbacks, such as multi-document cases where
    # an LLM merge is materially better.  Single-note cases can safely keep
    # the structural overview without becoming nightly churn.
    if prev and not prev.get("llm_synthesized", True) and prev.get("fallback_retryable", True):
        return True

    for note in notes:
        prev_hash = prev_hashes.get(note["path"])
        if prev_hash != note["content_hash"]:
            return True

    # Also check if notes were removed
    current_paths = {n["path"] for n in notes}
    for prev_path in prev_hashes:
        if prev_path not in current_paths:
            return True

    return False


# ── LLM Synthesis ───────────────────────────────────────────────────

def _get_gateway():
    """Get InferenceGateway instance."""
    from skills.bridge.inference_gateway import InferenceGateway
    return InferenceGateway()


def _omlx_chat_direct(prompt: str, timeout: int = 60, max_tokens: int = 500) -> Optional[str]:
    """Call oMLX directly with controlled max_tokens (bypasses gateway's hardcoded 2048)."""
    try:
        import requests as _req
        url = "http://127.0.0.1:8080/v1/chat/completions"
        payload = {
            "model": os.environ.get("MAGI_WIKI_LLM_MODEL", "gemma-4-e4b-it-4bit"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "stream": False,
        }
        r = _req.post(url, json=payload, timeout=(2.0, float(timeout)))
        if r.status_code == 200:
            data = r.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            return text if text else None
    except Exception as e:
        logger.debug("_omlx_chat_direct failed: %s", e)
    return None


def _extract_note_text(abs_path: str, max_chars: int = 5000) -> str:
    """Extract the Full Text section from an Obsidian note."""
    try:
        content = Path(abs_path).read_text("utf-8", errors="replace")
    except Exception:
        return ""

    # Try to find ## Full Text section
    match = re.search(r"## Full Text\s*\n(.+)", content, re.DOTALL)
    if match:
        text = match.group(1).strip()
    else:
        # Fall back to content after frontmatter
        text = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL).strip()

    # Truncate
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[截斷]"
    return text


def _synthesize_overview(
    case_number: str,
    client_name: str,
    notes: List[Dict],
    gw,
) -> Optional[str]:
    """Use LLM to synthesize a case overview from all notes.

    Set MAGI_WIKI_SKIP_LLM=1 to bypass LLM entirely (fast structural-only run).
    """
    if os.environ.get("MAGI_WIKI_SKIP_LLM", "0") == "1":
        return None  # caller will use _structural_overview

    # Collect source texts, staying within budget
    source_parts = []
    total_chars = 0

    # Prioritize: 判決書 > 書狀 > 筆錄 > 證據 > others
    priority_keywords = ["判決", "裁定", "起訴", "書狀", "答辯", "筆錄", "證據", "信件"]

    def note_priority(n):
        path_lower = n["path"].lower()
        for i, kw in enumerate(priority_keywords):
            if kw in path_lower:
                return i
        return len(priority_keywords)

    sorted_notes = sorted(notes, key=note_priority)

    # Adapt per-note limit based on number of notes
    per_note_chars = min(3000, max(800, MAX_SOURCE_CHARS // min(len(sorted_notes), MAX_NOTES_PER_CASE)))

    for note in sorted_notes[:MAX_NOTES_PER_CASE]:
        text = _extract_note_text(note["abs_path"], max_chars=per_note_chars)
        if not text or len(text) < 50:
            continue
        if total_chars + len(text) > MAX_SOURCE_CHARS:
            break

        # Use filename as document label
        doc_name = Path(note["path"]).stem.replace("summary__", "")
        source_parts.append(f"### 文件：{doc_name}\n{text}")
        total_chars += len(text)

    if not source_parts:
        return None

    prompt = f"""你是一位資深法律助理。以下是案件 {case_number}（當事人：{client_name}）的多份文件摘錄。
請根據這些文件，產生一份結構化的案件知識總覽。

要求：
1. 用繁體中文
2. 包含以下章節（若資料不足可標註「未見資料」）：
   ## 案件概要
   - 當事人、案由、法院、進度

   ## 事件時間軸
   - 以時間順序列出關鍵事件（日期 — 事件）

   ## 當事人與關係
   - 列出所有當事人及其角色/關係

   ## 爭點
   - 列出案件主要爭點

   ## 證據清單
   - 列出已掌握的證據（含來源文件）

   ## 矛盾與待補事項
   - 只有在不同文件描述同一事實且內容互相衝突時，才使用「⚠️ 矛盾：」並列出兩個版本
   - 單純缺少資料時，請列在「待補事項」，不要使用 ⚠️
   - 若沒有矛盾也沒有待補事項，寫「未發現」

3. 每項資訊後面用 `→ 來源：文件名` 標註出處
4. 不要輸出提示詞、方括號樣板、或「請提供文件內容」等對話語句

以下是文件內容：

{chr(10).join(source_parts)}
"""

    # Bound output tokens so E4B under memory pressure can't run for hours.
    # Use direct oMLX call (bypasses gateway's hardcoded max_tokens=2048).
    max_tokens = int(os.environ.get("MAGI_WIKI_MAX_TOKENS", "500"))
    timeout_sec = int(os.environ.get("MAGI_WIKI_LLM_TIMEOUT", "90"))

    resp = _omlx_chat_direct(prompt, timeout=timeout_sec, max_tokens=max_tokens)
    if resp and len(resp) >= 100:
        degraded_markers = ["系統降級", "逾時", "請稍後重試", "無法處理"]
        if not any(m in resp for m in degraded_markers):
            return resp
        logger.warning("LLM returned degraded response for %s", case_number)
        return None

    # oMLX direct failed or too short — fall back to gateway (Ollama etc.)
    if resp is not None:
        logger.warning("LLM response too short for %s (%d chars)", case_number, len(resp))
    try:
        result = gw.chat(prompt, task_type="general", timeout=30)
        if result.get("success"):
            gw_resp = result.get("response", "").strip()
            if gw_resp and len(gw_resp) >= 100:
                degraded_markers = ["系統降級", "逾時", "請稍後重試", "無法處理"]
                if not any(m in gw_resp for m in degraded_markers):
                    return gw_resp
        logger.warning("LLM synthesis failed for %s: %s", case_number, result.get("error"))
        return None
    except Exception as e:
        logger.warning("LLM synthesis error for %s: %s", case_number, e)
        return None


# ── Wikilink helpers ────────────────────────────────────────────────

def _note_path_to_wikilink(note_path: str) -> str:
    """Convert a relative note path like 'folder/My Note.md' to '[[My Note]]'."""
    name = Path(note_path).stem
    return f"[[{name}]]"


def _build_source_wikilinks_section(notes: List[Dict]) -> str:
    """Build a '## 相關文件' section with wikilinks to source notes."""
    links = []
    seen = set()
    for note in notes:
        wikilink = _note_path_to_wikilink(note["path"])
        if wikilink not in seen:
            seen.add(wikilink)
            links.append(f"- {wikilink}  ")
    if not links:
        return ""
    return "## 相關文件\n\n" + "\n".join(links) + "\n"


def _inject_source_wikilinks(content: str, notes: List[Dict]) -> str:
    """
    Post-process LLM content to convert '→ 來源：文件名' refs to wikilinks.
    Matches patterns like:
      → 來源：115年度偵聲字第10號刑事裁定
      → 來源：刑事答辯狀(陳明宗)
    And converts them to wikilinks based on the actual note stems.
    """
    # Build a map: doc_name (without extension, without summary__ prefix) → wikilink
    doc_map: Dict[str, str] = {}
    for note in notes:
        stem = Path(note["path"]).stem
        clean = stem.replace("summary__", "")
        doc_map[clean] = f"[[{stem}]]"

    def replace_source(m: "re.Match") -> str:
        raw_name = m.group(1).strip()
        # Try exact match first
        if raw_name in doc_map:
            return f"→ 來源：{doc_map[raw_name]}"
        # Try partial match (source name is a substring of doc key)
        for clean_key, wlink in doc_map.items():
            if raw_name in clean_key or clean_key in raw_name:
                return f"→ 來源：{wlink}"
        # No match — keep as plain text
        return m.group(0)

    # Match: → 來源：<name>  (until newline or next →)
    pattern = re.compile(r"→ 來源：([^\n→]+)")
    return pattern.sub(replace_source, content)


# ── Write wiki pages ────────────────────────────────────────────────

def _write_wiki_page(
    vault: Path,
    case_number: str,
    client_name: str,
    content: str,
    page_name: str = "overview",
    notes: Optional[List[Dict]] = None,
) -> Path:
    """Write a wiki page to vault/30_Wiki/<case>/<page>.md with wikilinks."""
    wiki_dir = vault / WIKI_FOLDER / f"{case_number}-{client_name}"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().isoformat()
    frontmatter = (
        f"---\n"
        f"source: MAGI-wiki-synthesizer\n"
        f"case_number: {case_number}\n"
        f"client_name: {client_name}\n"
        f"page: {page_name}\n"
        f"synthesized_at: {now}\n"
        f"tags: [magi-wiki, auto-synthesized]\n"
        f"---\n\n"
    )

    # Navigation section with wikilinks — appears before main content
    nav_section = (
        f"## 導覽\n\n"
        f"**索引**：[[案件]] | [[30_Wiki]]\n"
        f"**當事人**：[[{client_name}]]\n"
        f"**案件**：{case_number}\n\n"
        f"---\n\n"
    )

    # Inject wikilinks into source references in the LLM content
    if notes:
        content = _inject_source_wikilinks(content, notes)

    # Build footer with all source notes as wikilinks
    footer = ""
    if notes:
        footer = "\n\n---\n\n" + _build_source_wikilinks_section(notes)

    target = wiki_dir / f"{page_name}.md"
    full_content = frontmatter + nav_section + content.strip() + footer + "\n"
    target.write_text(full_content, encoding="utf-8")
    return target


def _structural_overview(
    case_number: str,
    client_name: str,
    notes: List[Dict],
) -> str:
    """
    Structural fallback: generate a wiki overview purely from note metadata.
    No LLM required. Produces a usable wiki page with wikilinks even when
    LLM is unavailable. Nightly cron will replace with LLM content later.
    """
    lines = [
        f"> ℹ️ 此為結構式自動概要（LLM 暫時無法使用）。待夜間 cron 03:30 補齊 LLM 合成版。\n",
        "",
        "## 案件概要",
        "",
        f"- **當事人**：{client_name}",
        f"- **案件號**：{case_number}",
        f"- **筆記數**：{len(notes)} 份",
        "",
        "## 文件清單",
        "",
    ]

    # Group notes by subfolder (inferred document category)
    category_map: Dict[str, List[str]] = {}
    for note in sorted(notes, key=lambda n: n["path"]):
        stem = Path(note["path"]).stem
        clean = stem.replace("summary__", "")
        # Infer category from path parts
        parts = Path(note["path"]).parts
        # Find the most specific named part after case folder
        category = "其他"
        for p in parts:
            if re.match(r"\d{2}_", p):
                category = p
                break
        category_map.setdefault(category, []).append(f"[[{stem}]]")

    for cat, wlinks in sorted(category_map.items()):
        lines.append(f"### {cat}")
        lines.extend(f"- {w}" for w in wlinks)
        lines.append("")

    lines += [
        "## 爭點",
        "",
        "> ⏳ 待 LLM 合成後補充",
        "",
        "## 事件時間軸",
        "",
        "> ⏳ 待 LLM 合成後補充",
        "",
    ]

    return "\n".join(lines)


def _ingest_wiki_to_vectors(
    vault: Path,
    case_number: str,
    client_name: str,
    content: str,
    page_name: str = "overview",
) -> Dict:
    """Ingest synthesized wiki page into vector memory."""
    try:
        from skills.memory.vector_pipeline import ingest_text_to_vector_memory
    except ImportError:
        logger.warning("vector_pipeline not available; skipping vector ingest")
        return {"success": False, "error": "vector_pipeline not available"}

    vault_name = vault.name
    note_rel = f"{WIKI_FOLDER}/{case_number}-{client_name}/{page_name}.md"
    source_meta = (
        f"obsidian|vault={vault_name}"
        f"|source_root=wiki"
        f"|case={case_number}"
        f"|note={note_rel}"
        f"|wiki=1"
    )

    try:
        vr = ingest_text_to_vector_memory(
            kind="obsidian",
            primary=source_meta,
            title=f"Wiki: {case_number} {client_name} — {page_name}",
            text=content,
            chunk_chars=1200,
            overlap=120,
            max_chunks_total=30,
        )
        return vr
    except Exception as e:
        logger.warning("Vector ingest failed for %s/%s: %s", case_number, page_name, e)
        return {"success": False, "error": str(e)}


# ── Main synthesis loop ─────────────────────────────────────────────

def synthesize(
    target_case: str = "",
    force: bool = False,
    dry_run: bool = False,
    limit: int = 0,
    quiet: bool = False,
    skip_ingest: bool = False,
):
    """Main entry: scan cases, synthesize wiki pages for changed ones."""
    t0 = time.time()

    vault = _get_vault_path()
    if not vault:
        logger.error("Obsidian vault 未設定")
        print("❌ Obsidian vault 未設定。請先執行 obsidian --task set_vault")
        return

    state = _load_state()

    # Gather all case notes
    all_cases = _gather_case_notes(vault)
    if not all_cases:
        if not quiet:
            print("ℹ️  vault 中沒有案件筆記")
        return

    if not quiet:
        print(f"📚 掃描到 {len(all_cases)} 個案件，共 {sum(len(v) for v in all_cases.values())} 份筆記")

    # Filter to target case if specified
    if target_case:
        if target_case in all_cases:
            all_cases = {target_case: all_cases[target_case]}
        else:
            print(f"❌ 找不到案件 {target_case}")
            return

    # Find cases needing update
    cases_to_update = []
    for case_number, notes in all_cases.items():
        if force or _case_needs_update(case_number, notes, state):
            cases_to_update.append((case_number, notes))

    if not cases_to_update:
        if not quiet:
            print("✅ 所有案件 wiki 已是最新")
        return

    if not quiet:
        print(f"📝 {len(cases_to_update)} 個案件需要更新 wiki")

    if dry_run:
        for case_number, notes in cases_to_update:
            client = notes[0].get("client_name", "?") if notes else "?"
            print(f"  → {case_number} ({client}) — {len(notes)} 份筆記")
        return

    # Synthesize
    if limit:
        cases_to_update = cases_to_update[:limit]

    gw = _get_gateway()
    synthesized = 0
    errors = 0

    for i, (case_number, notes) in enumerate(cases_to_update, 1):
        client_name = notes[0].get("client_name", "") if notes else ""
        if not quiet:
            print(f"\n[{i}/{len(cases_to_update)}] {case_number} ({client_name}) — {len(notes)} 份筆記")

        # Synthesize overview (LLM path)
        overview = _synthesize_overview(case_number, client_name, notes, gw)

        # Structural fallback: always produce a wiki page even if LLM fails
        used_fallback = False
        if not overview:
            overview = _structural_overview(case_number, client_name, notes)
            used_fallback = True
            if not quiet:
                if len(notes) <= 1:
                    print("  📋 單一來源，使用結構式總覽")
                else:
                    print("  ⚠️  LLM 失敗，使用結構式 fallback（夜間 cron 補齊）")

        # Write wiki page (with wikilinks injected)
        wiki_path = _write_wiki_page(vault, case_number, client_name, overview, "overview", notes=notes)
        if not quiet:
            label = "📋 結構式" if used_fallback else "✅ LLM"
            print(f"  {label} 寫入 {wiki_path.relative_to(vault)}")

        # Ingest to vectors (skip if --no-ingest; Phase 4 ingest handles it separately)
        if skip_ingest:
            chunks = 0
        else:
            vr = _ingest_wiki_to_vectors(vault, case_number, client_name, overview, "overview")
            chunks = vr.get("chunks_written", 0) if vr.get("success") else 0
            if not quiet:
                print(f"  📊 向量化 {chunks} chunks")

        # Update state
        state.setdefault("cases", {})[case_number] = {
            "client_name": client_name,
            "source_hashes": {n["path"]: n["content_hash"] for n in notes},
            "note_count": len(notes),
            "wiki_page": str(wiki_path.relative_to(vault)),
            "synthesized_at": datetime.now().isoformat(),
            "vector_chunks": chunks,
            "llm_synthesized": not used_fallback,
            "fallback_retryable": bool(used_fallback and len(notes) > 1),
        }
        _save_state(state)
        synthesized += 1

    elapsed = time.time() - t0
    if not quiet:
        print(f"\n{'='*50}")
        print(f"✅ Wiki 合成完成！耗時 {elapsed:.1f}s")
        print(f"   合成: {synthesized}  錯誤: {errors}  跳過: {len(all_cases) - len(cases_to_update)}")


def main():
    parser = argparse.ArgumentParser(description="MAGI Wiki Synthesizer — 案件知識預合成")
    parser.add_argument("--case", type=str, default="", help="只合成指定案件（e.g. 2025-0002）")
    parser.add_argument("--force", action="store_true", help="強制重新合成（忽略快取）")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式")
    parser.add_argument("--limit", type=int, default=0, help="最多合成幾個案件（0=不限）")
    parser.add_argument("--quiet", action="store_true", help="安靜模式（cron 用）")
    parser.add_argument("--no-ingest", action="store_true", help="跳過向量化（Phase 4 另外跑）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    synthesize(
        target_case=args.case,
        force=args.force,
        dry_run=args.dry_run,
        limit=args.limit,
        quiet=args.quiet,
        skip_ingest=getattr(args, "no_ingest", False),
    )


if __name__ == "__main__":
    main()
