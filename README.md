# MAGI

[繁體中文版](README.zh-TW.md)

[![MAGI CI](https://github.com/WhaleChao/MAGI-public/actions/workflows/ci.yml/badge.svg?branch=codex%2Ffactory-release-20260712-public)](https://github.com/WhaleChao/MAGI-public/actions/workflows/ci.yml)

MAGI is a local-first AI operations platform for Taiwanese legal work. It connects case records, files, calendars, tasks, document processing, legal-aid workflows, court file review, transcripts, legal research, accounting, notifications, and system health through web, LINE, Discord, Telegram, and administrator tools.

The default production target is a single Apple Silicon machine. Local inference uses oMLX with automatic day/night profiles; high-quality requests such as `@heavy` or `@重型` are routed through the guarded heavy-model path when configured. Windows and Linux installations may use the supported Ollama path.

## Current Release Status

The factory release prepared on 2026-07-12 and rechecked on 2026-07-13 has:

- 37 agent capabilities across 28 operational domains.
- Immutable intent and workflow-plan contracts with confidence, missing-field, confirmation, verification, retry, and rollback states.
- Natural-language calendar query, preview, create, update, cancel, all-day, timed, cross-day, and recurring-event support.
- A public-safe Agent status panel in NERV and the MAGI menubar.
- Durable completion evidence for long scheduled jobs so a daemon restart does not create a false failure.
- 4,392 local tests passing at the release checkpoint.
- `commercial-release` passing 12/12 and strict public isolation passing with 0 errors and 0 warnings.

GitHub keeps historical failed workflow runs. An old red run is not rewritten after a later fix. Check the newest commit or the active release PR; the latest public and private release checks are green.

## What MAGI Can Do

| Area | Supported work |
|---|---|
| Cases and clients | Create, search, update, close, disambiguate identities, and open managed case folders |
| Calendar and tasks | Natural-language query/create/update/cancel, all-day and recurring events, conflict checks, case tasks |
| Files and documents | Upload, preview, OCR, naming, indexing, finalization, guarded NAS and Drive synchronization |
| Legal Aid Foundation | Read activity counts, prepare portal drafts, submit only after confirmation, monitor attachments and closure evidence |
| Court file review | Probe availability, prepare applications, confirm submission, download and reconcile files |
| Court transcripts | Per-case download, batch synchronization, deduplication, rename, indexing, and manual-review queues |
| Audio and translation | Local speech-to-text, document translation, OCR consensus, and high-quality model routing |
| Legal research and drafting | Statute search, judgment collection, research ingestion, source-aware legal drafting |
| Office operations | Accounting transactions, quotations, memory rules, Obsidian writeback, notifications, backup and restore |
| System operations | Model-profile checks, acceptance gates, process hygiene, scheduled-job health, self-repair evidence |

The capability source of truth is [`config/agent_capabilities.json`](config/agent_capabilities.json). A capability is not considered ready unless its tool, side effect, verification rule, and human-handling rule pass the Agent readiness gate.

## Natural-Language Agent

MAGI now places a structured planning layer around its existing routes and tools:

1. Interpret the request into an intent, entities, constraints, confidence, and missing fields.
2. Build a dependency-aware workflow plan.
3. Select tools according to their side-effect contract.
4. Ask for clarification when identity, date, target, or scope is ambiguous.
5. Require explicit confirmation for external commits, destructive actions, and protected portal submissions.
6. Verify the persisted or external result before reporting completion.
7. Retain retry, degraded, rollback, and operator-review states instead of treating a failed action as done.

Examples:

```text
What is on my calendar next week?
Add an all-day event next Friday called filing deadline.
Move tomorrow's client meeting to 3:30 PM.
Every first Monday at 9 AM, add a case review meeting.
Check whether this case has new court-review files.
@heavy Translate this recording and preserve legal terminology.
```

Calendar writes are previewed and confirmed before execution. MAGI conservatively resolves recent references to cases, people, attachments, schedules, drafts, and plans; low-confidence references do not become write proposals.

## Menubar and NERV

The MAGI menubar is the operator's compact status surface. It reports:

- Core services and the three MAGI modules.
- Active local/heavy model profile and model endpoint health.
- Memory, database, NAS/storage capacity, and process state.
- Legal-aid attachments, court file review, transcripts, case reporting, monitored mailboxes, and web modules.
- Scheduled-job count, running jobs, failures, stale state, and human-readable failure details.
- The most recent public-safe Agent intent, plan step, tool category, confirmation state, retry count, and route confidence.

Clicking an abnormal item opens a copyable explanation. Raw internal JSON, prompts, user content, case data, local paths, tokens, and model reasoning are not exposed by the public Agent status endpoint.

## Safety Model

MAGI classifies tool effects as read-only, local draft, reversible write, external commit, or destructive. Protected actions use permission checks, confirmation tokens, idempotency keys, post-action verification, and rollback or operator-review instructions.

Never remove confirmation from:

- Legal-aid or court-portal submission.
- Database restore or migration rollback.
- Bulk deletion, case-folder movement, or external publication.
- An uncertain repeat submission where the first external result is unknown.

AI output is assistive work product. A qualified person must review legal citations, names, case numbers, deadlines, money, translations, transcripts, and documents before external use.

## Public Installation

```bash
git clone https://github.com/WhaleChao/MAGI-public.git
cd MAGI-public
python3 scripts/customer_install_wizard.py --public --yes
python3 scripts/public_release_audit.py --public-isolation --strict
magi status
```

The public repository contains no production database, case files, credentials, portal screenshots, private runtime artifacts, or private deployment paths. Private integrations remain disabled until the installer supplies configuration and credentials.

## Administrator Commands

```bash
magi status
magi start
magi stop
magi restart
magi menubar
magi zombie
```

Canonical test entry points:

```bash
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite smoke62
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_latest.json
./venv/bin/python scripts/ops/run_test_suite.py --suite commercial-release --json-out .runtime/commercial_release_latest.json
./venv/bin/python scripts/ops/agent_readiness_gate.py --strict
```

`ci` is public-safe and runs on GitHub. `smoke62` is the local production smoke gate. `production-live` checks configured production dependencies without making unconfirmed destructive submissions. `commercial-release` adds public isolation, clean-install, model, channel, heavy-route, real-world skill, and function-health gates.

The matrix source of truth is [`config/test_matrix.json`](config/test_matrix.json); see [`docs/TESTING_SYSTEM.md`](docs/TESTING_SYSTEM.md).

## Architecture

```text
Web / LINE / Discord / Telegram
               |
        Message pipeline
               |
   Intent envelope and safe planner
               |
 Existing deterministic routes / ReAct tools
               |
 Tool side-effect and confirmation contracts
               |
 Verification, retry, rollback, human review
               |
 OSC / files / portals / calendars / models
```

Important code areas:

| Path | Responsibility |
|---|---|
| `api/agentic/` | Intent, plan, confirmation, side-effect, session, telemetry, and shadow-agent contracts |
| `api/domains/calendar_agent*.py` | Natural-language calendar parsing and guarded runtime workflow |
| `api/pipelines/message_pipeline.py` | Cross-channel routing and deterministic interceptors |
| `api/orchestrator.py` | Existing orchestration and tool integration |
| `api/blueprints/` | Web and OSC APIs |
| `skills/` | Domain tools and workflow implementations |
| `gui/magi_menubar.py` | Operator status and copyable diagnostics |
| `scripts/ops/` | Health, acceptance, release, model, backup, and self-repair operations |

## Documentation

- [User guide](docs/USER_GUIDE.md)
- [Current operation manual](docs/guides/MAGI_操作手冊.md)
- [Commercial readiness](docs/COMMERCIAL_READINESS.md)
- [Public self-install guide](docs/PUBLIC_SELF_INSTALL.md)
- [Public operation manual](docs/PUBLIC_OPERATION_MANUAL.md)
- [Private operation manual](docs/PRIVATE_OPERATION_MANUAL.md)
- [Operator runbook](docs/OPERATOR_RUNBOOK.md)
- [Testing system](docs/TESTING_SYSTEM.md)
- [Security policy](SECURITY.md)
- [Support policy](SUPPORT.md)
- [Terms of service](docs/TERMS_OF_SERVICE.md)
- [Privacy policy](docs/PRIVACY_POLICY.md)
- [Data retention policy](docs/DATA_RETENTION_POLICY.md)
- [Third-party BOM](docs/THIRD_PARTY_BOM.md)

## License

See [`LICENSE`](LICENSE). Third-party components remain subject to their own licenses.
