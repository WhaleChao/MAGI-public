# MAGI — Multi-Agent Governance Infrastructure

[繁體中文版](README.zh-TW.md)

MAGI is a locally-deployed AI operations platform built for a Taiwanese law firm. It runs entirely on a single node, combining a Flask control plane, 57+ modular skill runners, scheduled workers, local LLM inference, and deep legal workflow automation in one repository.

**Cross-platform**: Runs on **macOS** (Apple Silicon via oMLX) and **Windows** (NVIDIA/CPU via Ollama). A built-in Setup Wizard detects your hardware, recommends models, and generates configuration automatically.

> **Single-node by default.** The codebase retains distributed inference scaffolding (Melchior, Balthasar) but all production workloads run locally on Casper. Set `MAGI_AVOID_DISTRIBUTED=0` to re-enable multi-node inference.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Setup Wizard](#setup-wizard)
- [Platform Support](#platform-support)
- [Architecture](#architecture)
- [All Skills (57+)](#all-skills-57)
  - [Legal Automation (14 skills)](#legal-automation-14-skills)
  - [Document Processing (7 skills)](#document-processing-7-skills)
  - [Financial Analysis (1 skill, 7 sub-commands)](#financial-analysis-1-skill-7-sub-commands)
  - [System Intelligence (7 skills)](#system-intelligence-7-skills)
  - [Communication & Utilities (7 skills)](#communication--utilities-7-skills)
  - [Infrastructure — Bridge Modules (14 modules)](#infrastructure--bridge-modules-14-modules)
  - [Infrastructure — Ops Modules (19 modules)](#infrastructure--ops-modules-19-modules)
  - [Self-Governance (3 modules)](#self-governance-3-modules)
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

### macOS (Apple Silicon — recommended)

```bash
# 1. Clone
git clone https://github.com/WhaleChao/MAGI.git && cd MAGI

# 2. Python environment
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-optional.txt   # all skill dependencies

# 3. Install oMLX — local Apple Silicon MLX inference engine
brew install omlx

# 4. Database
brew install mariadb && brew services start mariadb

# 5. Setup Wizard (auto-detects hardware, recommends models, generates .env)
python3 setup_wizard.py

# 6. Run
./start_magi.sh
```

### Windows

```powershell
# 1. Clone
git clone https://github.com/WhaleChao/MAGI.git && cd MAGI

# 2. Python environment
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-optional.txt
pip install -r requirements-windows.txt   # Windows-specific (pywin32, llama-cpp-python)

# 3. Install Ollama — cross-platform inference engine
# Download from https://ollama.com/download/windows

# 4. Database
# Install MariaDB from https://mariadb.org/download/

# 5. Setup Wizard
python setup_wizard.py

# 6. Run
start_magi.bat
```

### Verify

```bash
curl http://localhost:5003/sages        # Tools API health check
curl http://localhost:5002/api/status    # Server status
curl http://localhost:5002/health        # Full health (FAISS, disk, uptime)
```

---

## Setup Wizard

First-time users are guided through a web-based setup wizard that:

1. **Hardware Detection** — auto-detects CPU, GPU (Metal/CUDA), RAM, disk space
2. **Engine Check** — verifies oMLX (macOS) or Ollama (Windows/Linux) installation
3. **Model Recommendation** — suggests optimal models based on your hardware:
   - Apple Silicon (≥16 GB): TAIDE-12b (text+vision) + Coder-14B + ModernBERT + GLM-OCR
   - NVIDIA GPU (≥8 GB): TAIDE-8b GGUF + Qwen2.5-7b + Nomic-embed
   - CPU-only (≥8 GB): Lightweight GGUF models
4. **Configuration** — collects LINE API tokens, database credentials, admin identity
5. **Connection Test** — validates LINE API and database connectivity
6. **`.env` Generation** — produces a complete environment configuration file

Run manually anytime: `python3 setup_wizard.py`

The wizard launches automatically on first `daemon.py` startup if `.env` is missing or incomplete.

---

## Platform Support

| Feature | macOS (Apple Silicon) | Windows (NVIDIA/CPU) | Linux |
|---------|----------------------|---------------------|-------|
| Inference Engine | oMLX (MLX) | Ollama (GGUF) | Ollama (GGUF) |
| File Locking | fcntl | msvcrt | fcntl |
| Service Management | LaunchAgent | Task Scheduler | systemd |
| Calendar Integration | Apple Calendar (osascript) | Outlook (COM) | — |
| Browser Automation | Playwright / Selenium | Playwright / Selenium | Playwright / Selenium |
| Tool Discovery | Homebrew paths | Program Files paths | Standard paths |
| Process Daemon | `start_magi.sh` | `start_magi.bat` | `start_magi.sh` |

### Platform Abstraction Layer

All platform-specific code is centralized in `skills/ops/platform_utils.py`:

```python
from skills.ops.platform_utils import (
    IS_MACOS, IS_WINDOWS, IS_LINUX,
    file_lock, file_unlock, locked_file,
    get_venv_python, find_executable,
    get_service_manager, query_calendar_events,
)
```

Key abstractions:
- **`file_lock` / `file_unlock`** — fcntl (Unix) / msvcrt (Windows)
- **`get_service_manager()`** — returns LaunchAgent / TaskScheduler / systemd manager
- **`find_executable(name)`** — searches PATH + platform-specific directories
- **`query_calendar_events()`** — Apple Calendar or Outlook COM
- **`get_venv_python()`** — resolves `venv/bin/python3` or `venv\Scripts\python.exe`

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      Channels                             │
│        LINE Webhook  │  Discord Bot  │  Telegram Bot      │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│           Casper Orchestrator (api/orchestrator.py)        │
│  Input Sanitization → Iron Dome → Intent Classification   │
│  → Embedding Router → Skill Dispatch                      │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│                  Execution Layer                           │
│  oMLX / Ollama (local LLM)     │  57+ Skills  │  MCP     │
│  Embedding Router (ModernBERT)  │  Playwright   │  FAISS  │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│                   Data Layer                               │
│  magi_brain (local MariaDB)  │  law_firm_data (remote)    │
│  FAISS vector indices        │  NAS case folders           │
└──────────────────────────────────────────────────────────┘
```

### Inference Stack

#### macOS (Apple Silicon + oMLX)

| Model | Purpose | Quantization |
|-------|---------|-------------|
| **TAIDE-12b-Chat** | Chinese legal reasoning, translation, vision, general chat | MLX 4-bit |
| **Qwen2.5-Coder-14B** | Code generation, skill evolution | MLX 4-bit |
| **ModernBERT-embed** | Embedding router, semantic search | MLX 4-bit |
| **GLM-OCR** | Document OCR (PDF, images) | MLX bf16 |

#### Windows / Linux (Ollama + GGUF)

| Model | Purpose | Quantization |
|-------|---------|-------------|
| **TAIDE-8b-Chat** | Chinese legal reasoning | GGUF Q4 |
| **Qwen2.5-7b** | General chat, classification | GGUF Q4 |
| **Nomic-embed-text** | Embedding router, semantic search | GGUF |

---

## All Skills (57+)

Each skill follows a standard structure:

```
skills/{skill-name}/
├── SKILL.md       # Metadata, capabilities, usage
├── action.py      # CLI entry point (--task / --text)
└── *.py           # Supporting modules
```

### Legal Automation (14 skills)

| Skill | Description | Key Commands |
|-------|-------------|-------------|
| **`file-review-orchestrator`** | End-to-end court file review (閱卷): application submission, CAPTCHA solving (ddddocr+RapidOCR dual-engine), document download, payment tracking, case folder archival | `apply`, `download`, `payment`, `archive`, `probe` |
| **`laf-orchestrator`** | Legal Aid Foundation (法扶) case closing and go-live: activity counting, expense claim form auto-fill, document generation | `close`, `prepare`, `status` |
| **`laf-portal-automation`** | LAF portal form automation for 6 workflow types: case closing, go-live, status report, withdrawal, extension, fee claim. Human-in-the-loop with visual verification | `run_workflow`, `capture` |
| **`judicial-web-search`** | Taiwan Judicial Yuan ruling database crawler via Playwright, supports full-text search and Boolean queries | `search`, `download` |
| **`judicial-flow-search-archive`** | Natural language → Boolean query translation for judicial DB; full text download and archival to case folders | `search`, `archive` |
| **`judgment-collector`** | Supreme/High Administrative Court ruling auto-collection with structured LLM summaries. Includes URL dedup, hallucination detection, cache auto-cleanup | `collect`, `search`, `summary` |
| **`transcript-downloader`** | Court transcript auto-download from judicial portal, auto-rename by date/type, archive to NAS case folders | `download`, `rename`, `archive` |
| **`transcript-indexer`** | Transcript vector indexing using FAISS — semantic search by speaker, hearing date, or content | `index`, `search` |
| **`trial-prep`** | Court hearing preparation: query system calendar for upcoming hearings, scan case folders, cross-reference statutes and judgments, generate preparation memos | `upcoming`, `prepare`, `checklist`, `timeline` |
| **`brief-gen`** | Legal brief generation: 7 template types (complaint, answer, appeal, motion, closing argument, statement, labor). Auto-detects brief type, queries related statutes/judgments, exports to Word | `draft`, `template`, `enrich`, `export` |
| **`legal_attest`** | Registered mail letter generator (存證信函) — interactive questionnaire, outputs Taiwan postal PDF format | `generate`, `preview` |
| **`statutes-vdb`** | Statute vector database — auto-infers relevant laws by case type. FAISS-indexed semantic search | `search`, `index`, `info` |
| **`labor-law-calculator`** | Taiwan Labor Standards Act calculator: overtime pay, annual leave, severance. Pure statutory math | `overtime`, `leave`, `severance`, `verify` |
| **`law_review`** | Legal terminology review using TAIDE model — checks Taiwan legal conventions and formal style | `review` |

### Document Processing (7 skills)

| Skill | Description | Key Commands |
|-------|-------------|-------------|
| **`pdf`** | Swiss-army PDF tool: merge, split, extract text/tables/images, OCR, encrypt, decrypt, form-fill | `merge`, `split`, `extract`, `ocr`, `encrypt` |
| **`pdf-namer`** | Intelligent PDF renaming: OCR → vision model → auto-rename as `YYYY.MM.DD_Name_Type.pdf` | `rename`, `batch`, `learn` |
| **`pdf-annotator`** | Auto-generate PDF bookmarks and table of contents using vision models | `annotate`, `toc` |
| **`pdf-bookmarker`** | PDF bookmark management — add, edit, remove bookmarks programmatically | `add`, `list`, `remove` |
| **`docx`** | Word document creation, editing, template filling. Supports Taiwan legal formatting | `create`, `edit`, `template` |
| **`pptx`** | PowerPoint creation and editing with template support | `create`, `edit` |
| **`xlsx`** | Excel creation, editing, formula validation, data import/export | `create`, `edit`, `validate` |

### Financial Analysis (1 skill, 7 sub-commands)

| Sub-command | Description |
|-------------|-------------|
| **`market-briefing --task briefing`** | Daily stock prediction with self-tuning weighted linear model. Three modes: quick, technical, deep. Auto-pushes via Telegram |
| **`--task comps --text "台積電"`** | Comparable company analysis: auto-selects TWSE peers, fetches P/E, EPS, revenue YoY%, momentum |
| **`--task sector --text "半導體"`** | Sector analysis: 38 TWSE classifications, technical consensus, volume trends, ADX |
| **`--task export`** | Export watchlist to Excel/CSV with full technical indicators |
| **`--task performance`** | Model performance metrics: hit rate, MAE, per-stock breakdown |
| **`--task backtest`** | Cross-validated backtesting with auto parameter fitting |
| **`--task set/add/remove`** | Manage watchlist: TW stock names, codes, and US tickers |

**Data sources**: Yahoo Finance v8 chart API, TWSE OpenAPI (revenue/EPS), SEC EDGAR.

### System Intelligence (7 skills)

| Skill | Description | Key Commands |
|-------|-------------|-------------|
| **`memory`** | Long-term vector memory with RAG semantic search. FAISS-indexed, content MD5 dedup, bidirectional sync | `store`, `search`, `consolidate` |
| **`obsidian`** | Obsidian vault integration — extracts content from PDFs/DOCX with citations | `extract`, `sync`, `search` |
| **`brain_manager`** | Inference mode switching between local-only and distributed | `status`, `switch` |
| **`evolution`** | Self-evolution engine — generates new Python skills from natural language descriptions | `create`, `list`, `review` |
| **`magi-doctor`** | System self-diagnosis: skill validation, dependency verification, auto-repair | `diagnose`, `repair`, `report` |
| **`magi-autopilot`** | Nightly housekeeping: log rotation, cache cleanup, dead process patrol | `run`, `status` |
| **`iron-dome`** | Security core: rule scanning, prompt injection filtering, dangerous command blocking | `scan`, `update`, `status` |

### Communication & Utilities (7 skills)

| Skill | Description | Key Commands |
|-------|-------------|-------------|
| **`browser`** | Playwright/Selenium browser automation — headless/headed, screenshots, form filling | `navigate`, `screenshot`, `fill` |
| **`apple`** | Apple ecosystem integration: Calendar, Reminders, Notes, OCR (macOS only) | `calendar_upcoming`, `reminder`, `ocr` |
| **`translator`** | Full-text translation via local LLM. Supports text and webpage URL | `translate` |
| **`research`** | Multi-source research: RSS, GitHub monitor, web aggregation | `rss`, `github`, `web` |
| **`gmail-drafts`** | Gmail draft creation (draft-only, **never auto-sends**) | `create_draft`, `list` |
| **`worldmonitor-intel`** | Global event monitoring and intelligence gathering | `monitor`, `report` |
| **`crawler-targets`** | Managed URL target list for scheduled crawling | `add`, `list`, `remove` |

### Infrastructure — Bridge Modules (14 modules)

Located in `skills/bridge/`:

| Module | Description |
|--------|-------------|
| **`inference_gateway.py`** | Unified LLM routing — local first, fallback to remote, then cloud. Model alias mapping |
| **`embedding_router.py`** | ModernBERT cosine-similarity skill routing. 61 skills, 100% accuracy |
| **`intention_classifier.py`** | Three-stage classification: regex → heuristic → LLM |
| **`semantic_router.py`** | Legacy intent-based routing (embedding_router predecessor) |
| **`melchior_client.py`** | Remote inference gateway to Melchior node |
| **`iron_dome.py`** | Security filter — blocks dangerous SQL, shell commands, prompt injection |
| **`grounded_ai.py`** | Grounded response generation with source citations |
| **`code_analysis.py`** | Code analysis and review bridge |
| **`legal_bridge.py`** | Legal domain routing — case classification, statute lookup |
| **`casper_bridge.py`** | Main Casper orchestration bridge |
| **`melchior_bridge.py`** | Melchior node bridge — vision analysis (standby) |
| **`balthasar_bridge.py`** | Balthasar node bridge — summarization (standby) |
| **`watcher_bridge.py`** | Watcher node bridge — audit logging (standby) |
| **`tri_sage_collab.py`** | Three-sage collaborative reasoning |

### Infrastructure — Ops Modules (19 modules)

Located in `skills/ops/`:

| Module | Description |
|--------|-------------|
| **`platform_utils.py`** | **Cross-platform abstraction layer** — file locking, service management, hardware detection, executable discovery, calendar integration |
| **`red_phone.py`** | Multi-channel alert system (LINE + Discord + Telegram) |
| **`heartbeat.py`** | Node health monitoring with Tailscale guard |
| **`process_guardian.py`** | Process lifecycle — PID monitoring, zombie cleanup, auto-restart |
| **`db_sync.py`** | Bidirectional database synchronization |
| **`cron_scheduler.py`** | Cron job management from `cron_jobs.json` |
| **`openclaw_cron_runner.py`** | OpenClaw daily briefings and health checks |
| **`openclaw_updater.py`** | OpenClaw frontend auto-updater |
| **`file_review_auto_worker.py`** | Background court file review worker |
| **`system_test.py`** | End-to-end system validation |
| **`system_monitor.py`** | CPU, memory, disk, GPU monitoring |
| **`circuit_breaker.py`** | Circuit breaker for external API calls |
| **`structured_log.py`** | JSON structured logging with rotation |
| **`iron_dome_sync.py`** | Iron Dome rule sync from NAS |
| **`daily_reflection.py`** | Daily AI self-reflection |
| **`smart_summary.py`** | Multi-stage document compression |
| **`safe_state.py`** | Atomic JSON state file operations |
| **`export_text.py`** | Formatted text export for channels |
| **`task_tracker.py`** | Background task tracking — status, progress, cancellation |

### Self-Governance (3 modules)

Located in `skills/magi/`:

| Module | Description |
|--------|-------------|
| **`night_talk.py`** | Nightly discussion — three Sages review system health |
| **`local_council.py`** | Local consensus engine — Sage voting logic |
| **`council_approval.py`** | Proposal approval workflow |

### Legal Backend Engines

Located in `casper_ecosystem/law_firm_orchestrators/`:

| Engine | Description |
|--------|-------------|
| **`file_review_automation.py`** | Core file review engine: SSO login, CAPTCHA OCR, application, payment, archival |
| **`judicial_automation_v2.py`** | Judicial portal: transcript download, PDF extraction, case mapping |
| **`laf_automation_v2.py`** | Full LAF portal automation: upload/download, form filling, visual verification |
| **`laf_orchestrator.py`** | LAF workflow coordination |
| **`legalbridge_core.py`** | Core legal bridge — case DB, document management |
| **`osc/database.py`** | OSC database interface — case management CRUD |

---

## Message Processing Flow

```
Incoming message (LINE / Discord / Telegram)
    │
    ▼
Webhook handler (api/server.py)
    │  ─ Signature validation, role check, fast-path for probes
    │
    ▼
Background executor (async — LINE webhook must return < 3s)
    │
    ▼
Orchestrator (api/orchestrator.py)
    │  ─ Input sanitization
    │  ─ Iron Dome security check
    │  ─ Embedding Router (ModernBERT cosine similarity)
    │
    ▼
Intention Classifier (regex → heuristic → optional LLM)
    ├─ DANGER → Block + alert via red_phone
    ├─ CMD    → Execute skill via action.py
    ├─ QUERY  → ask_casper() with memory retrieval + web research
    └─ CHAT   → chat_casper() conversational mode
    │
    ▼
Response pushed back via channel API
```

---

## Governance & Security

MAGI's decision-making follows a strict hierarchy defined in `CONSTITUTION.md`:

```
User (Admin Token)           ← Supreme authority
  └── Constitution           ← Overrides all AI logic
      └── Casper (Governor)  ← AI authority
          └── Iron Dome      ← Hard protection layer
```

### Iron Dome

- **SQL Protection**: Hard blocks on `DELETE` / `DROP` / `TRUNCATE`
- **Shell Protection**: Blocks `rm -rf`, `mkfs`, `dd`, destructive commands
- **Prompt Injection**: Pattern-based filtering
- **Guest Containment**: Non-admin users are read-only

### Nightly Council

Every day at 03:00 AM: Synology sync → Night talk → Consensus vote → Report.

---

## Configuration

### Guided Setup (Recommended)

```bash
python3 setup_wizard.py
```

The wizard auto-generates `.env` with hardware-appropriate defaults.

### Manual Setup

Copy `.env.example` to `.env` and configure:

| Category | Variables | Notes |
|----------|-----------|-------|
| **Flask** | `FLASK_SECRET_KEY`, `MAGI_API_KEY` | Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| **Database** | `DB_HOST`, `DB_USER`, `DB_PASSWORD` | `magi_brain` — vector memory |
| **LINE** | `MAGI_LINE_CHANNEL_ACCESS_TOKEN`, `MAGI_LINE_CHANNEL_SECRET` | LINE Messaging API |
| **Admin** | `MAGI_ADMIN_DISPLAY_NAME`, `MAGI_ADMIN_LINE_IDS` | LINE user IDs |
| **Models** | `MAGI_MAIN_MODEL` | `taide-12b` (macOS) / `taide-lx-7b-chat` (Windows) |
| **Inference** | `MAGI_OMLX_ENABLED` | `1` for oMLX (macOS), `0` for Ollama |

See [.env.example](.env.example) for the complete list.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ (core), Node.js v22 (OpenClaw frontend) |
| Web Framework | Flask + Jinja2 |
| Database | MariaDB 10.11+ |
| Inference | oMLX (macOS) / Ollama (Windows/Linux) — Ollama-compatible API |
| Embedding | ModernBERT (oMLX) / Nomic-embed (Ollama) |
| Messaging | LINE Bot SDK, Discord.py, python-telegram-bot |
| Network | Tailscale VPN, Cloudflare Tunnel (auto-managed) |
| Browser | Playwright, Selenium |
| PDF/OCR | PyMuPDF, RapidOCR, pdfplumber, ReportLab |
| Vector DB | FAISS (local) |
| Scheduling | LaunchAgent (macOS) / Task Scheduler (Windows) / systemd (Linux) |
| Platform Layer | `skills/ops/platform_utils.py` — cross-platform abstraction |

---

## Repository Layout

```
MAGI/
├── api/                              # Flask server, orchestrator, Tools API
│   ├── server.py                     # Main entry — LINE webhook, dashboard (port 5002)
│   ├── orchestrator.py               # Intent routing and skill dispatch
│   ├── tools_api.py                  # RESTful tool API (port 5003)
│   ├── discord_bot.py                # Discord integration
│   ├── runtime_paths.py              # Cross-platform path resolution
│   └── handlers/                     # Modular request handlers
├── skills/                           # 57+ modular skills
│   ├── bridge/                       # Inference gateway, routing, security (14 modules)
│   ├── ops/                          # Operations + platform abstraction (19 modules)
│   │   └── platform_utils.py         # Cross-platform abstraction layer
│   ├── magi/                         # Self-governance (3 modules)
│   ├── memory/                       # FAISS vector memory + RAG
│   ├── definitions.json              # Central skill registry
│   └── {skill-name}/                 # Individual skill modules
├── casper_ecosystem/                  # Legal automation engines
│   └── law_firm_orchestrators/
├── scripts/                           # Cron jobs and automation
│   └── install_service.py             # Cross-platform service installer
├── templates/
│   └── wizard/                        # Setup Wizard HTML templates
├── setup_wizard.py                    # First-time setup GUI (hardware detection + .env gen)
├── daemon.py                          # Process guardian daemon (cross-platform)
├── start_magi.sh                      # macOS / Linux startup script
├── start_magi.bat                     # Windows startup script
├── requirements.txt                   # Core Python dependencies
├── requirements-optional.txt          # Optional skill dependencies
├── requirements-windows.txt           # Windows-specific dependencies
├── .env.example                       # Environment variable template
└── CONSTITUTION.md                    # Governance rules
```

---

## Ports

| Port | Service | Access |
|------|---------|--------|
| 5002 | LINE Webhook + Dashboard | localhost (via Caddy) |
| 5003 | Tools API | localhost |
| 8080 | oMLX / Ollama inference | localhost |
| 8081 | Embedding service | localhost |
| 8199 | Setup Wizard (temporary) | localhost |
| 18789 | OpenClaw Gateway | loopback only |

---

## Testing

```bash
# Full test suite
python -m pytest tests/ -v

# Smoke tests
python -m pytest tests/smoke_*.py -v

# System self-test
python3 skills/ops/system_test.py

# Market briefing
python3 skills/market-briefing/action.py --task briefing --force 1 --mode quick

# Legal skills
python3 skills/trial-prep/action.py --task upcoming --days 7
python3 skills/brief-gen/action.py --task template
python3 skills/market-briefing/action.py --task comps --text "台積電"
```

---

## License

No open-source license. All rights reserved until a LICENSE file is published.
