# MAGI final stabilization audit - 2026-05-09

This is the remaining work list before MAGI can be treated as an autonomous, low-maintenance operator.

## Live state snapshot

- Core health: DB OK, NAS `homes`/`lumi` OK, OCR OK, FAISS OK.
- Model health endpoint: E4B on 8080, Phi-4 on 8082, SmolLM3 on 8083 all reachable.
- Overall health: still `degraded`.
- Operational audit: 61 enabled cron jobs, 2 cron collisions, 3 active unresolved issue-agenda failures, 9 source/review files still dirty.
- Core route smoke: 6 PASS, 1 WARN, 0 FAIL.
  - WARN: judgment search reports missing API key.
  - Hidden live issue: summary path tried `gemma-4-26b-a4b-it-4bit` while day service exposes only `gemma-4-e4b-it-4bit`; fallback recovered, but this is still a model routing bug.

## P0 blockers

1. Model routing is not single-source-of-truth.
   - Health says day E4B is live, but summary/Balthasar bridge still attempted 26B.
   - Fix: all inference clients must read the live model registry or `/v1/models`, not hardcode day/night model names.
   - Acceptance: `scripts/ops/smoke_core_routes.py` runs with no timeout and no 26B-not-found warnings during day mode.

2. Operational audit is intentionally failing.
   - Current collisions:
     - `0 0 * * *`: `job_judicial_api_night_pull` vs `job_case_index_sync`.
     - `0 8 * * *`: `job_worldmonitor_intel` vs `job_gcal_sync`.
   - Fix: stagger jobs and add per-resource locks for heavy DB/NAS/model jobs.
   - Acceptance: `audit_operational_hardening.py` reports zero cron collisions.

3. Issue agenda has active unresolved failures.
   - `job_weekend_bookmark`: SIGTERM while processing large PDFs.
   - `job_omlx_switch_day`: port 8080 close timeout.
   - `job_operational_hardening_audit`: fails because it correctly detects collisions/dirty files.
   - Fix: make these either pass or become explicitly classified as stale/recovered/non-blocking.
   - Acceptance: active unresolved issue count is zero.

4. Self-check command is misrouted.
   - `@MAGI 自動巡檢` was treated as casual chat and answered with a question instead of running health probes.
   - Fix: route self-check/audit phrases to deterministic health tooling.
   - Acceptance: scheduled self-check emits a structured status report, not a conversational prompt.

5. Public release is not yet a clean artifact.
   - Public repo has a `main`, but the precise LAF/archive mechanism is not yet ported and verified there.
   - Fix: migrate only generic OSC archive hardening, omit secrets/private data/LAWSNOTE-specific features, then run `scripts/public_release_audit.py`.
   - Acceptance: public branch passes public audit and archive tests, then push to the public repo.

## P1 conflicts

1. Case lifecycle status semantics conflict.
   - `status` means case/procedure lifecycle; `legal_aid_status` means LAF workflow lifecycle.
   - Recent fix: final `legal_aid_status = 已結案` can archive exactly that case folder; same-name active procedures remain active.
   - Remaining: apply the same exact-folder logic in public and any other status update paths.

2. NAS/Synology Drive/SMB path views can duplicate folders.
   - Same case may appear through CloudStorage and `/Volumes/homes`.
   - Recent fix: archive cleanup removes duplicate active-path residuals after exact archive.
   - Remaining: run a broader residual scan for all closed LAF cases and add a scheduled dry-run report.

3. Launchd and daemon cron overlap.
   - Many LaunchAgents are installed but not loaded, while daemon cron also runs related jobs.
   - Failed/odd launchd statuses: `com.magi.rpc` -15, `com.magi.mlx-mtp` -15, `com.magi.omlx` -15 while the service is nevertheless reachable, paperclip share gateway 78, worldmonitor dev 78.
   - Fix: classify each service as daemon-managed, launchd-managed, on-demand, or retired. Remove duplicate ownership.

4. Share tunnel is unstable.
   - Logs show repeated quick-tunnel URLs and `getcwd` errors.
   - Fix: make the gateway/tunnel use a stable working directory and one live process; avoid infinite quick-tunnel churn.

5. Gmail/LAF email polling is fragile.
   - Live daemon log contains Gmail `BrokenPipeError`.
   - Gmail monitor is polling, not push/history.
   - Fix: retry with backoff, classify transient network failures, and add full-sync backstop if push is enabled later.

## P1 live issues

1. Judgment search degraded by missing API key.
   - Smoke route reports: `unauthorized: missing API key`.
   - Fix: either configure the key, or make local DB/Judicial fallback the default when key is absent.

2. Night model switch cannot always start 26B.
   - Recent issue: night switch aborted because available memory was 7GB and requirement was 8GB.
   - Fix: night mode must degrade cleanly to E4B/Phi/Smol with explicit status, not leave callers expecting 26B.

3. World news update has scheduling and output quality risks.
   - 08:00 job collides with GCal sync.
   - Earlier output showed stale date and Markdown/English leakage.
   - Fix: separate schedule, enforce fresh source date, Chinese plain-text renderer, and report if source freshness is stale.

4. PDF/bookmark and PDF-namer benchmarks are not quiet.
   - Weekend bookmark was SIGTERM.
   - PDF namer benchmark has historical sanitizer warnings and active regression history.
   - Fix: chunk large PDFs, cap per-run time, persist resumable state, and distinguish warning from true failure.

## Designed but not fully open / still shadowed

1. OCR consensus / Nemotron parsing.
   - Tests show consensus, shadow, and Nemotron flags exist.
   - Risk: multiple OCR paths can disagree; production mode must be explicitly chosen.

2. Docling layout extraction for pdf-namer.
   - `MAGI_PDF_NAMER_DOCLING_ENABLED` defaults disabled.
   - Risk: designed improvement exists but is not part of live reliability unless benchmarked and enabled.

3. NVIDIA NIM heavy path.
   - Tests gate it with `NVIDIA_NIM_ENABLE`; issue agenda shows missing key/rate-limit cases.
   - Risk: cloud fallback can silently degrade or stall heavy jobs.

4. Public installer and detection modules.
   - Installer exists, but not yet proven against a clean public repo + clean machine scenario.
   - Risk: public users hit private path assumptions or missing service setup.

5. Public sharing tunnel.
   - Designed, but current live process churn makes it unsuitable as a reliable public sharing primitive.

## Final improvement rounds

### Round 1 - Control plane hardening

- Fix cron collisions.
- Fix self-check routing.
- Normalize launchd/daemon ownership.
- Make operational audit pass with no active unresolved issues.

### Round 2 - Model and tool routing

- Centralize live model selection.
- Remove day/night hardcoded model names from summary/bridge paths.
- Re-run all tool-intent tests: weather, calendar, folders, files, LAF, documents, news, judgments, stocks.
- Acceptance: no "asked schedule, answered weather" class errors.

### Round 3 - Storage and OSC lifecycle

- Finish exact LAF archive migration to public.
- Add residual closed-case folder scanner.
- Re-test card folder open/file browser after archive.
- Re-test OSC stats: visits, prison meetings, contacts, document counts, and debt checklist logic.

### Round 4 - Data ingestion and daily intelligence

- Fix world news freshness and Traditional Chinese plain-text output.
- Fix Gmail/LAF polling resilience.
- Fix GCal multi-calendar sync consistency.
- Fix Judicial API missing-key fallback.

### Round 5 - Public release

- Apply only generic fixes to `MAGI-public`.
- Remove private data, keys, and LAWSNOTE-dependent surfaces.
- Run `scripts/public_release_audit.py`.
- Run clean install smoke test.
- Push public branch only after audit is green.

### Round 6 - 24h soak

- Run `observe_stability_24h.py` or equivalent.
- Required exit criteria:
  - health not degraded,
  - zero active unresolved issue-agenda failures,
  - no cron collisions,
  - no stale model service,
  - NAS paths stay mounted and deduped,
  - core route smoke has no WARN/FAIL.

