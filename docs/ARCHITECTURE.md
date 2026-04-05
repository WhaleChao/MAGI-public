# MAGI Architecture Overview

版本：v2.0 | 日期：2026-04-05

---

## System Architecture (v2 Modular)

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
                    │   │  Flask App (api/server.py — 802)  │  │
                    │   │  ├─ blueprints/admin_runtime.py   │  │
                    │   │  ├─ blueprints/dashboard_pages.py │  │
                    │   │  ├─ blueprints/osc_cases.py       │  │
                    │   │  ├─ blueprints/web_runtime.py     │  │
                    │   │  ├─ webhooks/line.py               │  │
                    │   │  ├─ webhooks/telegram.py           │  │
                    │   │  ├─ Auth (Flask-Login + API Key)  │  │
                    │   │  ├─ CSRF Guard                    │  │
                    │   │  └─ Security Headers              │  │
                    │   └──────────┬───────────────────────┘  │
                    │              │                           │
                    │   ┌──────────▼───────────────────────┐  │
                    │   │  Orchestrator (2335 lines)        │  │
                    │   │  Delegates to:                    │  │
                    │   │  ├─ pipelines/message_pipeline    │  │
                    │   │  ├─ pipelines/command_pipeline    │  │
                    │   │  ├─ pipelines/chat_pipeline       │  │
                    │   │  ├─ pipelines/command_dispatch    │  │
                    │   │  ├─ pipelines/skill_dispatch      │  │
                    │   │  ├─ pipelines/message_router      │  │
                    │   │  ├─ domains/judgment_flow         │  │
                    │   │  ├─ domains/laf_flow              │  │
                    │   │  ├─ domains/market_flow           │  │
                    │   │  └─ domains/memory_flow           │  │
                    │   └──────────┬───────────────────────┘  │
                    │              │                           │
                    │   ┌──────────▼───────────────────────┐  │
                    │   │  Routing Layer (api/routing/)     │  │
                    │   │  ├─ service_registry.py           │  │
                    │   │  ├─ model_registry.py             │  │
                    │   │  ├─ node_registry.py              │  │
                    │   │  ├─ datastore_registry.py         │  │
                    │   │  ├─ policy_engine.py              │  │
                    │   │  ├─ request_router.py             │  │
                    │   │  └─ inference_router.py           │  │
                    │   └──────────┬───────────────────────┘  │
                    │              │                           │
                    │   ┌──────────▼───────────────────────┐  │
                    │   │  Skills (skills/*)  — 67+         │  │
                    │   │  ├─ pdf-namer        (PDF 命名)   │  │
                    │   │  ├─ judgment-collector (裁判收集)  │  │
                    │   │  ├─ memory           (RAG 記憶)   │  │
                    │   │  ├─ research         (搜尋研究)   │  │
                    │   │  ├─ market-briefing  (股市晨報)   │  │
                    │   │  ├─ magi-autopilot   (自動巡檢)   │  │
                    │   │  ├─ magi-doctor      (自我診斷)   │  │
                    │   │  └─ ... (60+ skills)              │  │
                    │   └──────────────────────────────────┘  │
                    └──────────────────┬──────────────────────┘
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            │                          │                          │
     ┌──────▼──────┐           ┌───────▼──────┐          ┌───────▼──────┐
     │  Tools API  │           │   MariaDB    │          │  oMLX/Ollama │
     │ (Port 5003) │           │ (magi_brain) │          │  (LLM Host)  │
     │  ├─ /search │           │ ├─ users     │          │ ├─ gemma-4   │
     │  ├─ /vision │           │ ├─ cases     │          │ ├─  26b-4bit │
     │  ├─ /fetch  │           │ ├─ memories  │          │ └─ bert-embed│
     │  └─ /skills │           │ └─ judgments │          └──────────────┘
     └─────────────┘           └──────────────┘

     ┌──────────────────────────────────────────────────────┐
     │              Registry System (json/)                  │
     │  ┌──────────────┐  ┌────────────┐  ┌──────────────┐ │
     │  │services.json │  │models.json │  │ nodes.json   │ │
     │  │(endpoints)   │  │(aliases)   │  │ (IPs/roles)  │ │
     │  └──────────────┘  └────────────┘  └──────────────┘ │
     │  ┌──────────────┐                                    │
     │  │datastores.json│                                   │
     │  │(connections) │                                    │
     │  └──────────────┘                                    │
     └──────────────────────────────────────────────────────┘

     ┌──────────────────────────────────────────────────────┐
     │               Federation (Optional)                  │
     │  ┌─────────┐  ┌───────────┐  ┌─────────────────┐   │
     │  │BALTHASAR│  │ MELCHIOR  │  │    KEEPER       │   │
     │  │(Summary)│  │ (Vision)  │  │ (Remote DB)     │   │
     │  │Apple AI │  │ GPU Node  │  │ MariaDB Node    │   │
     │  └─────────┘  └───────────┘  └─────────────────┘   │
     └──────────────────────────────────────────────────────┘
```

---

## Modular Split Summary

### server.py (9,463 → 802 lines)

| Module | Lines | Responsibility |
|--------|-------|---------------|
| `blueprints/admin_runtime.py` | ~800 | Admin dashboard routes |
| `blueprints/dashboard_pages.py` | ~300 | Dashboard page routes |
| `blueprints/osc_cases.py` | ~4500 | Case management CRUD |
| `blueprints/osc_accounting.py` | ~600 | Accounting routes |
| `blueprints/osc_debt.py` | ~400 | Debt case routes |
| `blueprints/osc_settings.py` | ~300 | Settings routes |
| `blueprints/web_runtime.py` | ~400 | Web app routes |
| `webhooks/line.py` | ~1900 | LINE webhook handler |
| `webhooks/telegram.py` | ~1700 | Telegram webhook handler |

### orchestrator.py (10,269 → 2,335 lines)

| Module | Lines | Responsibility |
|--------|-------|---------------|
| `pipelines/message_pipeline.py` | ~3200 | Message intake & sanitization |
| `pipelines/command_pipeline.py` | ~1500 | Command parsing |
| `pipelines/chat_pipeline.py` | ~1200 | Conversational AI |
| `pipelines/command_dispatch.py` | ~3600 | Skill invocation |
| `pipelines/skill_dispatch.py` | ~800 | Skill resolution |
| `pipelines/message_router.py` | ~1600 | Intent routing |
| `pipelines/attachment_pipeline.py` | ~500 | File handling |
| `pipelines/specialized_commands.py` | ~600 | Domain commands |
| `domains/judgment_flow.py` | ~400 | Judicial queries |
| `domains/laf_flow.py` | ~400 | LAF operations |
| `domains/market_flow.py` | ~300 | Market analysis |
| `domains/memory_flow.py` | ~300 | Memory operations |
| `domains/codex_flow.py` | ~200 | Code analysis |
| `domains/skill_interview_flow.py` | ~200 | Skill queries |

---

## Component Responsibilities

| Component | Port | Role | Required? |
|-----------|------|------|-----------|
| CASPER (server.py) | 5002 | Main app: channels, web UI, orchestration | Yes |
| Tools API (tools_api.py) | 5003 | HTTP API for external callers | Yes |
| MariaDB | 3306 | Persistent storage | Yes |
| oMLX / Ollama | 8080 | Local LLM inference | Yes |
| ModernBERT | 8081 | Embedding service | Yes |
| BALTHASAR | 5002 | Apple Intelligence summarization | No |
| MELCHIOR | 8080 | GPU vision/code analysis | No |
| KEEPER | 3306 | Remote MariaDB (law_firm_data) | No |

---

## Data Flow

```
User Message → Channel Handler → Orchestrator
                                      │
              ┌───────────────────────┤
              ▼                       ▼
    message_pipeline.py        command_pipeline.py
    (sanitize, context)        (parse prefix)
              │                       │
              ▼                       ▼
    message_router.py          command_dispatch.py
    (intent classification)    (skill resolution)
              │                       │
              ├───────────────────────┤
              ▼                       ▼
    chat_pipeline.py           skill_dispatch.py
    (LLM + memory RAG)        (action.py invocation)
              │                       │
              └───────┬───────────────┘
                      ▼
              Response Formatter
                      │
                      ▼
              Channel Delivery
                      │
                      ▼
               User Response
```

---

## Registry System Flow

```
Code needs a value (e.g., Melchior's IP)
    │
    ▼
api/routing/node_registry.get_node_ip("melchior")
    │
    ├─ Check: env var MAGI_MELCHIOR_IP?
    │     └─ Yes → return env value
    │
    ├─ Check: json/nodes.json["melchior"]["ip"]?
    │     └─ Yes → return JSON value
    │
    └─ Fallback: hardcoded default in registry module
          └─ return "100.116.54.16"
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
         Iron Dome
         (SQL/shell/injection filter)
              │
              ▼
         Route Handler
```

---

## Directory Structure

```
MAGI/
├── api/                    # Core API layer
│   ├── server.py           # Flask app entry (802 lines)
│   ├── tools_api.py        # Tools API (5003)
│   ├── orchestrator.py     # Routing hub (2335 lines)
│   ├── db_failover.py      # DB failover controller
│   ├── runtime_paths.py    # Path abstraction layer
│   ├── authz.py            # Unified authorization
│   ├── csrf_guard.py       # CSRF protection
│   ├── blueprints/         # Flask Blueprint modules (7)
│   ├── webhooks/           # Channel webhook handlers (2)
│   ├── pipelines/          # Processing pipelines (8)
│   ├── domains/            # Domain-specific flows (6)
│   ├── routing/            # Registry + routing system (14)
│   ├── handlers/           # Request handlers
│   ├── agents/             # Multi-agent runtime
│   ├── coordinator/        # Task coordination
│   ├── events/             # Event system
│   ├── hooks/              # Hook bus
│   ├── permissions/        # RBAC authorization
│   ├── session/            # Session management
│   ├── tasks/              # Task runtime
│   ├── tools/              # Tool registry
│   └── verification/       # Response verification
├── json/                   # Declarative config (Registry)
│   ├── services.json       # Service endpoints
│   ├── models.json         # Model definitions
│   ├── nodes.json          # Node definitions
│   └── datastores.json     # DB connections
├── gui/                    # GUI (macOS menubar)
├── skills/                 # 67+ pluggable skill modules
├── providers/              # LLM provider abstraction
├── scripts/                # Operational scripts (60+)
├── migrations/             # DB schema management
├── casper_ecosystem/       # LAF automation subsystem
├── tests/                  # 90+ test files
├── docs/                   # Documentation
├── templates/              # Web UI (Jinja2)
├── static/                 # Static assets
├── .github/workflows/      # CI pipeline
└── CONSTITUTION.md         # Governance rules
```

---

## Deployment Modes

| Mode | Description | Phase |
|------|-------------|-------|
| Single-node dev | All on one machine | Current |
| Single-tenant managed | Dedicated host per customer | Phase 1 target |
| Multi-node federation | CASPER + BALTHASAR + MELCHIOR + KEEPER | Supported |
| Multi-tenant SaaS | Shared infrastructure | Future (not Phase 1) |

---

## Support Matrix

| Feature | Status | Since |
|---------|--------|-------|
| LINE Bot channel | Production | v0.1 |
| Discord Bot channel | Production | v0.5 |
| Telegram Bot channel | Production | v0.8 |
| Web Dashboard | Production | v0.3 |
| Modular Architecture | Production | v2.0 |
| Registry System | Production | v2.0 |
| Unified Routing | Production | v2.0 |
| DB Failover | Production | v2.0 |
| macOS Status Bar | Production | v2.0 |
| `magi` CLI | Production | v2.0 |
| Federation (multi-node) | Beta | v0.9 |
| MCP Server | Beta | v1.0 |
| Browser automation (LAF) | Controlled | v0.5 |
