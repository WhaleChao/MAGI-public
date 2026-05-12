# MAGI — Multi-Agent Governance Infrastructure

[繁體中文版](README.zh-TW.md)

MAGI v2 is a locally-deployed AI operations platform built for a Taiwanese law firm. It runs entirely on a single Apple Silicon node, combining a Flask control plane, 60+ modular skill runners, a three-philosopher ensemble inference pipeline, a ReAct agentic tool-call engine, scheduled workers, on-device LLM inference, and deep legal workflow automation — all in one repository.

**macOS-primary.** Production runs on Apple Silicon via [oMLX](https://github.com/omlx/omlx) with a three-model day / night inference architecture. Windows / Linux via Ollama is also supported.

> **Single-node by default.** All production workloads run locally on Casper (Mac Mini M4). The codebase retains distributed inference scaffolding for Melchior and Balthasar. Set `MAGI_AVOID_DISTRIBUTED=0` to re-enable multi-node inference.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Current Public Status](#current-public-status)
- [Architecture](#architecture)
  - [Three-Model Inference — Day / Night](#three-model-inference--day--night)
  - [Three-Philosopher Ensemble Review](#three-philosopher-ensemble-review)
  - [Agentic Tool Calls — ReAct](#agentic-tool-calls--react)
  - [Chinese NLP & Knowledge Graph](#chinese-nlp--knowledge-graph)
- [Legal Automation](#legal-automation)
  - [Legal Aid Foundation (LAF)](#legal-aid-foundation-laf)
  - [Court File Review](#court-file-review)
  - [Court Transcripts](#court-transcripts)
  - [Apple Shortcuts (thin wrappers)](#apple-shortcuts-thin-wrappers)
- [Operations — `magi` CLI](#operations--magi-cli)
- [Skill Catalogue](#skill-catalogue)
- [Message Processing Flow](#message-processing-flow)
- [Governance & Security](#governance--security)
- [Configuration](#configuration)
- [Tech Stack](#tech-stack)
- [Repository Layout](#repository-layout)
- [Ports](#ports)
- [Testing](#testing)
- [License](#license)

---

## Quick Start

### macOS (Apple Silicon)

```bash
# 1. Clone
git clone https://github.com/WhaleChao/MAGI-v2.git && cd MAGI-v2

# 2. Beginner-safe installer dry run
python3 scripts/install_magi.py --dry-run --check-live

# 3. Install when the plan looks correct
python3 scripts/install_magi.py --yes
source .venv/bin/activate  # or source venv/bin/activate on existing installs

# 4. Create the first-run checklist and local .env without printing secrets
python3 scripts/first_run_setup.py --write-env
python3 scripts/first_run_setup.py --json

# 5. Edit .env, then run diagnostics
python3 scripts/magi_doctor.py

# 6. Start
launchctl load ~/Library/LaunchAgents/com.magi.daemon.plist
magi status
```

Existing operators can still use the manual flow:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-optional.txt
```

### Linux / Windows (Ollama backend)

```bash
ollama pull gemma2:9b          # or any supported model
MAGI_ALLOW_CLOUD_MODELS=1 python daemon.py
```

---

## Current Public Status

This branch is prepared for public release with private runtime material removed from git tracking. Local-only folders such as `.runtime/`, `.claude/`, `.claire/`, `runtime/supplement_cache/`, and operator deployment notes are ignored and should stay private.

Public readiness checks:

```bash
python3 scripts/public_release_audit.py --public-isolation
python3 scripts/first_run_setup.py --public --json
python3 scripts/magi_doctor.py --json
python3 scripts/install_magi.py --dry-run --check-live
```

`first_run_setup.py` is the guided entrypoint for a first-time operator: it can create a local `.env`, list missing required settings, keep next commands in a machine-readable checklist, and never prints token or password values. The public audit blocks high-confidence secrets and private tracked paths; before pushing to the public project, add `--public-isolation` to also block Lawsnote private integrations and private mailbox/NAS markers. For release and commercial use, run it with `--strict`; the release branch is expected to pass with `0 errors / 0 warnings`.

Before publishing or handing MAGI to another operator, treat these as go/no-go gates:

- README files, the operator guide, terms, privacy policy, data-retention policy, and third-party bill of materials are current.
- The daemon starts, and `/health`, the main OSC tabs, messaging channels, DB, NAS/file storage, and Google Calendar OAuth all pass live checks.
- NERV (`/dashboard/nerv` or `/nerv`) is the production status page for the target host; use it to confirm model, OCR, DB, NAS, and background-service health before handoff.
- `scripts/public_release_audit.py --strict` has no errors or warnings. Install-only packages without a private DB may use the dedicated `--skip-db` installability check.
- `.env`, OAuth tokens, DB dumps, case/client material, portal screenshots, NAS paths, and runtime reports are not tracked by git.
- LAF, court file review, transcript, and calendar workflows are high-risk workflows; production submission, DB restore, and bulk file movement must remain confirmation-gated.
- This release is single-host by design. Multi-tenant service, electronic signatures, and a public upload portal are outside the enabled scope; operators should use the built-in "對外資料" copy text with their existing communication channel.

Commercial readiness documents:

- [Commercial readiness guide](docs/COMMERCIAL_READINESS.md)
- [Terms of service template](docs/TERMS_OF_SERVICE.md)
- [Privacy policy](docs/PRIVACY_POLICY.md)
- [Data retention policy](docs/DATA_RETENTION_POLICY.md)
- [Third-party bill of materials](docs/THIRD_PARTY_BOM.md)
- [Security policy](SECURITY.md)
- [Support policy](SUPPORT.md)

Commercial production gate:

```bash
./venv/bin/python scripts/ops/commercial_readiness_live.py --strict-public
```

Use `--skip-db` only for public installability checks that intentionally do not include a private production database.

Gemma 4 E4B / MTP support is available through the MLX sidecar:

```bash
python3 scripts/serve_mlx_mtp.py --host 127.0.0.1 --port 8090
curl http://127.0.0.1:8090/health
```

### 2026-05 Stabilization Highlights

Recent hardening work is reflected in the public docs and live gates:

- **LAF**: consumer-debt checklists restore OSC conditional logic; income-list years are derived from the filing year. LAF status can be adjusted in the web UI, and closed cases can be archived while folders/files remain openable.
- **LAF closing**: enforcement cases can close with enforcement orders found under the judgment folder; same-name/different-procedure cases are no longer closed by name alone. Long-running progress reminders support 90-day cooldowns.
- **Activity counts**: hearings, meetings, detention visits, file review, and phone contact counts combine OSC todos, Google Calendar, meeting records, and review-folder evidence; review folders containing only payment slips are excluded.
- **PDF / OCR**: PDF naming supports envelope-page skipping, multi-engine OCR consensus, legal-text correction, and training feedback for court notices, procedural rulings, judgments, opposing-party pleadings, and judgment folders.
- **Pleadings**: OSC pleading generation includes Word/PDF layout safeguards and case-reason-scoped correction learning.
- **Accounting**: Google Sheets imports can exclude coworker-tagged rows, deduplicate recurring fixed expenses, and run on Monday/Friday schedules.
- **Legal research**: the Taiwan legal MCP adapter is available as a practical-opinion source; misses are reported as misses rather than filled by the model.
- **Office overview**: the web UI links cases, todos, LAF, pleading index, public-facing data, and business overview without duplicating the source modules.
- **Operations**: smoke50, commercial live gates, public secret audit, disk low-water alarms, cache cleanup, NAS mount guards, and notification-routing checks are part of the release discipline.

Live acceptance is covered by `scripts/live_magi_mtp_eval.py`. The latest local verification exercised JSON routing, ReAct real tool calls, all ReAct tool-selection paths, tool-confusion guards, and hallucination abstention checks.

---

## Architecture

```
User (LINE / Discord / Telegram / Web)
          │
          ▼
 ┌─────────────────────────────────┐
 │   message_pipeline.py           │  Intent classification
 │   20+ interceptors (legal / ops)│  Command dispatch
 └────────────┬────────────────────┘
              │  QUERY / CMD
              ▼
 ┌─────────────────────────────────────────────────────┐
 │            ensemble_chat_with_tools()               │
 │                                                     │
 │  Phase 1 — Casper + ReAct engine                    │
 │    ├─ No tool needed  → FINAL (direct answer)       │
 │    └─ Tool needed     → ACTION → execute → OBSERVE  │
 │                          ↺ up to 5 steps            │
 │                                                     │
 │  Phase 2 — Melchior + Balthasar parallel review     │
 │    ├─ Both approve    → MAGI consensus              │
 │    └─ Either vetoes   → show individual opinion     │
 └─────────────────────────────────────────────────────┘
              │
              ▼
      format_magi_response()
      Iron Dome output guard
      tw_output_guard (trust-badge leak prevention)
```

### Three-Model Inference — Day / Night

MAGI runs different model configurations depending on the time of day.

| Mode | Port 8080 | Port 8082 | Port 8083 | Trigger |
|------|-----------|-----------|-----------|---------|
| **Day** (07:00–21:50) | Gemma-4 E4B (Casper) | Phi-4-mini (Melchior) | SmolLM3-3B (Balthasar) | `cron` + daemon auto-start |
| **Night** (21:50–07:00) | Gemma-4 26B | — | — | `cron` switch |

- **Day mode**: Three models run in parallel for ensemble review. Each reviewer has an independent soul persona injected at startup (`docs/soul/SOUL_*.md`).
- **Night mode**: A single high-capacity 26B model handles batch tasks (LAF audit, PDF naming, transcript indexing, LoRA distillation).
- Switching is managed by two cron jobs (`job_omlx_switch_night` / `job_omlx_switch_day`) and `daemon.py` which auto-starts Phi-4 and SmolLM3 on day-mode boot.

### Three-Philosopher Ensemble Review

Each response goes through a two-phase pipeline:

**Phase 1 — Casper generates**
- Runs `ReActEngine.for_omlx()` with up to 8 tools and up to 5 ReAct steps.
- Uses `get_compact_tools(user_query)` — 8 always-on tools plus a gated `remember` tool (opens only when the user explicitly asks to save something).

**Phase 2 — Melchior + Balthasar review in parallel**
- Melchior (Phi-4-mini): logical consistency and legal accuracy review.
- Balthasar (SmolLM3-3B): format and citation auditing.
- Each reviewer independently votes `APPROVE` or `VETO` with a one-line reason.

**Output format**
- Unanimous approval → `「MAGI：...」` (consensus label)
- Any veto → shows the dissenting philosopher's name and reason.
- Tool sources are appended when tools were used (e.g., `（資料來源：web_search、query_cases）`).

**Model policy**
- Mainland Chinese models (Qwen / DeepSeek / GLM / Yi) are excluded due to censorship risk.
- Rule-based simplified-Chinese detector blocks SC responses without LLM inference overhead.

### Agentic Tool Calls — ReAct

`ReActEngine.for_omlx()` runs a synchronous ReAct loop against E4B:

```
User query
  → Build system prompt (soul + tool manifest + ReAct format)
  → LLM turn: THINK → ACTION: <tool> / PARAMS: {...}
  → Execute tool locally
  → Inject OBSERVATION into conversation
  → Repeat until FINAL: <answer> or max_steps (5) reached
```

**Available tools (compact set)**

| Tool | Description |
|------|-------------|
| `search_memory` | RAG recall from FAISS + Graph-RAG |
| `web_search` | Live web search via Scrapling |
| `query_cases` | Look up case DB by case number / party |
| `get_schedule` | Read court calendar |
| `calculate` | Safe arithmetic evaluator |
| `current_time` | Current datetime |
| `summarize` | Long-text summarisation (extractive fallback) |
| `translate` | Translation with Google GTX fast path |
| `remember` | *(gated)* Write to long-term memory |

Feature flag: `MAGI_ENSEMBLE_TOOLS=1` (default `0`).

### Heavy Cloud Fallback — NVIDIA NIM (Plan A, 2026-04-19)

When local oMLX fails or a request needs SOTA reasoning, MAGI can fall back to NVIDIA NIM's free cloud inference:

- **Trigger**: User prefixes message with `@heavy` or `@重型` (opt-in, never automatic)
- **Primary model**: `meta/llama-3.1-405b-instruct` (128K context, multilingual, no content censorship)
- **Fast model**: `meta/llama-3.3-70b-instruct` for simpler heavy requests
- **Hardcoded block list**: Chinese models (DeepSeek / Qwen / MiniMax / Kimi / GLM / Yi / Baichuan / Moonshot / InternLM / ChatGLM / SenseTime) — banned due to content censorship unsuitable for legal work
- **PII scrubber**: Reversible masking of TW ID, LAF case no, court case no, mobile, and DB-known client names (restored in reply)
- **Safety**: Circuit breaker (3×429 → 60s cooldown), daily budget (500 req), semaphore (3 concurrent)
- **Rate limit**: 40 req/min (shared across all NIM models on a single `nvapi-` key)
- **Feature flag**: `NVIDIA_NIM_ENABLE=0` (default off)

### Chinese NLP & Knowledge Graph

- **PKUSeg** segmenter with legal dictionary (`skills/engine/legal_dict.txt`), via Python 3.11 sidecar for compatibility.
- **Graph-RAG** (`skills/engine/knowledge_graph/`): entity extraction → relation building → community detection → context injection into `recall()`.
- GraphStore uses mtime-keyed load cache; entity fast-path for short legal queries keeps p95 < 200 ms.
- All vector embeddings use NLP-normalized input; original text is preserved for display.

---

## Legal Automation

### Legal Aid Foundation (LAF)

Automates the full lifecycle of Legal Aid Foundation cases:

| Stage | What MAGI does |
|-------|----------------|
| **Incoming mail** | Gmail monitor detects LAF notification emails |
| **Portal go-live** | Auto-fills case opening forms, uploads commission letter + LAF notice |
| **Pending drafts** | Scans portal for unsigned drafts, surfaces to lawyer |
| **Closing** | Drafts case-closing submissions with correct remark format; supports `引用OOO的會議` (inherit another case's meeting count) and `OOO就是結案檔案` (specify any file as closing basis by keyword) |
| **Document finalisation** | OSC document index can produce stamped 正本 / 副本 / 繕本 files, including manual stamp placement and final PDF merge |
| **Consumer-debt checklist** | OSC LAF tab restores the conditional consumer-debt required-document checklist, copyable client text, and LAF number detection/sync |
| **Checklist CRUD** | OSC LAF tab provides editable legal-aid required-item checklists; case cards retain a separate case supplement checklist |
| **CSV exchange** | Cases and clients can be imported/exported as UTF-8 CSV from the Paperclip UI |
| **Office outputs** | Case cards can generate address-label PNG files; quotations can be exported as PDF |
| **Theme toggle** | Paperclip includes a persisted light/dark theme switch for long drafting sessions |
| **Batch ops** | Bulk query / batch closing / batch audit via natural-language commands |
| **Smart lookup** | Disambiguates multiple cases using DB case type, LAF number, legal-aid status, status priority, and keyword filtering |
| **LAF activity counts** | Court hearings, meetings, detention visits, file review, and phone-contact counts are matched to LAF cases through DB-backed identity rules; same-name regular cases are not mixed into LAF reports |

NAS folder structure is respected for each case category (法扶 / 一般 / 無償 / 指定辯護).

### Google Calendar / OSC Sync Rules

MAGI can read multiple Google calendars, but OSC todo import is intentionally narrow so coworker-entered events, holidays, and private reminders do not pollute case records:

- General OSC events must begin with the OSC system case number, for example `[2026-0035] Hearing` or `2026-0035: Hearing`.
- LAF activity-count events may still be imported when the DB identifies the target as a Legal Aid Foundation case and the event text is a reportable activity: hearing, meeting, detention visit, file review, or phone contact.
- Same-name cases are resolved through DB fields such as `laf_case_no`, `application_no`, `case_category=法律扶助案件`, `legal_aid_status`, and case-reason hints. MAGI skips only when multiple LAF cases for the same client remain indistinguishable.
- Imported Google Calendar event ids are deduplicated to avoid repeated todos.

### Court File Review

Two-phase electronic court file review (`file-review-orchestrator`):

1. **Apply** — system fills the e-filing form and captures a screenshot.
2. **Confirm** — generates a 6-character hex confirmation code (30-minute TTL), sends screenshot to lawyer for review.
3. **Lawyer approves** — replies with the code; system re-authenticates and submits.

Security gate: confirmation endpoint only accepts requests from `user/telegram/discord/line` sources (not raw CLI) unless `MAGI_FILE_REVIEW_ALLOW_CONFIRM=1`.

Attachment scan has a 20-second budget and 600-file candidate cap to protect NAS from I/O saturation.

### Court Transcripts

- Automatic download and deduplication via MD5 registry (JSON + MariaDB dual-write).
- DB fallback: if local JSON is missing, dedup records are recovered from the DB.
- Self-test, `db_probe`, and smoke-login steps integrated.

### Apple Shortcuts (thin wrappers)

Four `text/plain`-in / `text/plain`-out endpoints on Tools API (`5003`) so
`Get Contents of URL` in Shortcuts.app can call MAGI without JSON gymnastics:

| Endpoint | Input body | Returns |
|----------|-----------|---------|
| `POST /shortcut/ocr` | raw image bytes (jpg/png/heic) | extracted text |
| `POST /shortcut/pdf_text` | raw PDF bytes | extracted text (with OCR fallback) |
| `POST /shortcut/summarize` | `text/plain` UTF-8 body | summary |
| `POST /shortcut/transcribe` | raw audio bytes | transcription |

All require `X-API-Key`. Body size caps: OCR 20 MB, PDF 50 MB, audio 100 MB, text 500 KB.

---

## Operations — `magi` CLI

```
magi status       # full system health (services, oMLX, NAS, DB, zombies)
magi restart      # clean restart via launchctl kickstart
magi stop         # graceful shutdown
magi zombie       # list + reap zombie processes
magi logs         # tail all logs
```

NAS status checks both `/Volumes/` and `~/.magi_mounts/` (Tailscale fallback path).

**50+ scheduled cron jobs** (managed via `cron_jobs.json`, executed by the Discord Bot scheduler):

| Category | Jobs |
|----------|------|
| Legal | LAF pending scan, nightly LAF audit, judicial API pull (night + morning), file review check (10:00 / 15:00 weekdays) |
| Knowledge | Obsidian ingest (`--limit 50`, 07:10 daily), case card index sync, insight sync, knowledge lint, reprocess insights, judgment retry |
| Ops | Health report (07:30), nightly autopilot, optimize report, nightly regression, purge persona, debug cleanup |
| NAS / Files | PDF namer (nightly), weekend bookmark, transcript sync, weekly legal crawl |
| Market | Market briefing (weekday 08:30), world monitor (every 6h), hedge fund committee |
| Infrastructure | oMLX day/night switch, OSC case index/scan, gcal sync, smoke chat check |
| **Disk hygiene (2026-05-12)** | **`disk_low_water_alarm`** (hourly :05 — High <30 GB / Critical <10 GB → `self_repair`), **`weekly_cache_cleanup`** (Sun 04:00 — remove retired Ollama root and rebuildable caches; protect MAGI DB, NAS, model roots, training outputs, standalone JSON/pickle/db state files, and judicial raw backlog) |

### Self-repair loop & autonomy guards (2026-04-21 → 2026-04-25)

- **Phase 1 issue tracker** — every cron failure / orchestrator catch-all / Tools API errorhandler logs to `.runtime/issue_agenda.jsonl` (PII-scrubbed, 5-min dedup, 5000-row rotation). Truncation limits: stderr `[:4000]`, error_msg `[:5000]`, context `[:2000]`. Set `MAGI_ISSUE_TRACKER_ENABLE=1`.
- **Layer 1 — `omlx_heartbeat_reaper.py`** — kills duplicate `omlx serve` processes by `--model-dir` fingerprint. Default `OMLX_HEARTBEAT_KILL_MODE=shadow`.
- **Layer 2 — `memory_watchdog.py`** (LaunchAgent `com.magi.memory-watchdog`) — kills the highest-RSS recoverable MAGI subprocess when swap >8 GB or free+inactive <2 GB for 90 s. Default `MAGI_WATCHDOG_KILL_MODE=shadow`; it also reaps MAGI-owned Playwright driver/headless browser processes older than 45 minutes so portal automation teardown hangs do not linger. Decisions logged to `~/.local/share/magi/runtime/metrics/memory_watchdog_decisions.jsonl`.
- **NAS load guard (2026-05-08)** — `com.magi.nas-mountpoints` only removes unmounted empty/stale `/Volumes/homes` and `/Volumes/lumi` directories and never pre-creates them, preventing macOS from mounting as `homes-1`/`lumi-1`; daemon NAS recursive watching is opt-in via `MAGI_ENABLE_NAS_FSWATCHER=1`.
- **Portal retry guard (2026-05-08)** — the LAF Gmail monitor no longer retries pending portal downloads at every MAGI boot, preventing surprise NAS/portal batches after restart; set `MAGI_LAF_PORTAL_RETRY_ON_START=1` to enable. `file_review_auto_worker` is the single background owner for file-review checks/downloads and now runs on startup plus every hour by default; set `MAGI_FILE_REVIEW_AUTO_DOWNLOAD=0` or `MAGI_FILE_REVIEW_AUTO_RUN_ON_START=0` only for maintenance windows.
- **Cron catch-up guard (2026-05-08)** — startup catch-up skips NAS/case-index/portal-heavy jobs such as OSC scan, Obsidian ingest, PDF benchmark, and LAF nightly audit so reboot recovery does not flood the NAS.
- **Layer 3 — `omlx_switch_gatekeeper.py`** — preflight RSS check + TTL pause (≤24 h) before oMLX day/night switch. **Enforce by default.**
- **Layer 4 — `disk_cleanup_healthcheck.py`** (cron 03:45) — JSONL rotation + LRU cache prune. Default `MAGI_DISK_CLEANUP_DRY_RUN=1`. Build outputs that still contain standalone state files (JSON / pickle / db / sqlite) are skipped so Paperclip / MAGI portable data is not removed by mistake.

---

## Skill Catalogue

60+ skill runners under `skills/`, each with a standalone `action.py` entry point.

### Legal
| Skill | Function |
|-------|----------|
| `laf-orchestrator` | LAF case lifecycle automation |
| `file-review-orchestrator` | Two-phase electronic court file review |
| `transcript-downloader` | Court transcript download + dedup |
| `statutes-vdb` | Statute vector DB + article mapping |
| `judgment-collector` | Judicial Yuan judgment scraping |
| `judicial-web-search` | Live judicial website search (HTTP form + Scrapling) |
| `judicial-flow-search-archive` | Local judgment archive fallback |
| `contract-review` | AI-assisted contract review with MarkItDown |
| `trial-prep` | Trial preparation checklists |
| `evidence-admissibility` | Hearsay rule classification for criminal case indices |
| `labor-law-calculator` | Overtime / severance pay calculator |
| `laf-refine-case` | LAF case data enrichment |
| `laf-withdrawal-report` | LAF withdrawal report automation |
| `brief-gen` | AI-generated legal brief drafts |
| `court-hearing-reminder` | Hearing date reminders |
| `hearing` | Hearing management |

### Documents & PDF
| Skill | Function |
|-------|----------|
| `pdf-namer` | AI PDF naming (Vision OCR + multi-engine consensus) |
| `pdf-bookmarker` | PDF TOC and bookmark generation |
| `pdf-annotator` | PDF annotation (legacy, deprecated) |
| `doc-producer` | Document production pipeline |
| `docx` | Word document creation / editing |
| `pptx` | PowerPoint generation |
| `xlsx` | Spreadsheet processing |
| `documents` | Unified document reader (MarkItDown adapter) |
| `screenshot-sorter-tw` | Screenshot classification and filing |

### Intelligence & Research
| Skill | Function |
|-------|----------|
| `market-briefing` | Hedge fund committee: Technical / Fundamental / Sentiment analysts + Risk & Portfolio managers |
| `worldmonitor-intel` | Global news and legal intelligence monitoring |
| `autoresearch` | Autonomous research pipeline |
| `insight-refine` | Insight distillation and refinement |
| `crawler-targets` | Scheduled web crawl targets |
| `obsidian` | Obsidian vault sync, vector ingest, and case card index (`30_Index/`) |

### Memory & Inference
| Skill | Function |
|-------|----------|
| `memory` | Long-term memory: FAISS vector store + Graph-RAG |
| `brain_manager` | Cross-session memory management |
| `reasoning` | Step-by-step reasoning scaffold |
| `bridge` | Ensemble inference bridge (Casper / Melchior / Balthasar) |
| `casper` | Casper LLM direct interface |
| `casper-client` | Remote Casper API client |
| `translator` | Translation with Google GTX primary + LLM fallback |

### Operations
| Skill | Function |
|-------|----------|
| `magi-autopilot` | Nightly autopilot batch tasks |
| `magi-doctor` | System health diagnostics |
| `magi-self-repair` | Automated self-repair for known failure modes |
| `process-hygiene` | Zombie and stale process cleanup |
| `iron-dome` | Security rule engine |
| `ops` | Operational helpers (notify, red phone, etc.) |
| `gmail-drafts` | Gmail draft management |
| `management` | Internal management utilities |

---

## Message Processing Flow

```
Incoming message
    │
    ├─ 20+ regex interceptors (LAF / file review / transcript / scheduling / billing …)
    │       ↓ matched → domain handler (skip LLM entirely)
    │
    ├─ Intent classifier  →  CMD / QUERY / CHAT / SYSTEM
    │
    ├─ CMD / QUERY (MAGI_ENSEMBLE_TOOLS=1)
    │       ↓  ensemble_chat_with_tools()
    │       ↓  Phase 1: ReAct (Casper + tools, up to 5 steps)
    │       ↓  Phase 2: Melchior + Balthasar parallel review
    │       ↓  format_magi_response()
    │
    ├─ CMD / QUERY (MAGI_ENSEMBLE_TOOLS=0, default)
    │       ↓  ensemble_chat_verified() — direct three-sage text generation
    │
    └─ CHAT  →  grounded_ai.chat_casper() with small-talk fast path
```

**Channels**: LINE Messaging API, Discord Bot, Telegram Bot, Web API (`/osc/external/chat`).

---

## Governance & Security

### Iron Dome
Multi-layer security review on every tool call and shell command:
- Pattern matching against known dangerous strings (`rm -rf`, SQL `DROP`, path traversal, etc.)
- Severity scoring: BLOCK / WARN / ALLOW.
- Runs before every ReAct tool execution.

### Trust Badge Leak Prevention
- Internal context labels (`[已驗證事實]`, `[使用者陳述]`, etc.) are for internal reasoning only.
- `tw_output_guard.py` strips or rewrites any response that leaks these labels to external channels.
- `grounded_ai.py` detects persona hallucination (`身為 CASPER …`) and retries before surfacing output.

### Mainland Model Policy
Models with known content restrictions (Qwen / DeepSeek / GLM / Yi series) are excluded by policy. Only open-weight models without censorship constraints are permitted.

### Reaper Safety
`daemon.py` Phase 4 stale-process reaper has an explicit safe-list (`REAPER_SAFE_UTILITIES`) that protects oMLX, magi_menubar, admin_server, and benchmark processes from being killed as "stale unprotected Python".

### SafeProcess — Shell Injection Guard (`api/platforms/safe_process.py`)
All cron commands are routed through `SafeProcess` when `MAGI_USE_SAFE_PROCESS=1` (legacy `shell=True` path preserved for gradual rollout):
- **argv whitelist**: only `python3`, `launchctl`, `git`, `curl`, `mount_smbfs`, `osascript`, and the MAGI venv interpreter are allowed as `argv[0]`.
- **Shell metachar denylist**: `;`, `|`, `&`, `` ` ``, `$`, `<`, `>`, newline — rejected even in no-shell mode.
- **Env prefix whitelist**: only `MAGI_`, `JUDICIAL_`, `PATH`, `HOME`, `USER`, `PYTHONPATH`, `LANG`, `LC_*`, `TZ` pass through.
- **Timeout flow**: SIGTERM → 3 s grace → SIGKILL; 1 MB stdout cap; `BoundedSemaphore(8)` for concurrency control.
- **CI gate**: `scripts/ci/check_shell_true.py` blocks any new `shell=True` / `os.system(f"…")` / `os.popen()` additions. Four legacy sites are grandfather-listed until SafeProcess Phase 3 grey-rollout is complete.

### RemoteHealthGate — Unified Inference Circuit Breaker (`api/platforms/remote_health_gate.py`)
Provides a single shared circuit breaker for all remote inference peers (Balthasar / Melchior / NIM). Replaces the previous per-module ad-hoc try/except pattern:
- Per-peer `PeerState` with `threading.Lock` (no bare acquire/release).
- Progressive cooldown: 30 s → 5 min → 30 min → 2 h.
- Probe result cache (`probe_cache_ttl_sec`) to avoid redundant HTTP health checks.
- `get_gate()` module-level singleton protected by `_SINGLETON_LOCK`.
- Feature flag: `MAGI_USE_REMOTE_HEALTH_GATE=1`.

### RuntimeDir — Centralised `.runtime/` Path Management (`api/platforms/runtime_dir.py`)
Ensures all ephemeral state lands under `.runtime/` rather than being scattered in the repo root or cron_jobs.json:
- `atomic_write_json()` — write via `.tmp` + `os.replace()` (no partial writes on crash).
- `atomic_append_jsonl()` — thread-safe append with auto-rotation.
- `legacy_fallback()` — dual-read: try new path first, fall back to legacy path for zero-downtime migration.
- `cron_state()` — separates job execution timestamps (`last_run`, `last_run_minute`) from the job definition file `cron_jobs.json`, keeping the definition file commit-stable.
- Feature flag: `MAGI_USE_RUNTIME_DIR=1`.

---

## Configuration

Key environment variables (set in `.env`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `MAGI_ENSEMBLE_TOOLS` | `0` | Enable ReAct agentic tool calls in ensemble |
| `MAGI_ALLOW_CLOUD_MODELS` | `0` | Allow Claude / GPT fallback |
| `MAGI_USE_SCRAPLING` | `0` | Use Scrapling for web fetch (faster, no browser) |
| `MAGI_USE_MARKITDOWN` | `0` | Use MarkItDown for document extraction |
| `MAGI_PDF_OCR_CONSENSUS` | `1` | Multi-engine OCR consensus for PDF naming (pdf-namer only) |
| `MAGI_OCR_CACHE_ENABLE` | `1` | SHA-256 image-hash LRU cache for new unified OCR runtime |
| `MAGI_VISION_OCR_CONSENSUS_ENABLE` | `1` | `/vision` API opt-in consensus (`task_type=ocr/text/scan` only; captcha bypassed) |
| `MAGI_SHORTCUT_OCR_CONSENSUS_ENABLE` | `1` | `/shortcut/ocr` consensus (mimetype stays `text/plain`) |
| `MAGI_PDF_OCR_CONSENSUS_SHADOW` | `1` | pdf_bridge shadow mode — run new consensus for metrics, return legacy text |
| `MAGI_PDF_OCR_CONSENSUS_ENABLE` | `0` | pdf_bridge full switch (leave `0` until shadow metrics confirm parity) |
| `MAGI_LAF_OCR_CONSENSUS_SHADOW` | `0` | LAFVision shadow mode (observe only; production default is off) |
| `MAGI_LAF_OCR_CONSENSUS_ENABLE` | `1` | LAFVision guarded-write OCR consensus (auto-adopt high-confidence output; conflicts/low-confidence stay non-writable) |
| `MAGI_OBSIDIAN_OCR_CONSENSUS_ENABLE` | `0` | Obsidian PDF OCR fallback consensus |
| `MAGI_NAS_HOST` | `MAGI_NAS_HOST` | NAS LAN IP |
| `MAGI_NAS_TAILSCALE_HOST` | `MAGI_NAS_TAILSCALE_HOST` | NAS Tailscale IP (auto-fallback) |
| `MAGI_AVOID_DISTRIBUTED` | `1` | Run single-node only |
| `MAGI_COMMITTEE_LIGHT_MODEL` | *(E4B)* | Analyst agents model |
| `MAGI_COMMITTEE_HEAVY_MODEL` | *(26B)* | Risk / Portfolio manager model |
| `MAGI_FILE_REVIEW_ALLOW_CONFIRM` | `0` | Allow CLI-triggered file review confirmation |
| `MAGI_JUDICIAL_VERIFY_SSL` | `0` | SSL verify for judicial website (disable for TLS quirks) |
| `NVIDIA_NIM_ENABLE` | `0` | Enable NVIDIA NIM cloud fallback for heavy tasks (Plan A) |
| `NVIDIA_NIM_API_KEY` | — | `nvapi-…` key from build.nvidia.com (free tier, 40 req/min) |
| `NVIDIA_NIM_MODEL` | `meta/llama-3.1-405b-instruct` | Heavy model (128K context, multilingual, non-censored) |
| `NVIDIA_NIM_MODEL_FAST` | `meta/llama-3.3-70b-instruct` | Fast model for general @heavy requests |
| `NVIDIA_NIM_REQUIRE_OPTIN` | `1` | Require `@heavy` / `@重型` prefix to trigger NIM |
| `NVIDIA_NIM_REQUIRE_PII_SCRUB` | `1` | PII scrub before sending to cloud (never disable) |
| `NVIDIA_NIM_DAILY_BUDGET` | `500` | Max daily NIM requests before blocking |
| `MAGI_USE_REMOTE_HEALTH_GATE` | `0` | Unified circuit breaker for Balthasar / Melchior / NIM (R1) |
| `MAGI_USE_SAFE_PROCESS` | `0` | Route cron commands through argv whitelist guard instead of `shell=True` (R2) |
| `MAGI_USE_RUNTIME_DIR` | `0` | Write cron state + ephemeral JSON to `.runtime/` instead of `cron_jobs.json` (R3) |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Runtime** | Python 3.9+ (production: 3.14 on macOS), venv |
| **LLM inference** | [oMLX](https://github.com/omlx/omlx) (MLX / Apple Silicon) · Ollama (Linux/Windows) |
| **Models** | Gemma-4 E4B · Phi-4-mini · SmolLM3-3B · Gemma-4 26B (night) |
| **Embeddings** | ModernBERT-embed-4bit (port 8081) |
| **Vector store** | FAISS (144K+ vectors, mmap) |
| **Database** | MariaDB (local + Tailscale remote sync) |
| **NLP** | PKUSeg (3.11 sidecar) · Apple NaturalLanguage fallback |
| **Knowledge graph** | Custom Graph-RAG (entity extraction → community detection) |
| **Web scraping** | Scrapling · requests + BeautifulSoup fallback |
| **Document parsing** | MarkItDown · pdftotext · fitz · pdfplumber · Tesseract · macOS Vision |
| **OCR** | macOS Vision · RapidOCR · Tesseract · unified runtime `skills/engine/ocr/` (Vision + Tesseract consensus, SHA-256 image cache, legal-text corrector, feature-flagged) |
| **API framework** | Flask · Flask-Login · Flask-SocketIO |
| **Scheduling** | Internal CronScheduler in `discord_bot.py` (cron_jobs.json) |
| **Channels** | LINE Messaging API · Discord.py · python-telegram-bot |
| **NAS** | SMB via LAN (MAGI_NAS_HOST) with Tailscale fallback (MAGI_NAS_TAILSCALE_HOST) |
| **Calendar** | Google Calendar API (OAuth2, auto-refresh) |
| **Security** | Iron Dome rule engine · tw_output_guard · trust-badge leak detector |
| **Testing** | pytest (1 336 tests) |

---

## Repository Layout

```
MAGI_v2/
├── daemon.py                   # Main process manager (KeepAlive, reaper, day/night switch)
├── api/
│   ├── server.py               # Flask API (port 5002)
│   ├── tools_api.py            # Tools API (port 5003)
│   ├── discord_bot.py          # Discord bot + CronScheduler
│   ├── pipelines/              # message_pipeline, command_dispatch, skill_dispatch …
│   ├── domains/                # laf_flow, multimedia_flow, judgment_flow, schedule_flow …
│   ├── blueprints/             # web_runtime, admin_runtime
│   ├── platforms/              # RemoteHealthGate (CB), SafeProcess (argv guard), RuntimeDir (path mgmt)
│   ├── nas_mount_guard.py      # NAS SMB auto-mount + Tailscale fallback
│   ├── debug_capture.py        # Unified debug screenshot helper
│   └── tw_output_guard.py      # Output normalisation + trust-badge leak guard
├── skills/
│   ├── engine/                 # react_engine, tool_registry, chinese_nlp, knowledge_graph …
│   ├── bridge/                 # ensemble_inference, grounded_ai, llm_direct …
│   ├── legal/                  # laf.py, judicial.py (browser automation)
│   ├── memory/                 # mem_bridge, vector_pipeline
│   ├── documents/              # file_bridge, multimodal_parser, document_reader
│   ├── research/               # web_research, github_monitor
│   ├── evolution/              # usage_tracker, skill_improver, skill_genesis
│   ├── laf-orchestrator/       # LAF lifecycle skill
│   ├── file-review-orchestrator/
│   ├── transcript-downloader/
│   ├── pdf-namer/
│   ├── market-briefing/        # Hedge fund committee (agents/, models/, predict/)
│   └── … (60+ skills total)
├── docs/
│   └── soul/                   # SOUL_CASPER.md · SOUL_MELCHIOR.md · SOUL_BALTHASAR.md
├── tests/                      # 1 336 pytest tests
├── cron_jobs.json              # Single source of truth for all scheduled jobs
└── .env                        # Runtime configuration (not committed)
```

---

## Ports

| Port | Service |
|------|---------|
| `5002` | Flask main server (`/health`, `/chat`, `/skills/…`) |
| `5003` | Tools API (`/summarize`, `/translate`, `/collab/transcribe`, `/osc/external/chat`, `/shortcut/*`) |
| `8080` | oMLX text — Gemma-4 E4B (Casper, day) / Gemma-4 26B (night) |
| `8081` | oMLX embed — ModernBERT-embed-4bit |
| `8082` | oMLX text — Phi-4-mini-instruct (Melchior, day only) |
| `8083` | oMLX text — SmolLM3-3B (Balthasar, day only) |
| `8088` | Website Admin panel |
| `50052` | gRPC RPC Worker |

---

## Testing

```bash
# Full suite (~140 files · ~1 575 tests · ~12 min)
./venv/bin/python -m pytest -q

# By module
pytest tests/test_routing_unified.py            # unified routing (38 tests)
pytest tests/test_tools_api_async_jobs.py       # async job queue API (18 tests)
pytest tests/test_react_omlx.py                 # ReAct + ensemble tools
pytest tests/test_document_reader.py            # MarkItDown adapter (24 tests)
pytest tests/test_translator_legal_termbase.py  # three-tier legal termbase (22 tests)
pytest tests/test_translator_post_edit.py       # APE post-edit pipeline (22 tests)
pytest tests/test_knowledge_graph.py            # Graph-RAG
pytest tests/test_hallucination_regression.py   # hallucination guard (22 tests)
pytest tests/test_laf_progress_helper.py        # LAF progress report helpers (16 tests)
pytest tests/test_memory_policy.py              # memory write policy (20 tests)

# Live smoke (requires running services)
magi status
curl http://127.0.0.1:5002/health
curl http://127.0.0.1:5003/health
MAGI_USE_SCRAPLING=1 skills/judicial-web-search/action.py --task self_test
skills/laf-orchestrator/action.py --task self_test
skills/file-review-orchestrator/action.py --task self_test
skills/transcript-downloader/action.py --task self_test
```

### Test Suite Coverage (~140 files · ~1 575 tests)

| Category | Files | Tests | Key areas |
|----------|-------|-------|-----------|
| **Routing & dispatch** | 13 | 190 | Unified routing, skill contracts (market-briefing / trial-prep / contract-review), command dispatch, skill smoke |
| **Apple platform** | 10 | 173 | Spotlight, Keychain, EventKit (calendar), CoreML classifier, NaturalLanguage NLP, Contacts, file monitor |
| **Infrastructure** | 33 | 218 | Health probes, session/context management, audio pipeline, text processing, logging, packaging, entrypoints, security baseline (CORS / headers / cookies) |
| **Platform layer (R1–R3)** | 7 | 72 | RemoteHealthGate circuit breaker (16), Balthasar/Melchior/NIM opt-in (15), SafeProcess argv/env/timeout (19), RuntimeDir atomic I/O (14), cron state migration (8) |
| **Documents & PDF** | 7 | 86 | MarkItDown adapter, PDF bridge (OCR + timeout recovery), pdf-namer (naming_validator, dynamic confidence), pdf-bookmarker (OLA threshold, Vision fallback) |
| **Legal Aid (LAF)** | 11 | 81 | Progress report helpers, submit-pending token lifecycle, closing E2E mock, email classification, case category resolver, duplicate check |
| **Config & runtime** | 21 | 80 | Runtime path resolution, modular config, model config, authz, provider adapters, job scheduling |
| **Tools API** | 8 | 76 | Tool-first pipeline, async job queue (202/poll pattern), inference gateway routing, shortcut endpoints |
| **Translation** | 5 | 65 | Three-tier legal termbase (MOJ SQLite / JSON / prompt), Apple Translation + APE post-edit validator, pipeline resilience, unified API |
| **Memory system** | 8 | 58 | Memory write policy, grounding & query augmentation, Graph-RAG recall, false-memory regression, assistant-utterance promotion guard, provenance tracking |
| **Verification & safety** | 6 | 49 | Hallucination regression (22 scenarios), answer verifier, authz gate, output guard (trust-badge leak), security baseline |
| **Data & persistence** | 6 | 45 | Job queue (SQLite), embedding router, migration framework, DB helper, vector pipeline NLP |
| **CI / packaging** | 2 | 29 | Hardcode checker, console-script targets |

CI gates:
- `scripts/ci/check_hardcodes.py` — fails on any committed IP / credential.
- `scripts/ci/check_shell_true.py` — blocks new `shell=True` / `os.system(f"…")` additions (grandfather list for 4 approved legacy sites).

---

## License

Private / proprietary. All rights reserved.

Source code is provided for reference and internal use only. Not licensed for redistribution or commercial use without written permission.
