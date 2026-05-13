# 2026-04-26 Monitoring / Calendar / Cron Hardening

## Scope

使用者要求將可見風險排除，且必須維持監控模組與三個業務模組（閱卷 / 筆錄 / 法扶）正常。本輪不處理 LAF progress Plan C live 送出驗收，也不恢復 Gemini 功能。

## Completed fixes

- Nightly autopilot: fixed `_user_active_defer` definition order so the 00:00 judicial API step can defer safely before `run_tick()` initializes result aggregation.
- Morning health report: top-level nightly failure JSON (`ok:false` without `details.steps`) now surfaces as `_nightly_run` instead of "unable to parse steps".
- Cron monitoring: added `skills/ops/cron_result_policy.py` and routed Discord cron execution through SafeProcess argv parsing instead of shell execution; strong success stdout no longer creates false-positive issue agenda records.
- SafeProcess: added guarded `env_extra` support with whitelist prefix filtering.
- Obsidian ingest: expanded bad-PDF isolation hints for trailer/xref/open-file failures so one malformed PDF does not break the batch.
- Calendar import: added optional incremental Google Calendar syncToken mode with HTTP 410 reset fallback to bounded full sync.
- Gmail monitor audit: operational hardening audit now records the current monitor mode and enforces a future push/history requirement.
- Legal hallucination guard: grounding checks now cover concrete court/case citations, not only statute article references.
- OCR scan quality: added effective-DPI assessment with 300 DPI good / 400 DPI excellent guidance and low-quality rescanning recommendations.
- Disk cleanup: added `--dry-run` / `--apply` flags and a default 20 GiB delete safety cap for oMLX cache cleanup.
- LAF parser guard: fixed bracketed anonymized client names like `[當事人G]` in report commands without touching portal automation core.

## Validation

### Tests

- `tests/test_cron_monitoring_hardening.py tests/test_safe_process.py tests/test_osc_gcal_sync_dedup.py tests/test_hallucination_guard.py tests/test_ocr_quality.py tests/test_obsidian_ingest_source.py tests/test_disk_cleanup_healthcheck.py` → `94 passed`
- System/security/PDF/calendar protection set → `115 passed`
- Three business module protection set (`file-review`, `transcript`, `LAF`) → `155 passed`
- `tests/test_laf_handler.py` local parser regression → `10 passed`

### Live / operational verification

- `magi restart` completed; fresh PIDs observed for daemon/server/Discord bot/tools API/RPC worker/status bar.
- `magi status`: daemon, server, Discord bot, Tools API, RPC worker, status bar, oMLX text/embed all up; zombies 0.
- `GET http://127.0.0.1:5002/health`: `status=operational`, `operational_health.ok=true`, DB/NAS/oMLX/OCR ok.
- `GET http://127.0.0.1:5003/health`: Tools API `status=ok`.
- `scripts/ops/audit_operational_hardening.py`: `cron_parse_failures=0`, `cron_collisions=0`, `gmail_monitor_mode=polling`.

## Notes

- The first health probe to port 8000 failed because MAGI's documented main API port is 5002. Final live verification used 5002/5003.
- FAISS reported `startup_grace_period` immediately after restart but still `ok=true`; overall health remained `operational`.
- Pre-existing dirty files not owned by this work remain untouched: `skills/transcript-downloader/action.py`, `static/knowledge_lint_latest.json`, and prior desktop cleanup fix docs.
