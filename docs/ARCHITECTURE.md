# MAGI Architecture Overview

版本：v1.0 | 日期：2026-03-19

---

## System Architecture

```
                    ┌─────────────────────────────────────────┐
                    │              External Clients            │
                    │   LINE Bot  │  Discord  │  Telegram     │
                    │   Web UI    │  OpenClaw │  MCP Client   │
                    └──────┬──────┴─────┬─────┴──────┬────────┘
                           │            │            │
                    ┌──────▼────────────▼────────────▼────────┐
                    │           CASPER (Port 5002)             │
                    │   ┌──────────────────────────────────┐  │
                    │   │  Flask App (api/server.py)        │  │
                    │   │  ├─ LINE Webhook Handler          │  │
                    │   │  ├─ Discord Bot                   │  │
                    │   │  ├─ Telegram Handler              │  │
                    │   │  ├─ Web Dashboard                 │  │
                    │   │  ├─ Auth (Flask-Login + API Key)  │  │
                    │   │  ├─ CSRF Guard                    │  │
                    │   │  └─ Security Headers              │  │
                    │   └──────────┬───────────────────────┘  │
                    │              │                           │
                    │   ┌──────────▼───────────────────────┐  │
                    │   │  Orchestrator                     │  │
                    │   │  (api/orchestrator.py)            │  │
                    │   │  ├─ NL Router (意圖分類)          │  │
                    │   │  ├─ Skill Dispatcher              │  │
                    │   │  ├─ Job Queue                     │  │
                    │   │  └─ Inference Gateway             │  │
                    │   └──────────┬───────────────────────┘  │
                    │              │                           │
                    │   ┌──────────▼───────────────────────┐  │
                    │   │  Skills (skills/*)                │  │
                    │   │  ├─ pdf-namer        (PDF 命名)   │  │
                    │   │  ├─ judgment-collector (裁判收集)  │  │
                    │   │  ├─ memory           (RAG 記憶)   │  │
                    │   │  ├─ research         (搜尋研究)   │  │
                    │   │  ├─ market-briefing  (股市晨報)   │  │
                    │   │  ├─ magi-autopilot   (自動巡檢)   │  │
                    │   │  ├─ magi-doctor      (自我診斷)   │  │
                    │   │  └─ ... (20+ skills)              │  │
                    │   └──────────────────────────────────┘  │
                    └──────────────────┬──────────────────────┘
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            │                          │                          │
     ┌──────▼──────┐           ┌───────▼──────┐          ┌───────▼──────┐
     │  Tools API  │           │   MariaDB    │          │   Ollama     │
     │ (Port 5003) │           │ (magi_brain) │          │  (LLM Host)  │
     │  ├─ /search │           │ ├─ users     │          │ ├─ taide-12b │
     │  ├─ /vision │           │ ├─ cases     │          │ ├─ llama3.1  │
     │  ├─ /fetch  │           │ ├─ memories  │          │ └─ minicpm-v │
     │  └─ /skills │           │ └─ judgments │          └──────────────┘
     └─────────────┘           └──────────────┘

            ┌──────────────────────────────────────────────────────┐
            │               Federation (Optional)                  │
            │  ┌─────────┐  ┌───────────┐  ┌─────────────────┐   │
            │  │BALTHASAR│  │ MELCHIOR  │  │    WATCHER      │   │
            │  │(Summary)│  │ (Vision)  │  │ (Security Audit)│   │
            │  │Apple AI │  │ GPU Node  │  │ Audit Node      │   │
            │  └─────────┘  └───────────┘  └─────────────────┘   │
            └──────────────────────────────────────────────────────┘
```

---

## Component Responsibilities

| Component | Port | Role | Required? |
|-----------|------|------|-----------|
| CASPER (server.py) | 5002 | Main app: channels, web UI, orchestration | Yes |
| Tools API (tools_api.py) | 5003 | HTTP API for external callers | Yes |
| MariaDB | 3306 | Persistent storage | Yes |
| Ollama | 11434 | Local LLM inference | Yes |
| BALTHASAR | 5002 | Apple Intelligence summarization | No |
| MELCHIOR | 5002 | GPU vision/code analysis | No |
| WATCHER | 5010 | Security audit monitoring | No |

---

## Data Flow

```
User Message → Channel Handler → Orchestrator → NL Router
                                                    │
                                    ┌───────────────┼───────────────┐
                                    ▼               ▼               ▼
                               Skill Action    Inference       Direct Response
                                    │           Gateway              │
                                    ▼               │                │
                               DB / File            ▼                │
                               Operations      LLM (Ollama)         │
                                    │               │                │
                                    └───────┬───────┘                │
                                            ▼                        │
                                    Response Formatter               │
                                            │                        │
                                            ▼                        │
                                    Channel Delivery ◄───────────────┘
                                            │
                                            ▼
                                     User Response
```

---

## Security Architecture

```
Request → Rate Limiter → CORS Check → CSRF Validation
              │               │              │
              ▼               ▼              ▼
         Auth Check    Security Headers   Audit Log
         (Session /     (X-Frame-Options,  (endpoint,
          API Key)       X-XSS, etc.)      user, role)
              │
              ▼
         Authz Check
         (@require_role /
          @require_api_key)
              │
              ▼
         Route Handler
```

---

## Directory Structure

```
MAGI/
├── api/                    # Core API layer
│   ├── server.py           # Main Flask app (5002)
│   ├── tools_api.py        # Tools API (5003)
│   ├── orchestrator.py     # NL routing engine
│   ├── runtime_paths.py    # Path abstraction layer
│   ├── authz.py            # Unified authorization
│   ├── csrf_guard.py       # CSRF protection
│   ├── blueprints/         # Modular route groups
│   └── thread_pools.py     # Shared executors
├── bin/                    # Standard entry points
│   ├── bootstrap           # First-time install
│   ├── start               # Start services
│   ├── check               # Health diagnostics
│   ├── release             # Build release
│   ├── upgrade             # Upgrade to new version
│   └── rollback            # Rollback to previous
├── skills/                 # Pluggable skill modules
│   ├── ops/                # Operations (config, cron, logging)
│   ├── pdf-namer/          # PDF classification & naming
│   ├── judgment-collector/  # Judicial data collection
│   ├── memory/             # RAG memory system
│   ├── research/           # Web search & research
│   ├── bridge/             # Federation bridges
│   └── ...
├── migrations/             # DB schema management
│   ├── migrate.py          # Migration runner
│   └── versions/           # Ordered SQL migrations
├── casper_ecosystem/       # LAF automation subsystem
├── tests/                  # Test suite
├── docs/                   # Documentation
│   ├── OPERATOR_RUNBOOK.md
│   ├── API_CONTRACT.md
│   ├── ARCHITECTURE.md
│   ├── THIRD_PARTY_BOM.md
│   ├── PRIVACY_POLICY.md
│   └── DATA_RETENTION_POLICY.md
├── templates/              # Web UI (Jinja2)
├── static/                 # Static assets
├── .github/workflows/      # CI pipeline
├── pyproject.toml          # Package metadata
├── .env.example            # Config template
└── LICENSE                 # Commercial license
```

---

## Deployment Modes

| Mode | Description | Phase |
|------|-------------|-------|
| Single-node dev | All on one machine | Current |
| Single-tenant managed | Dedicated host per customer | Phase 1 target |
| Multi-node federation | CASPER + BALTHASAR + MELCHIOR | Supported |
| Multi-tenant SaaS | Shared infrastructure | Future (not Phase 1) |

---

## Support Matrix

| Feature | Status | Since |
|---------|--------|-------|
| LINE Bot channel | Production | v0.1 |
| Discord Bot channel | Production | v0.5 |
| Telegram Bot channel | Production | v0.8 |
| Web Dashboard | Production | v0.3 |
| PDF naming (pdf-namer) | Production | v0.2 |
| Judicial data collection | Production | v0.4 |
| RAG memory | Production | v0.6 |
| Market briefing | Production | v0.7 |
| Federation (multi-node) | Beta | v0.9 |
| MCP Server | Beta | v1.0 |
| Browser automation (LAF) | Controlled | v0.5 |
| Insecure SSL fallback | Opt-in only | v1.0 |
