# MAGI V3 — Local-first AI Operations Platform

MAGI V3 is not a chat server with a collection of scripts. It is a single-active,
local-first operations platform that coordinates interactive web services,
durable schedules, isolated workers, legal-office workflows, document and media
processing, storage synchronization, local models, and verifiable recovery.

This repository is the privacy-isolated public MAGI V3 source snapshot. It
publishes reusable architecture, contracts, tests, examples, and de-identified
documentation. Private legal connectors, case data, credentials, workstation
paths, runtime state, and canonical production receipts are excluded.

> This Git repository is an engineering source snapshot, not the live runtime.
> Credentials, cookies, case data, databases, browser profiles, mutable queues,
> and canonical production receipts are deliberately excluded.

Traditional Chinese: [README.zh-TW.md](README.zh-TW.md)

## Current public release

- **Release:** MAGI RC643 / R75 (2026-08-30).
- **Release contract:** [`PUBLIC_RELEASE.json`](PUBLIC_RELEASE.json).
- **De-identified release notes:**
  [`RC643_R75_PUBLIC_RELEASE.md`](docs/architecture/v3/RC643_R75_PUBLIC_RELEASE.md).
- **Maintenance record:**
  [`RC643_R75_MAINTENANCE.md`](docs/architecture/v3/RC643_R75_MAINTENANCE.md).
- **Private verified source baseline:**
  `ab398a44c99b9e83e6ba5c989312df29cf914006` (digest only).

Private immutable `hotfixN` suffixes do not alter the public R75 label. This
public repository is not evidence that a private deployment is live; production
identity must be verified from its own active marker and signed receipts.

## Contents

- [System model](#system-model)
- [Runtime topology](#runtime-topology)
- [Repository map](#repository-map)
- [State, data, and trust boundaries](#state-data-and-trust-boundaries)
- [Work scheduling and recovery](#work-scheduling-and-recovery)
- [Business and AI capabilities](#business-and-ai-capabilities)
- [Health and user-visible status](#health-and-user-visible-status)
- [Development and verification](#development-and-verification)
- [Immutable release and cutover](#immutable-release-and-cutover)
- [Operations and troubleshooting](#operations-and-troubleshooting)
- [Documentation](#documentation)
- [Public/private repository boundary](#publicprivate-repository-boundary)

## System model

MAGI separates four things that V2 often mixed together:

1. **Source** — reviewed code and tests in Git.
2. **Release** — a complete, immutable, manifest-hashed copy of executable code.
3. **Deployment** — rendered launchd configuration, environment, ownership, and
   external-input bindings for exactly one release.
4. **Runtime state** — ledgers, checkpoints, queues, receipts, caches, locks, and
   logs that survive release replacement.

The active release marker is the authority for what is live. A Git branch name,
working tree, queued command hash, stale PID file, or historical test receipt does
not prove which code owns production.

The normal lifecycle is:

```text
source commit
  -> focused tests and privacy audit
  -> sealed release bundle + manifest
  -> exact/full certification (once per immutable input set)
  -> backup + two restore drills
  -> inactive installation
  -> rendered deployment + ownership manifest
  -> prepared transaction
  -> single-active cutover
  -> synchronous core HTTP/security evidence
  -> independent module and background-health receipts
```

Every stage is fail-closed. Evidence from an abandoned candidate is not reused by
the next candidate.

## Runtime topology

```text
Browser / menu bar / LINE / Discord / Telegram
                      |
                      v
        +-----------------------------+
        | Gateway role                |
        | 5002: main OSC/web surface  |
        | 5003: tools/API surface     |
        +-----------------------------+
                      |
                      v
        +-----------------------------+
        | Control role                |
        | 8088: health/admin control  |
        | release identity + ledger   |
        +-----------------------------+
                      |
                      v
        +-----------------------------+
        | Supervisor role             |
        | scheduler + bounded workers |
        | retries + resource leases   |
        +-----------------------------+
              |       |       |
              v       v       v
          browser  document  integration/model workers
```

| Role | Responsibility | Scheduling class |
|---|---|---|
| `gateway` | User-facing HTTP ingress, authentication, compatibility routes | Interactive |
| `control` | Health/admin HTTP, release identity, lightweight control plane | Background |
| `supervisor` | Scheduler and lifecycle ownership of non-HTTP services | Background |

The service manifest is authoritative for roles, ports, factories, child
entrypoints, deployment mode, and forbidden legacy processes. Production binds
the manifest path and SHA-256 in the launchd environment; entrypoints refuse an
unbound or modified manifest.

Heavy work does not run in the gateway. Browser, OCR/PDF, transcription,
Drive/NAS, model, and maintenance operations run in supervised child processes
with timeouts, resource limits, cancellation, process-group cleanup, and durable
completion evidence.

## Repository map

| Path | What it owns |
|---|---|
| `magi_v3/` | V3 runtime core: gateway, control, supervisor, ledger, scheduling, health, ownership, recovery |
| `api/` | OSC/web application, domain adapters, compatibility endpoints, authentication and API composition |
| `api/blueprints/` | Flask page/API boundaries, including authenticated maintenance manual routes |
| `api/osc/` | OSC case, calendar, Drive, file, and domain workflows |
| `skills/` | Explicit tool entrypoints and domain workflows; heavy dependencies stay outside the control plane |
| `scripts/v3_validation/` | Formal certification, exact route replay, fault/performance checks, evidence schemas |
| `scripts/v3_cutover/` | Single-active preflight, activation, rollback, ownership and process probes |
| `scripts/v3_deploy_prepare.py` | Offline deployment renderer; it does not start services |
| `scripts/v3_release_bundle.py` | Creates a clean, allowlisted, immutable release payload and manifest |
| `scripts/v3_backup_prepare.py` / `v3_backup_verify.py` | Backup creation and restore-proof gates |
| `scripts/ops/` | Operational audits, health snapshots, cleanup and bounded repair entrypoints |
| `config/` | Versioned policy, service, schedule, validation, and feature definitions |
| `templates/`, `static/` | Web UI and local assets |
| `gui/` | macOS menu-bar status UI |
| `mobile_app/` | Mobile-compatible web/app shell |
| `tests/` | Unit, contract, security, replay, release, cutover, and regression coverage |
| `docs/` | Architecture notes, technical manuals, and generated maintenance encyclopedia |
| `magi_v3/manual_assets/` | Release-packaged, authenticated, no-store maintenance manuals |

The machine-readable source index in `docs/` maps documented components to
source files, line ranges, symbols, and SHA-256 values. Use it when the prose and
the current tree appear to disagree.

## State, data, and trust boundaries

### Immutable release

An installed release contains only files declared by its manifest. Each member
has a path, size, mode, and SHA-256. Symlinks, special files, unknown additions,
source drift, and post-seal mutation are rejected. Release code is never used as
a queue, cache, log directory, or credential store.

Host-level singleton services never bind their installed launch configuration to
a versioned `releases/v3-*` directory. A stable host launcher resolves the active
marker and verifies the release manifest plus the selected script hash before
starting memory watchdog, optional MTP, or Paperclip services. This lets an old
release be retired without leaving a hidden restart dependency.

### Mutable runtime

Runtime state is external to the release and divided by ownership:

- control ledger and release transaction state;
- cron definitions versus mutable occurrence/retry results;
- Drive file checkpoints and content-hash cache;
- browser/domain locks and owner metadata;
- notification outbox and business receipts;
- logs, bounded caches, and generated exports.

Lock files and owner metadata are evidence, not garbage. Never delete them merely
to make a dashboard green. First prove whether the bound PID/process group is
alive and whether its executable, release root, command, and ownership schema are
canonical.

### External systems

NAS, Drive, LAF/court portals, calendars, mail, and messaging are outside the
transaction boundary. Writes therefore use prepare/commit/read-back evidence,
idempotency, or a durable outbox. An accepted request is not reported as complete
until the required external effect and receipt are verified.

### Secrets and personal data

Secrets are loaded from Keychain or deployment-bound environment files. Real
case identifiers, parties, paths, tokens, cookies, raw messages, runtime
databases, and browser profiles must never enter Git or public evidence. Public
reports use counts, fixed reason codes, salted/opaque digests, and artifact hashes.

## Work scheduling and recovery

The scheduler keeps immutable job definitions separate from mutable state. An
occurrence records the exact command-definition hash it was created for. At
startup MAGI reconciles interrupted work, supersedes stale command bodies, and
rebuilds eligible work using the current sealed definition rather than executing
queued code from an older release.

Canonical outcomes are semantically distinct:

- `succeeded`: business effect and required receipt are complete;
- `deferred`: a safe retry is required because an external resource is busy or unavailable;
- `waiting_children`: child work is still outstanding;
- `skipped`: verified evidence proves there is nothing to do;
- `needs_confirmation` / `awaiting_input`: human evidence is required;
- `failed` / `timed_out` / `cancelled`: terminal outcomes with explicit cleanup.

Retries are bounded and reason-coded. A timeout, storage outage, portal busy
state, identity guard, process interruption, and human-required outcome are not
interchangeable. Health presentation uses the current reconciled outcome; it
must not keep a permanent red light solely because an older run failed.

Drive all-files synchronization is intentionally checkpointed. Each bounded
chunk records cursor movement, progress time, content-hash cache use, staging
bytes, and zero/non-zero risk counters. Semantic filename aliases are resolved
by verified identity or bounded local hashing; uncertain collisions remain
deferred and never trigger destructive guessing.

## Business and AI capabilities

MAGI groups user-facing work into independently observable domains:

- OSC cases, files, tasks, calendars, billing, and lifecycle;
- LAF messages, portal follow-up, attachments, progress drafts, and closing;
- court file-review detection, download, signature reconciliation, and filing;
- transcript download, transcription, diarization, indexing, and summaries;
- document generation, DOCX/PDF editing, OCR, naming, bookmarks, and review;
- Drive/NAS bidirectional synchronization and storage recovery;
- legal research, judgments, statutes, evidence, and trial preparation;
- local model routing, NERV/agent reasoning, embeddings, memory, and knowledge;
- notifications through outbox-backed messaging adapters;
- Cookie Cutter image-to-printable-mesh generation with bounded resource and
  no-persistence validation paths;
- a public tool directory that separates creative/manufacturing, learning,
  legal-work, and maintenance surfaces;
- a local-only Video Studio derived from the pinned
  `video-autopilot-kit` programmatic path. It combines up to five local
  images/videos or text-only scenes under an edit plan that MAGI first parses
  and shows back to the user. Both paths produce attested 9:16 H.264/AAC
  output without CapCut, remote asset fetching, automatic publishing, or
  persistent uploads.

The public `/tools`, `/video-studio`, `/cookie-cutter`, `/lottery`, and
`/exam-tutor` pages do not require a MAGI account. Public does not mean
unbounded: write endpoints retain CSRF, strict input schemas, durable rate
limits, concurrency limits, deadlines, process cleanup, output attestation,
and no-store responses. Case, legal-office, health, and maintenance surfaces
remain authenticated.

Each domain may be healthy, waiting, degraded, or action-required without
changing the liveness of unrelated domains. The menu bar and business snapshot
translate only allowlisted reason codes into user-visible text.

## Health and user-visible status

Health is layered rather than reduced to “the process exists”:

1. **Liveness** — the lightweight process and event loop respond.
2. **Readiness** — release identity, dependencies, role ownership, and children are valid.
3. **Business health** — domain work has fresh, reconciled receipts.
4. **Function/Doctor/guardian/Funnel** — operational contracts, protection,
   routes, and user journeys remain valid.
5. **Post-cutover evidence** — the active release and transaction passed the
   release-specific live checks.

The authenticated maintenance encyclopedia is available at `/manual`; its PDF,
Markdown, and machine-readable source index are fixed allowlisted routes. All
manual responses are `no-store`, reject symlinks/path traversal, and are served
from the active immutable release.

## Development and verification

Use the repository's pinned runtime/dependency instructions. Do not casually run
production entrypoints from the source tree: imports and paths are designed to
reject source/runtime mixing.

Typical source checks:

```bash
python3 -m pytest -q tests/test_dashboard_pages_blueprint.py
python3 -m pytest -q tests/test_web_information_architecture.py
python3 -m py_compile api/blueprints/dashboard_pages.py
python3 scripts/privacy_audit.py --strict
python3 scripts/public_release_audit.py --public-isolation --strict
git diff --check
```

Formal promotion additionally requires exact test selection, source hashes,
route replay, fault and recovery cases, privacy isolation, a clean worktree, and
an outer-host certification runner. A nested macOS Seatbelt cannot be created
from every sandbox; that limitation is handled by running the hash-bound outer
runner, never by silently disabling the official inner sandbox.

Validation has three distinct execution layers:

1. **Synchronous promotion/cutover** proves immutable identity, the complete
   formal suite, backup/restore, ownership, rollback, local core routes, and
   security. The complete formal suite runs once for an identical commit,
   manifest, suite manifest, runtime, and test-source hash set.
2. **Changed-module acceptance** proves only the domains affected by the change
   (for example Cookie Cutter geometry, judgment-source evidence, file-review
   reconciliation, manual rendering, or Drive checkpoint semantics). It must
   not rerun formal nodes already covered by the same sealed receipt.
3. **Independent background health** produces separate business, function,
   Doctor, guardian, Funnel, Drive, portal/MCP, and benchmark receipts. A
   bounded `waiting` or `deferred` result remains visible for that domain but
   does not retroactively invalidate an otherwise healthy release.

An immutable receipt may be reused only when every bound input above is byte
identical. External availability, lock timing, a daily sample, or a cron
occurrence is mutable operational evidence and can never be used as a cache key
for source certification. See
[Validation gate policy](docs/architecture/v3/VALIDATION_GATE_POLICY.md).

Tests must not read or write canonical runtime state. Use temporary HOME,
runtime, agent, browser, queue, upload, and cache directories. Network and
launchctl access remain off unless a specifically reviewed LIVE wrapper owns the
operation.

## Immutable release and cutover

MAGI uses single-active cold replacement:

1. certify a clean sealed release;
2. create a fresh backup and prove two independent restores;
3. install the candidate without activating it;
4. render launchd configuration and bind every external input by path and hash;
5. prepare a transaction and rollback artifacts;
6. quiesce the old supervisor inside the rollback envelope;
7. verify zero old owners and durable handoff state;
8. atomically activate and start gateway, control, and supervisor;
9. run the synchronous core Web/security gate and changed-module acceptance;
10. publish independent background-health receipts for business, function,
    Doctor, guardian, Funnel, Drive, portals/MCP, and quality benchmarks;
11. restore the old release automatically only when a synchronous hard gate
    fails before the transaction is committed.

Never run two full production releases concurrently. Never replace a file inside
an installed release. Never reuse a receipt from another transaction. Never
manually edit cron state or erase a checkpoint to manufacture a successful gate.

## Operations and troubleshooting

Start with evidence, in this order:

1. Read the active marker and transaction.
2. Verify the installed manifest and deployment ownership manifest.
3. Inspect role and worker owner metadata; compare PID, process group,
   executable, argv, and release root.
4. Read the freshest terminal/business receipt and checkpoint counters.
5. Distinguish current failure from an old failure, deferred retry, or waiting
   external resource.
6. Reproduce with the narrowest source test or read-only probe.
7. Fix source and add an adversarial regression test.
8. Build a new immutable release; do not patch LIVE files.

Common anti-patterns:

- deleting a lock without proving its owner is gone;
- treating exit code zero as business success;
- trusting a path, filename, or stale command hash as release identity;
- allowing `bool` where an exact integer counter is required;
- swallowing deadline exceptions inside broad exception handlers;
- marking queued background work complete before child receipts exist;
- copying mutable runtime directories into a release or Git;
- using a visually similar image or document when byte-exact evidence is required.

Process health is defined once in `magi_v3/process_monitor.py`. Golem and the
macOS Menubar consume the same core/worker/orphan/zombie/duplicate summary.
Shell `-c` launchers are not workers; orphan status follows the actual worker's
ancestry to a canonical MAGI owner; zombies must persist for five seconds.

For detailed symptom-to-source mappings and repair runbooks, use the encyclopedia
linked below.

## Documentation

- [RC643/R75 public release](docs/architecture/v3/RC643_R75_PUBLIC_RELEASE.md)
- [RC643/R75 de-identified maintenance record](docs/architecture/v3/RC643_R75_MAINTENANCE.md)
- [Architecture reference](docs/architecture/v3/MAGI_V3_ARCHITECTURE.md)
- [Agent Gateway](docs/architecture/v3/MAGI_AGENT_GATEWAY.md)

The complete private maintenance encyclopedia and machine-readable private
source index are intentionally not copied into the public repository. The two
RC643/R75 documents above are the de-identified public projection.

## Public/private repository boundary

- **`WhaleChao/MAGI-v3`**: the original private repository and complete V2/V3
  engineering history. Private does not mean secrets may be committed.
- **`WhaleChao/MAGI-public`**: privacy-filtered architecture, source, tests,
  examples, and documentation for public review.

The canonical sealed payload and production receipts remain in the local release
evidence chain. GitHub intentionally omits case-bearing and runtime-only data, so
neither repository checkout should be mistaken for the currently active
production deployment.

## Security and support

Never report a security issue with real credentials or case material in a public
issue. See [SECURITY.md](SECURITY.md) and [SUPPORT.md](SUPPORT.md) where present.
