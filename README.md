# MAGI V3 — Local-first AI Operations Platform

MAGI V3 coordinates interactive web services, durable schedules, isolated
workers, document and media processing, storage synchronization, local models,
business workflows, health evidence, and rollback-ready releases. It is not a
single chat process and it does not treat “the process is alive” as proof that a
business operation completed.

This is the privacy-filtered public repository. Production credentials, cookies,
case data, databases, browser profiles, mutable queues, and canonical live
receipts are intentionally absent.

Traditional Chinese: [README.zh-TW.md](README.zh-TW.md)

## Contents

- [Architecture](#architecture)
- [Source map](#source-map)
- [State and trust boundaries](#state-and-trust-boundaries)
- [Scheduling and recovery](#scheduling-and-recovery)
- [Capabilities](#capabilities)
- [Health](#health)
- [Development and certification](#development-and-certification)
- [Release and rollback](#release-and-rollback)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)
- [Repository boundary](#repository-boundary)

## Architecture

MAGI separates four artifacts that older deployments often mixed:

1. **Source** — reviewed code and tests in Git.
2. **Release** — an immutable, per-file manifest-hashed executable bundle.
3. **Deployment** — launchd configuration, ownership, environment, and external
   input bindings for exactly one release.
4. **Runtime state** — ledgers, checkpoints, queues, receipts, caches, locks, and
   logs that survive release replacement.

```text
Browser / menu bar / messaging integrations
                    |
                    v
      Gateway (Interactive, ports 5002/5003)
                    |
                    v
      Control (Background, port 8088)
                    |
                    v
      Supervisor (Background, scheduler/workers)
          |          |          |
       browser    document   integration/model
```

The service manifest is authoritative for roles, ports, factories, child
entrypoints, deployment mode, and forbidden legacy processes. Production binds
its path and SHA-256. Entry points reject an unbound, moved, or modified manifest.

Heavy Playwright, OCR/PDF, transcription, Drive/NAS, model, and maintenance work
runs in supervised children, not in the gateway. Children have explicit timeout,
resource, cancellation, process-group cleanup, and completion contracts.

## Source map

| Path | Responsibility |
|---|---|
| `magi_v3/` | Gateway, control, supervisor, ledger, scheduling, ownership, health and recovery |
| `api/` | OSC/web composition, authentication, compatibility routes and domain adapters |
| `api/osc/` | Cases, files, calendar, Drive and OSC workflows |
| `api/blueprints/` | Page/API boundaries, including authenticated manual routes |
| `skills/` | Explicit tool and domain workflow entrypoints |
| `scripts/v3_validation/` | Exact/full certification, replay, fault and evidence machinery |
| `scripts/v3_cutover/` | Single-active preflight, activation, rollback and owner probes |
| `scripts/v3_release_bundle.py` | Clean allowlisted release bundle and manifest creation |
| `scripts/v3_deploy_prepare.py` | Offline deployment rendering; never starts services |
| `scripts/v3_backup_prepare.py` / `v3_backup_verify.py` | Backup and actual restore gates |
| `scripts/ops/` | Operational audits, health snapshots, cleanup and bounded repair |
| `config/` | Versioned service, schedule, feature and validation policy |
| `templates/`, `static/`, `gui/`, `mobile_app/` | Web, desktop and mobile presentation |
| `tests/` | Unit, contract, security, replay, release, cutover and regression tests |
| `docs/` | Architecture and maintenance documentation |
| `magi_v3/manual_assets/` | Release-packaged authenticated/no-store manuals |

## State and trust boundaries

Installed releases contain only manifest-declared regular files with exact path,
size, mode and SHA-256. Symlinks, special files, unknown additions, source drift,
and post-seal mutation are rejected. Runtime state never belongs inside a release.

Mutable state is split by ownership: control ledger, cron occurrences/retries,
Drive checkpoint/hash cache, domain locks, owner metadata, notification outbox,
business receipts, logs, bounded caches, and generated exports.

Locks are evidence, not litter. Do not remove one to clear a dashboard: first
prove whether the bound PID/process group, executable, argv, release root, and
schema describe a live canonical owner.

NAS, Drive, portals, calendars, mail, and messaging are outside the local
transaction. External effects therefore require idempotency, prepare/commit/read-
back evidence, or a durable outbox. HTTP 200 or queue acceptance is not business
completion.

## Scheduling and recovery

Immutable job definitions are separate from mutable scheduler state. Each
occurrence carries its command-definition hash. After restart MAGI reconciles
interrupted work, supersedes stale command bodies, and rebuilds eligible work from
the current sealed definition.

Outcomes retain distinct meanings: `succeeded`, `deferred`, `waiting_children`,
`skipped`, `needs_confirmation`, `awaiting_input`, `failed`, `timed_out`, and
`cancelled`. Retries are bounded and reason-coded; timeout, storage outage,
portal contention, identity guard, process interruption, and human-required work
cannot be substituted for one another.

Drive all-files synchronization advances in checkpointed chunks. Its terminal
evidence includes cursor movement, fresh progress, content-hash cache use,
anonymous staging bytes, and strict zero-risk counters. Ambiguous filename aliases
remain deferred until source identity or bounded content hashing resolves them.

## Capabilities

- OSC cases, files, tasks, calendars, billing, and lifecycle;
- legal-aid and court message/portal workflows;
- file-review detection, download, signature reconciliation, and filing;
- transcription, diarization, indexing, translation, and summaries;
- DOCX/PDF/OCR generation, editing, naming, bookmarks, and review;
- Drive/NAS bidirectional synchronization and storage recovery;
- legal research, judgments, statutes, evidence, and trial preparation;
- local model routing, agent reasoning, embeddings, memory, and knowledge;
- outbox-backed notifications;
- bounded image-to-printable-mesh generation and validation.

Domains are independently observable. One deferred portal does not make the
gateway dead, and one historical failure does not make a reconciled current run
red. User-visible messages are produced from allowlisted reason codes.

## Health

MAGI health is layered:

1. lightweight liveness;
2. dependency, identity, role-owner, and child readiness;
3. fresh reconciled business receipts;
4. Function, Doctor, guardian, and Funnel contracts;
5. release/transaction-specific post-cutover evidence.

The maintenance encyclopedia is available to authenticated users at `/manual`.
Its HTML, PDF, Markdown, and source-index routes are fixed allowlists served from
the active immutable release with `Cache-Control: no-store`.

## Development and certification

Use the pinned runtime and keep tests isolated from canonical HOME, runtime,
agent, browser, queue, upload, and cache paths.

```bash
python3 -m pytest -q tests/test_dashboard_pages_blueprint.py
python3 -m pytest -q tests/test_web_information_architecture.py
python3 -m py_compile api/blueprints/dashboard_pages.py
python3 scripts/privacy_audit.py --strict
python3 scripts/public_release_audit.py --public-isolation --strict
git diff --check
```

Formal promotion also binds exact test selection, source hashes, route replay,
fault/recovery coverage, privacy isolation, and a clean source tree. On macOS the
official inner Seatbelt is run by a hash-bound host-outer runner; sandbox nesting
limitations are not solved by weakening the security gate.

## Release and rollback

MAGI uses single-active cold replacement:

1. certify a clean sealed bundle;
2. create a fresh backup and prove independent restores;
3. install the candidate without activating it;
4. render and hash-bind deployment inputs;
5. prepare transaction and rollback artifacts;
6. quiesce the previous supervisor inside the rollback envelope;
7. prove zero old owners and durable state handoff;
8. atomically activate and start the three roles;
9. run web, Funnel, STL, business, health, and Drive live gates;
10. automatically restore the prior release after any hard-gate failure.

Never run two production releases concurrently, patch an installed release,
reuse another transaction's receipt, or edit cron/checkpoint state to manufacture
a green result.

## Troubleshooting

1. Read the active marker and transaction.
2. Verify installed manifest and deployment ownership.
3. Inspect role/worker owner metadata and the actual PID/PGID/executable/argv.
4. Read the latest terminal/business receipt and safe checkpoint counters.
5. Separate current failure from historical failure, deferred retry, or waiting.
6. Reproduce with the narrowest read-only probe or source test.
7. fix source and add an adversarial regression;
8. publish a new immutable release instead of patching LIVE.

Avoid broad exception handlers that swallow deadlines, integer coercion that
accepts booleans, path-based identity guesses, parent success before child
receipts, and copying mutable runtime content into Git or a release.

## Documentation

- [Maintenance encyclopedia — HTML](docs/MAGI_V3_維修百科全書_rc627.html)
- [Maintenance encyclopedia — PDF](docs/MAGI_V3_維修百科全書_rc627.pdf)
- [Maintainable encyclopedia source](docs/MAGI_V3_維修百科全書_rc627.md)
- [Machine-readable source index](docs/MAGI_V3_原始碼索引_rc627.json)
- [Public technical manual](docs/MAGI_V3_技術手冊_rc627_公版.md)
- [Architecture reference](docs/architecture/v3/MAGI_V3_ARCHITECTURE.md)
- [Self-host deployment](docs/SELFHOST_DEPLOYMENT.md)
- [Security policy](SECURITY.md)
- [Support](SUPPORT.md)

The self-contained HTML manual has a generated table of contents, full-text
search, source links, and a day/night theme that uses MAGI's local UI preference.

## Repository boundary

- `WhaleChao/MAGI-public` contains privacy-filtered source, tests, examples, and
  documentation.
- `WhaleChao/MAGI-v3` is the original private V2 repository renamed in place and
  preserves complete V2/V3 engineering history.

Neither checkout is the live runtime. Canonical sealed payloads and production
receipts remain in the local release evidence chain. See [LICENSE](LICENSE).
