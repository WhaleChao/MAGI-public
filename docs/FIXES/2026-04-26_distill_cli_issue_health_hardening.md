# 2026-04-26 Distill / CLI / Issue Health Hardening

## Scope

使用者要求排除 LAF 以外的剩餘可見問題，實作交由 GPT-5.3 worker，GPT-5.5 負責整合審查與驗收。LAF portal / progress 真送出驗收未處理，Gemini 功能未恢復。

## Completed fixes

- `magi status` memory display:
  - Fixed the `~0.0?GB` display caused by a `pipefail` pipeline fallback.
  - Added PID-based RSS aggregation for MAGI core processes and oMLX ports 8080-8083.
  - Synced the verified script to `/opt/homebrew/bin/magi`, not only the repo copy.

- Operational issue noise:
  - Split cron issue state into `active_unresolved`, `recovered`, `superseded`, `stale`, and `false_positive`.
  - `/health.operational_health` now exposes `active_unresolved_24h`, `inactive_breakdown_24h`, and marks `raw_counts_24h.for_context_only=true`.
  - Red-light behavior remains based on active unresolved failures.
  - `scripts/ops/audit_operational_hardening.py` now includes issue `state` and `recent_state_counts`.

- Gemma E4B distillation validation gate:
  - `train_gemma_e4b_lora.py` now rejects channel marker leaks (`<|channel>`, `<|channel>thought`), English thinking traces, simplified Chinese, insufficient Traditional Chinese content, and too-short output.
  - Validation prompts now include `/no_think` and final-answer-only instructions.
  - `nightly_distill_gemma.py` refuses to write `pending_deploy.json` when validation gate fails.
  - Second-pass check found the old v001 `pending_deploy.json` still had a deploy command from the false-positive validation run. It is now marked `status=rejected`, `deploy_allowed=false`, and the deploy command is disabled.
  - `nightly_distill_gemma.py --deploy <version>` now refuses deployment when the pending record for that version is rejected/blocked.
  - Added `tests/test_gemma_distill_validation_gate.py`.

- Repo hygiene:
  - No `.gitignore` change needed. Current untracked files are source/test/fix-note artifacts, not runtime pollution.

## Validation

### Tests

- Distill / cron / SafeProcess / issue-health targeted set:
  - `tests/test_gemma_distill_validation_gate.py`
  - `tests/test_cron_monitoring_hardening.py`
  - `tests/test_safe_process.py`
  - `tests/test_admin_runtime_blueprint.py::test_operational_issue_health_reconciles_recovered_and_false_positive`
  - Result before rejected-pending guard: `39 passed`
  - Distill validation guard after rejected-pending patch: `10 passed`
  - Manual deploy refusal smoke: `nightly_distill_gemma.py --deploy gemma-distill-v001` returned `rc=1` with "Refusing to deploy rejected Gemma distill version".

- Three business module protection set:
  - file-review / transcript / LAF focused test files
  - Result: `155 passed`

- Syntax / compile:
  - `py_compile` passed for distill scripts, admin runtime, audit hardening, Discord cron execution, SafeProcess, and cron result policy.
  - `bash -n` passed for both `scripts/magi_cli.sh` and `/opt/homebrew/bin/magi`.

### Live verification

- `magi restart` completed and fresh PIDs observed for daemon, server, Discord bot, Tools API, RPC worker, and status bar.
- oMLX night switch was already running after 21:50; it completed normally. Final state:
  - Text 8080 reachable with `gemma-4-26b-a4b-it-4bit` night profile.
  - Phi-4 8082 reachable.
  - SmolLM3 8083 reachable.
  - Embed 8081 reachable.
- `GET http://127.0.0.1:5002/health`: `status=operational`, `operational_health.ok=true`, `cron_failures_24h=0`, `active_unresolved_24h.cron_failures=0`, `raw_counts_24h.for_context_only=true`.
- `GET http://127.0.0.1:5003/health`: Tools API `status=ok`.
- `scripts/ops/audit_operational_hardening.py`: `cron_parse_failures=0`, `cron_collisions=0`, `gmail_monitor_mode=polling`.
- `magi status`: no `~0.0?GB`; final memory estimate displayed as `~1.2GB (MAGI + oMLX)`.
- Second-pass live check after rejected-pending guard: `/health.status=operational`, `operational_health.ok=true`, `omlx.ok=true`, `active_unresolved_24h.cron_failures=0`.

## Notes

- The live text model is 26B because the existing 21:50 night switch was active during validation. This is expected night profile behavior, not a distillation deployment. The rejected v001 distill artifacts remain undeployed.
- No LAF portal core file was intentionally changed in this round.
