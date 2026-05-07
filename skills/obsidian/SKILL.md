---
name: obsidian
description: Obsidian vault integration - read, search, ingest notes, selective source extraction into MAGI vector memory
license: MIT
compatibility: requires obsidian-cli (brew install yakitrak/yakitrak/obsidian-cli) or direct file access
metadata:
  author: MAGI
  version: "2.0"
  sage: keeper
---

# Obsidian Integration (Phase 2)

Connect MAGI to an Obsidian vault for notebook-style Q&A with citations.
Supports selective ingestion from Synology source roots with PDF/DOCX extraction.

## Capabilities

- Discover and configure Obsidian vaults
- Search notes by name or content
- Read individual notes
- Ingest notes into MAGI vector memory (with dedup)
- **Phase 2:** Selective source ingestion from Synology (案件, fang, 結案, 舊案)
- **Phase 2:** PDF/DOCX/TXT/MD text extraction with pdfplumber/PyMuPDF/PyPDF2
- **Phase 2:** Generated markdown notes with YAML frontmatter under `20_Notes/`
- **Phase 2:** Enhanced source metadata for NotebookLM-style citations
- Scoped retrieval: source root, folder, case number, vault, or tag

## Usage

```bash
# Bootstrap a local vault with Synology/MariaDB-backed source links
python3 bootstrap_synology_vault.py

# Check status
python3 action.py --task status

# Selective ingest from a source root
python3 action.py --task ingest_source --source 案件 --subpath "法扶案件/刑事/2025-0014..." --limit 20
python3 action.py --task ingest_source --source fang --subpath "函授/陳楓公司法" --limit 10

# Ingest vault notes into vector memory
python3 action.py --task ingest --folder "20_Notes"

# Search notes
python3 action.py --task search --query "過失傷害"

# Ask with source scoping
python3 action.py --task ask --query "案件概要" --scope "source:案件"
python3 action.py --task ask --query "公司法重點" --scope "source:fang"
python3 action.py --task ask --query "陳紫箖" --scope "case:2025-0014"
```

## Commands (for orchestrator routing)

- `obsidian status` - Show vault config and index stats
- `obsidian search <query>` - Search note names and content
- `obsidian read <note>` - Read a specific note
- `obsidian ingest [folder]` - Ingest vault notes into vector memory
- `obsidian ingest_source --source X [--subpath Y] [--limit N]` - Extract & ingest from Synology
- `obsidian ask <question> [--scope source:X|case:Y]` - Q&A with citations
- `obsidian set_vault <path>` - Configure vault path

## Source Roots

| Name | Path | Priority |
|------|------|----------|
| 案件 | ~/Library/CloudStorage/SynologyDrive-homes/01_案件 | 1 (highest) |
| fang | /Volumes/lumi/fang | 2 |
| 結案 | /Volumes/lumi/lumi/03_工作資料/10_結案 | 3 |
| 舊案 | /Volumes/lumi/lumi/03_工作資料/10_結案/舊案 | 4 |

## Files

- `SKILL.md` - This file
- `action.py` - CLI entry point and task implementations
- `extractors.py` - Text extraction helpers (PDF, DOCX, TXT, MD)
- `bootstrap_synology_vault.py` - Vault creation and source linking
