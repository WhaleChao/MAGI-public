# MAGI — Multi-Agent Governance Infrastructure

[繁體中文版](README.zh-TW.md)

MAGI v2 is a locally-deployed AI operations platform built for a Taiwanese law firm. This is a ground-up rewrite of MAGI v1, adding enterprise-grade infrastructure (permissions, events, hooks, tasks, sessions, tool registry, multi-agent runtime, provider abstraction) and a low-hallucination architecture. It runs entirely on a single node, combining a Flask control plane, 67+ modular skill runners, scheduled workers, local LLM inference, and deep legal workflow automation in one repository.

**Cross-platform**: Runs on **macOS** (Apple Silicon via oMLX) and **Windows** (NVIDIA/CPU via Ollama). A built-in Setup Wizard detects your hardware, recommends models, and generates configuration automatically.

> **Single-node by default.** The codebase retains distributed inference scaffolding (Melchior, Balthasar) but all production workloads run locally on Casper. Set `MAGI_AVOID_DISTRIBUTED=0` to re-enable multi-node inference.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Setup Wizard](#setup-wizard)
- [Platform Support](#platform-support)
- [Architecture](#architecture)
- [Operations (`magi` CLI)](#operations-magi-cli)
- [All Skills (67+)](#all-skills-67)
  - [Legal Automation (14 skills)](#legal-automation-14-skills)
  - [Document Processing (7 skills)](#document-processing-7-skills)
  - [Financial Analysis (1 skill, 7 sub-commands)](#financial-analysis-1-skill-7-sub-commands)
  - [System Intelligence (7 skills)](#system-intelligence-7-skills)
  - [Communication & Utilities (7 skills)](#communication--utilities-7-skills)
  - [Infrastructure — Bridge Modules (14 modules)](#infrastructure--bridge-modules-14-modules)
  - [Infrastructure — Ops Modules (19 modules)](#infrastructure--ops-modules-19-modules)
  - [Self-Governance (3 modules)](#self-governance-3-modules)
- [Message Processing Flow](#message-processing-flow)
- [Registry System](#registry-system)
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
magi status                              # Full system dashboard
```

---

## Setup Wizard

First-time users are guided through a web-based setup wizard that:

1. **Hardware Detection** — auto-detects CPU, GPU (Metal/CUDA), RAM, disk space
2. **Engine Check** — verifies oMLX (macOS) or Ollama (Windows/Linux) installation
3. **Model Recommendation** — suggests optimal models based on your hardware:
   - Apple Silicon (>=16 GB): TAIDE-12b (text+vision) + Coder-14B + ModernBERT + GLM-OCR
   - NVIDIA GPU (>=8 GB): TAIDE-8b GGUF + Qwen2.5-7b + Nomic-embed
   - CPU-only (>=8 GB): Lightweight GGUF models
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
| Status Bar | `gui/magi_menubar.py` (rumps) | — | — |

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

### Modular Architecture (v2)

The v2 architecture splits the original monolith files into focused modules:

```
┌──────────────────────────────────────────────────────────────┐
│                        Channels                               │
│   LINE Webhook     │  Discord Bot  │  Telegram Bot            │
│  (webhooks/line.py)│(discord_bot.py)│(webhooks/telegram.py)   │
└─────────┬──────────┴──────┬────────┴─────────┬───────────────┘
          │                 │                   │
┌─────────▼─────────────────▼───────────────────▼───────────────┐
│              Flask App (api/server.py — 802 lines)             │
│  Blueprints: admin_runtime │ dashboard │ osc_cases │ web      │
│  Webhooks:   line.py       │ telegram.py                       │
│  Startup:    thread pools, security headers, CSRF              │
└─────────────────────────────┬─────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│           Orchestrator (api/orchestrator.py — 2335 lines)      │
│  Delegates to:                                                  │
│  ├─ pipelines/message_pipeline.py    (message intake)          │
│  ├─ pipelines/command_pipeline.py    (command parsing)         │
│  ├─ pipelines/chat_pipeline.py       (conversational AI)       │
│  ├─ pipelines/command_dispatch.py    (skill invocation)        │
│  ├─ pipelines/skill_dispatch.py      (skill resolution)        │
│  ├─ pipelines/message_router.py      (intent routing)          │
│  ├─ pipelines/attachment_pipeline.py (file handling)            │
│  └─ domains/{codex,judgment,laf,market,memory}_flow.py         │
└─────────────────────────────┬─────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│                    Routing Layer (api/routing/)                 │
│  Registry:  service_registry │ model_registry │ node_registry  │
│  Policy:    policy_engine    │ route_policy   │ route_decision │
│  Routers:   request_router   │ inference_router                │
│  Context:   routing_context  │ telemetry                       │
│  Config:    json/services.json │ models.json │ nodes.json      │
└─────────────────────────────┬─────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│                    Execution Layer                              │
│  oMLX / Ollama (local LLM)     │  67+ Skills  │  MCP Server   │
│  Embedding Router (ModernBERT)  │  Playwright   │  FAISS        │
└─────────────────────────────┬─────────────────────────────────┘
                              │
┌─────────────────────────────▼─────────────────────────────────┐
│                      Data Layer                                │
│  magi_brain (local MariaDB)  │  law_firm_data (remote)         │
│  FAISS vector indices        │  NAS case folders               │
│  DB Failover (auto-switch remote ↔ local with mysqldump sync)  │
└───────────────────────────────────────────────────────────────┘
```

### Key Refactoring Summary

| Original File | Lines | After | Strategy |
|--------------|-------|-------|----------|
| `api/server.py` | 9,463 | 802 | Split into `blueprints/` (7 modules) + `webhooks/` (2 modules) |
| `api/orchestrator.py` | 10,269 | 2,335 | Split into `pipelines/` (8 modules) + `domains/` (6 modules) |
| `templates/osc.html` | 7,558 | 2,543 | Split into `osc/` template partials |
| Hardcoded values | scattered | 0 | Externalized to `json/` configs + `api/routing/` registries |

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

## Operations (`magi` CLI)

The `magi` command manages the full MAGI lifecycle including daemon, all services, and the macOS status bar.

### Installation

```bash
cp scripts/magi_cli.sh /opt/homebrew/bin/magi && chmod +x /opt/homebrew/bin/magi
```

### Commands

```bash
magi                 # Show full system status (default)
magi status          # Same — shows services, nodes, NAS, DB, zombies, memory
magi start           # Start daemon + status bar via launchctl
magi stop            # Stop daemon + all services + status bar
magi restart         # Full stop → start cycle
magi menubar         # Restart only the macOS status bar
magi zombie          # Detect and clean zombie processes
```

### Status Dashboard

`magi status` displays a comprehensive real-time overview:

```
═══ MAGI System Status ═══

Core Services:
  ● Daemon             PID 4272
  ● Server             PID 4358
  ● Discord Bot        PID 4359
  ● Tools API          PID 4361

UI:
  ● Status Bar         PID 4275

oMLX Inference:
  ● Text (TAIDE)       port 8080  PID 1234
  ● Embed (BERT)       port 8081  PID 1235

Remote Nodes:
  ● Melchior           100.116.54.16:8080
  ○ Balthasar          100.118.235.126:5002  DOWN
  ● Keeper             100.121.61.74:3306

NAS Mounts:
  ● homes              1.2T/3.6T (34%)
  ● lumi               800G/1.8T (45%)

Database:
  ● 雙活同步 (remote+local)

Zombies: 0
Memory:  ~2.3GB (MAGI + oMLX)
```

### macOS Status Bar

The status bar (`gui/magi_menubar.py`) runs as a macOS menu bar app showing real-time system health:

- **Service status**: Daemon, Server, Discord Bot, Tools API — with colored indicators
- **Remote nodes**: Melchior, Balthasar, Keeper — TCP + HTTP health checks
- **Cron jobs**: Per-job last-run timestamps with staleness detection (31 scheduled jobs)
- **NAS mounts**: Per-share mount status with disk usage via `os.statvfs()`
- **Database**: Failover detail (remote+local dual-active, failover status, sync state)
- **oMLX inference**: Text and embedding model status with port checks

### LaunchAgent Management

MAGI uses macOS LaunchAgents for process lifecycle:

| Agent | Label | Purpose |
|-------|-------|---------|
| Daemon | `com.magi.daemon` | Master process (spawns server, discord, tools_api) |
| Status Bar | `com.magi.menubar` | macOS menu bar health monitor |
| oMLX Text | `com.magi.omlx` | TAIDE-12b inference (port 8080) |
| oMLX Embed | `com.magi.omlx-embed` | ModernBERT embedding (port 8081) |
| DB Proxy | `com.magi.db-proxy` | SSH tunnel to remote MariaDB |
| SMB Reconnect | `com.magi.smb-reconnect` | NAS auto-reconnect on network change |
| Caddy | `com.magi.caddy-openclaw` | Reverse proxy for OpenClaw |

---

## All Skills (67+)

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
| **`file-review-orchestrator`** | End-to-end court file review: application submission, CAPTCHA solving (ddddocr+RapidOCR dual-engine), document download, payment tracking, case folder archival | `apply`, `download`, `payment`, `archive`, `probe` |
| **`laf-orchestrator`** | Legal Aid Foundation case closing and go-live: activity counting, expense claim form auto-fill, document generation | `close`, `prepare`, `status` |
| **`laf-portal-automation`** | LAF portal form automation for 6 workflow types: case closing, go-live, status report, withdrawal, extension, fee claim. Human-in-the-loop with visual verification | `run_workflow`, `capture` |
| **`judicial-web-search`** | Taiwan Judicial Yuan ruling database crawler via Playwright, supports full-text search and Boolean queries | `search`, `download` |
| **`judicial-flow-search-archive`** | Natural language to Boolean query translation for judicial DB; full text download and archival to case folders | `search`, `archive` |
| **`judgment-collector`** | Supreme/High Administrative Court ruling auto-collection with structured LLM summaries. Includes URL dedup, hallucination detection, cache auto-cleanup | `collect`, `search`, `summary` |
| **`transcript-downloader`** | Court transcript auto-download from judicial portal, auto-rename by date/type, archive to NAS case folders | `download`, `rename`, `archive` |
| **`transcript-indexer`** | Transcript vector indexing using FAISS — semantic search by speaker, hearing date, or content | `index`, `search` |
| **`trial-prep`** | Court hearing preparation: query system calendar for upcoming hearings, scan case folders, cross-reference statutes and judgments, generate preparation memos | `upcoming`, `prepare`, `checklist`, `timeline` |
| **`brief-gen`** | Legal brief generation: 7 template types (complaint, answer, appeal, motion, closing argument, statement, labor). Auto-detects brief type, queries related statutes/judgments, exports to Word | `draft`, `template`, `enrich`, `export` |
| **`legal_attest`** | Registered mail letter generator — interactive questionnaire, outputs Taiwan postal PDF format | `generate`, `preview` |
| **`statutes-vdb`** | Statute vector database — auto-infers relevant laws by case type. FAISS-indexed semantic search | `search`, `index`, `info` |
| **`labor-law-calculator`** | Taiwan Labor Standards Act calculator: overtime pay, annual leave, severance. Pure statutory math | `overtime`, `leave`, `severance`, `verify` |
| **`law_review`** | Legal terminology review using TAIDE model — checks Taiwan legal conventions and formal style | `review` |

### Document Processing (7 skills)

| Skill | Description | Key Commands |
|-------|-------------|-------------|
| **`pdf`** | Swiss-army PDF tool: merge, split, extract text/tables/images, OCR, encrypt, decrypt, form-fill | `merge`, `split`, `extract`, `ocr`, `encrypt` |
| **`pdf-namer`** | Intelligent PDF renaming: OCR + vision model + auto-rename as `YYYY.MM.DD_Name_Type.pdf` | `rename`, `batch`, `learn` |
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
| **`intention_classifier.py`** | Three-stage classification: regex + heuristic + LLM |
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
Channel handler (webhooks/line.py, webhooks/telegram.py, discord_bot.py)
    │  ─ Signature validation, role check, fast-path for probes
    │
    ▼
Background executor (async — LINE webhook must return < 3s)
    │
    ▼
Orchestrator (api/orchestrator.py) → delegates to pipelines/
    │
    ├─ message_pipeline.py     ─ Input sanitization, context loading
    ├─ command_pipeline.py     ─ Command prefix detection & parsing
    ├─ message_router.py       ─ Embedding Router (ModernBERT cosine similarity)
    └─ command_dispatch.py     ─ Skill resolution & invocation
        │
        ▼
    Intention Classifier (regex → heuristic → optional LLM)
        ├─ DANGER → Block + alert via red_phone
        ├─ CMD    → skill_dispatch.py → action.py
        ├─ QUERY  → chat_pipeline.py (ask_casper with memory retrieval + web research)
        └─ CHAT   → chat_pipeline.py (conversational mode)
        │
        ▼
    Domain-specific flows (domains/):
        ├─ judgment_flow.py   ─ Judicial ruling queries
        ├─ laf_flow.py        ─ Legal Aid Foundation operations
        ├─ market_flow.py     ─ Stock market analysis
        ├─ memory_flow.py     ─ RAG memory operations
        └─ codex_flow.py      ─ Code analysis requests
        │
        ▼
    Response pushed back via channel API
```

---

## Registry System

MAGI v2 externalizes all hardcoded values (IPs, ports, model names, connection strings) into a declarative JSON + Python registry system. Every registry follows the same pattern: **JSON config → Python singleton module → environment variable override → hardcoded fallback**.

### JSON Configuration Files (`json/`)

| File | Purpose | Example Entry |
|------|---------|---------------|
| `services.json` | Service endpoints (host, port, path) | `{"casper": {"host": "127.0.0.1", "port": 5002}}` |
| `models.json` | Model aliases, providers, parameters | `{"taide-12b": {"provider": "omlx", "ctx": 4096}}` |
| `nodes.json` | Execution nodes (IP, role, health URL) | `{"melchior": {"ip": "100.116.54.16", "role": "vision"}}` |
| `datastores.json` | Database and storage connections | `{"magi_brain": {"host": "127.0.0.1", "port": 3306}}` |

### Python Registry Modules (`api/routing/`)

| Module | Functions | Reads From |
|--------|-----------|-----------|
| `service_registry.py` | `get_service()`, `get_service_url()`, `get_service_host_port()` | `json/services.json` |
| `model_registry.py` | `get_role_model()`, `resolve_model()`, `is_alias()` | `json/models.json` |
| `node_registry.py` | `get_node()`, `get_node_ip()`, `get_node_url()` | `json/nodes.json` |
| `datastore_registry.py` | `get_datastore()`, `get_connection_params()` | `json/datastores.json` |

### Override Chain

```
Environment variable (MAGI_CASPER_PORT=5002)
    → JSON config (json/services.json)
        → Hardcoded fallback (in registry module)
```

### Unified Routing (Phase 4)

| Module | Role |
|--------|------|
| `context.py` | `RoutingContext` — per-request state |
| `models.py` | `RoutingDecision`, `FallbackPlan`, `ServiceTarget` |
| `policy_engine.py` | `PolicyEngine` — applies routing rules |
| `request_router.py` | `RequestRouter` — routes HTTP requests to services |
| `inference_router.py` | `InferenceRouter` — routes LLM calls to providers |
| `telemetry.py` | `RoutingTelemetry` — observability and metrics |

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
| Database | MariaDB 10.11+ (dual-active failover: remote + local) |
| Inference | oMLX (macOS) / Ollama (Windows/Linux) — Ollama-compatible API |
| Embedding | ModernBERT (oMLX) / Nomic-embed (Ollama) |
| Messaging | LINE Bot SDK, Discord.py, python-telegram-bot |
| Network | Tailscale VPN, Cloudflare Tunnel (auto-managed) |
| Browser | Playwright, Selenium |
| PDF/OCR | PyMuPDF, RapidOCR, pdfplumber, ReportLab |
| Vector DB | FAISS (local) |
| Scheduling | LaunchAgent (macOS) / Task Scheduler (Windows) / systemd (Linux) |
| Status Bar | rumps + PyObjC (macOS menu bar) |
| Platform Layer | `skills/ops/platform_utils.py` — cross-platform abstraction |

---

## Repository Layout

```
MAGI/
├── api/                              # Core API layer
│   ├── server.py                     # Flask app entry (802 lines — delegates to modules)
│   ├── orchestrator.py               # Intent routing hub (2335 lines — delegates to pipelines)
│   ├── tools_api.py                  # RESTful tool API (port 5003)
│   ├── discord_bot.py                # Discord integration + cron scheduler
│   ├── db_failover.py                # DB failover controller (remote ↔ local auto-switch)
│   ├── runtime_paths.py              # Cross-platform path resolution
│   ├── blueprints/                   # Flask blueprint modules (split from server.py)
│   │   ├── admin_runtime.py          # Admin dashboard routes
│   │   ├── dashboard_pages.py        # Dashboard page routes
│   │   ├── osc_cases.py              # Case management system routes
│   │   ├── osc_accounting.py         # Accounting system routes
│   │   ├── osc_debt.py               # Debt case routes
│   │   ├── osc_settings.py           # System settings routes
│   │   └── web_runtime.py            # Web application routes
│   ├── webhooks/                     # Channel webhook handlers (split from server.py)
│   │   ├── line.py                   # LINE messaging webhook
│   │   └── telegram.py               # Telegram bot webhook
│   ├── pipelines/                    # Processing pipelines (split from orchestrator.py)
│   │   ├── message_pipeline.py       # Message intake & sanitization
│   │   ├── command_pipeline.py       # Command parsing & validation
│   │   ├── chat_pipeline.py          # Conversational AI pipeline
│   │   ├── command_dispatch.py       # Skill invocation dispatch
│   │   ├── skill_dispatch.py         # Skill resolution logic
│   │   ├── message_router.py         # Intent-based message routing
│   │   ├── attachment_pipeline.py    # File attachment processing
│   │   └── specialized_commands.py   # Domain-specific command handlers
│   ├── domains/                      # Domain-specific flows (split from orchestrator.py)
│   │   ├── judgment_flow.py          # Judicial ruling queries
│   │   ├── laf_flow.py              # Legal Aid Foundation operations
│   │   ├── market_flow.py           # Stock market analysis
│   │   ├── memory_flow.py           # RAG memory operations
│   │   ├── codex_flow.py            # Code analysis flow
│   │   └── skill_interview_flow.py  # Skill capability queries
│   ├── routing/                      # Unified routing & registry system
│   │   ├── service_registry.py      # Service endpoint registry
│   │   ├── model_registry.py        # Model alias & provider registry
│   │   ├── node_registry.py         # Execution node registry
│   │   ├── datastore_registry.py    # Database connection registry
│   │   ├── policy_engine.py         # Routing policy engine
│   │   ├── request_router.py        # HTTP request router
│   │   ├── inference_router.py      # LLM inference router
│   │   ├── context.py               # Per-request routing context
│   │   ├── models.py                # Routing data models
│   │   ├── telemetry.py             # Routing telemetry
│   │   ├── route_decision.py        # Route decision builder
│   │   ├── route_explanations.py    # Routing explanation collector
│   │   └── route_policy.py          # Skill dispatch policies
│   ├── handlers/                     # Modular request handlers
│   ├── agents/                       # Agent runtime implementations
│   ├── coordinator/                  # Task coordination
│   ├── events/                       # Event handling system
│   ├── hooks/                        # Hook system
│   ├── osc/                          # Online Service Center integrations
│   ├── permissions/                  # Authorization & permissions
│   ├── session/                      # Session management
│   ├── tasks/                        # Task queue & execution
│   ├── tools/                        # Tool definitions & registry
│   └── verification/                 # Response verification & validation
├── json/                             # Declarative configuration (Registry system)
│   ├── services.json                 # Service endpoints
│   ├── models.json                   # Model definitions & aliases
│   ├── nodes.json                    # Execution node definitions
│   ├── datastores.json               # Database connection configs
│   └── holidays_config.json          # Holiday calendar data
├── skills/                           # 67+ modular skills
│   ├── bridge/                       # Inference gateway, routing, security (14 modules)
│   ├── ops/                          # Operations + platform abstraction (19 modules)
│   │   └── platform_utils.py         # Cross-platform abstraction layer
│   ├── magi/                         # Self-governance (3 modules)
│   ├── memory/                       # FAISS vector memory + RAG
│   ├── definitions.json              # Central skill registry
│   └── {skill-name}/                 # Individual skill modules
├── gui/                              # GUI components
│   └── magi_menubar.py               # macOS status bar (rumps + PyObjC)
├── scripts/                          # Operational scripts (60+)
│   ├── magi_cli.sh                   # `magi` CLI tool
│   ├── nightly_council.py            # Daily knowledge consolidation
│   ├── casper_night_patrol.py        # Automated validation runner
│   ├── memory_consolidation.py       # Memory system optimization
│   └── ops/                          # Operations scripts (smoke tests, DB sync, etc.)
├── casper_ecosystem/                  # Legal automation engines
│   └── law_firm_orchestrators/
├── providers/                         # AI provider integrations
│   ├── anthropic/                     # Claude API
│   ├── openai/                        # OpenAI API
│   ├── ollama/                        # Ollama local inference
│   └── omlx/                          # oMLX Apple Silicon inference
├── mcp/                               # MCP server implementation
│   └── magi_mcp_server.py
├── tests/                             # 90+ test files
│   ├── eval/                          # Evaluation tests
│   ├── smoke_*.py                     # Smoke tests
│   └── test_*.py                      # Unit & integration tests
├── docs/                              # Documentation
│   ├── ARCHITECTURE.md                # System architecture
│   ├── OPERATOR_RUNBOOK.md            # Operations manual
│   ├── API_CONTRACT.md                # API specifications
│   ├── ENV_REFERENCE.md               # Environment variable reference
│   └── SECURITY_INTEGRATION_GUIDE.md  # Security guide
├── migrations/                        # Database migration scripts
├── templates/                         # Flask/Jinja2 templates
│   ├── osc/                           # Case management system templates
│   └── wizard/                        # Setup wizard templates
├── static/                            # Static assets (CSS, JS, images)
├── setup_wizard.py                    # First-time setup GUI
├── daemon.py                          # Process guardian daemon
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
# Full test suite (247 tests)
python -m pytest tests/ -v

# Smoke tests
python -m pytest tests/smoke_*.py -v

# Registry & routing tests
python -m pytest tests/test_registry*.py tests/test_routing*.py -v

# Blueprint tests
python -m pytest tests/test_*_blueprint.py -v

# Pipeline tests
python -m pytest tests/test_*_pipeline.py tests/test_command_dispatch.py -v

# Skill contract tests
python -m pytest tests/test_skill_contract_*.py -v

# System self-test
python3 skills/ops/system_test.py

# Market briefing
python3 skills/market-briefing/action.py --task briefing --force 1 --mode quick

# Legal skills
python3 skills/trial-prep/action.py --task upcoming --days 7
python3 skills/brief-gen/action.py --task template
```

---

## License

No open-source license. All rights reserved until a LICENSE file is published.
