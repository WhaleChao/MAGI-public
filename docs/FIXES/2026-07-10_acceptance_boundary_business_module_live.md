# 2026-07-10 Acceptance Boundary and Business Module Live Check

## Scope

- Formalized MAGI acceptance-boundary suites in `config/test_matrix.json`:
  `acceptance-live` and `acceptance-weekly-deep`.
- Documented the factory-boundary profiles in `docs/TESTING_SYSTEM.md`.
- Expanded `business_module_live_check.py` source/runtime fingerprint coverage
  across LAF, file review, transcript, business cron jobs, and acceptance gate
  files.
- Synchronized live runtime copies and restarted MAGI after runtime-affecting
  changes.

## Fixes

- Detected and fixed file-review source/runtime drift:
  runtime was missing the current duplicate-download quarantine logic, staging
  cleanup command, denser non-destructive downloadable probe, and updated
  file-review cron cadence.
- Seeded runtime `cron_jobs.json` from the current runtime root so paths remain
  runtime-local while matching source cron semantics.
- Refined Drive/NAS health boundaries: stale status for disabled worker kinds
  is now retained as `inactive_kinds` for observation but no longer blocks
  `/health` or the business module live check. Active kinds still block on
  stale, failed, interrupted, auth-required, partial-failure, or action-required
  states.

## Verification

- `py_compile` passed for touched runtime scripts and file-review modules.
- Focused regression: `80 passed`.
- File-review regression before runtime sync: `35 passed`.
- `git diff --check`: clean.
- `run_test_suite.py --suite acceptance-live --dry-run`: pass as dry-run.
- Restarted MAGI; fresh PIDs verified for daemon, server, Discord bot, Tools API,
  RPC worker, status bar, OSC NAS helper, and website admin.
- Runtime deployment backups:
  `.runtime/deploy_backups/20260710_acceptance_boundary`,
  `.runtime/deploy_backups/20260710_file_review_runtime_drift`,
  `.runtime/deploy_backups/20260710_drive_inactive_kind_boundary`, and
  `.runtime/deploy_backups/20260710_admin_runtime_fingerprint`.
- `/livez`, `/readyz`, and Tools API health all returned OK.
- `/health?fresh=1` returned `status=operational`; Drive/NAS active kinds are
  `all_files` and `priority`, with stale `inventory` retained only as inactive.
- Final non-destructive business module live check:
  `.runtime/business_module_live_check_20260710_final.json`, `ok=true`.

## Follow-up: LAF Dry-run Notification Containment

- A manual LAF dry-run health check was found to traverse pending closing cases.
  Its validation warning path could still call the shared notifier, despite the
  run being dry-run. This produced operationally misleading closing-report
  notices in the LAF Discord channel.
- The affected Discord notices were removed after confirming that they came
  from the manual dry-run and not from a live closing action.
- `LAFOrchestrator` now supplies a process-local dry-run notifier which accepts
  every notifier method but only writes a concise local log entry. No dry-run
  path can load or send through the production Telegram/Discord notifier.
- The LAF skill self-test now imports the orchestrator and executes one
  read-only `SELECT 1` database probe. It no longer invokes the CLI dry-run
  mode, so a health check cannot enumerate pending cases.
- Added regression coverage for the dry-run notifier containment and the
  bounded read-only LAF self-test probe.

## Follow-up Verification

- Targeted regression suite: `82 passed`.
- Runtime LAF self-test: compiler, import, and read-only database probe passed.
- Runtime transcript self-test: imports, credentials, database probe, and
  verified TLS compatibility probe passed.
- Runtime business-module live check passed for LAF, file review, and
  transcript. The official `business_module_live_check_latest.json` was
  refreshed from that successful non-destructive check.
- Runtime `magi_doctor.py --json`: `55 pass / 0 warn / 0 fail`.
- Runtime self-repair guardian (`repair-safe`): no open issues and no pending
  manual repair. Function health reported `0` failed, stale, or missing health
  artifacts. Eight newly seeded scheduled jobs remain in their first-run grace
  window; the nightly autopilot is currently within its configured timeout.

## 2026-07-11 Autonomous Operations Hardening

- Reconciled dispatched cron jobs that exceeded their explicit timeout without
  a completion record. Cron state now records start and completion separately,
  redacts persisted output, and closes abandoned runs as timed out.
- Added one shared cron timeout policy for the Discord and daemon dispatchers.
  Seeding now assigns an explicit timeout to every enabled job without
  replacing existing custom values.
- Fixed runtime-root command quoting for paths under `Application Support` and
  scheduled function-health, guardian, and reporter jobs in dependency order.
- Made process timeout cleanup include nested, runner-owned process groups and
  fail explicitly if a child cannot be reaped.
- Made business-module and acceptance JSON contracts fail closed on missing,
  empty, or non-boolean result fields. Business reports are atomically written,
  redact legal identifiers and paths, and record notification delivery state.
- Kept file-review read-only checks free of dependency installation, event-log
  writes, token restoration, and default Gmail refresh. The worker stale reaper
  now requires run-id, PID, command, and age evidence before signalling a group.
- Made transcript synchronization acquire its ownership lock before database,
  schema, or portal work. LAF retry notifications expose only action labels and
  trace IDs, and upload staging requires an explicit completion sentinel.
- Added integrity checks and atomic metadata writes to database and OSC backup
  restore paths. Legacy OSC backups require an explicit unverified-restore opt-in.
- Hardened runtime JSONL appends with a cross-process lock and bounded tail
  reads. Active runtime-root fallbacks now derive from loaded code or environment
  instead of a Desktop path.
- Reworked the MAGI dashboard to use actual HTTP liveness, cron completion,
  guardian/function-health artifacts, NAS/model/credential state, business
  module reports, and parseable live logs. Failed/stale jobs sort ahead of
  healthy rows, and manual checks persist a visible failure result.
- Matrix-only CI/release artifacts are retained as durable acceptance evidence;
  only cron-owned health artifacts are subject to continuous freshness SLA.

## 2026-07-11 Verification

- Full regression: `4252 passed`.
- `compileall` and `git diff --check`: pass.
- Source/runtime hash comparison for the deployed change set: no mismatch.
- Runtime cron seed: `100` jobs, no enabled job missing `timeout_sec`, and no
  malformed shell tokenization.
- Restarted MAGI and verified daemon, server, Discord bot, Tools API, RPC worker,
  status bar, OSC NAS helper, website admin, four oMLX services, three NAS mounts,
  MariaDB, and zero zombies.
- Main server and Tools API health endpoints both returned success.
- Reloaded `com.magi.omlx-watchdog`; its live launchd environment now points to
  the installed runtime root.
- Non-destructive business LIVE check passed all LAF, file-review, transcript,
  token, NAS, Drive, and calendar checks. No notification was requested.
- Re-ran the repaired long jobs through the live scheduler contract:
  Obsidian source ingest, note repair, duplicate cleanup, Wiki vector reindex,
  Obsidian acceptance, and PDF bookmark-label repair all completed successfully.
  The PDF repair scanned `120` files, repaired `4` in this bounded batch, and
  reported `0` errors.
- Added an explicit non-destructive nightly diagnostic mode. It verifies the
  runtime root, run directory, required skills, cron definition, and production
  budget through the real nightly entry point without downloads, training,
  notifications, or business writes. The live diagnostic passed; the normal
  22:00 job remains a full run.
- Moved `63` legacy LAF upload-staging directories without ownership sentinels
  into `.runtime/quarantine/laf_upload_staging_20260711` after confirming they
  were strict MAGI run directories containing only PDF staging copies and no
  symlinks. Ten sentinel-owned directories remain under the 14-day retention
  policy.
- Final MAGI Doctor: `55 pass / 0 warn / 0 fail`.
- Final self-repair guardian: `0` open issues and `0` human-required actions.
- Final function health: `0 failed / 0 stale / 0 missing`.
- Staggered four colliding maintenance schedules without changing their daily
  frequency or work limits. Final operational audit reported `0` cron parse
  failures and `0` cron collisions; `/health?fresh=1` returned `operational`.

## Follow-up: LAF Attachment Retry Monitor

- Enabled `MAGI_LAF_PORTAL_RETRY_ON_START=1` in the installed runtime after the
  dashboard correctly exposed that the attachment retry loop was disabled.
- Moved initial queue seeding and retry work into the daemon thread so enabling
  attachment recovery cannot delay HTTP or Gmail-monitor startup.
- Added an atomic, public-safe `static/laf_portal_retry_state.json` heartbeat.
  It contains only status, timestamps, interval, and aggregate counts; it does
  not persist case numbers, names, filenames, or raw errors.
- MENUBAR now reads the heartbeat first. `waiting`, `starting`, `idle`, and
  stale states are yellow; only an explicit error or stopped state is red.
- LIVE first cycle: heartbeat changed from `starting` to `ok`, scanned `15`
  pending entries, processed `6`, and reduced the pending queue to `14` after
  confirming one attachment was already present on NAS.
- The restart also reconciled an old weekend-bookmark dispatch without a
  completion marker. Added a `330` minute internal soft budget beneath its
  six-hour scheduler timeout. A non-destructive live plan scanned `2,056` PDFs
  in `104` seconds and completed successfully without modifying PDFs.
