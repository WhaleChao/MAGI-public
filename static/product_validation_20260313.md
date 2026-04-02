# MAGI Product Validation 2026-03-13

## Scope

- file-review
- transcript
- laf

## Automated checks completed

- `pytest`
  - `tests/test_product_runtime.py`
  - `tests/test_inference_gateway.py`
  - Result: `11 passed`
- `file-review-orchestrator --task self_test`
  - Result: pass
- `file-review-orchestrator --task db_smoke`
  - Result: pass
  - Active DB profile: `Studio_VPN_Remote`
- `transcript-downloader --task self_test`
  - Result: pass
- `transcript-downloader --task db_probe`
  - Result: pass
  - Eligible cases found: `70`
- `laf-orchestrator --task self_test`
  - Result: pass

## Mock / test environment validation

- Full mock end-to-end suite:
  - Command: `mock_skill_test.py --skills all --no-stop`
  - Result: `18 PASS / 0 FAIL / 0 SKIP`
- LAF runtime smoke on test portal:
  - Command: `MAGI_LAF_PORTAL_ENV=test laf_portal_smoke_test.py --headless 1 --probe-only 1`
  - Result: pass
  - Executed base URL: `http://127.0.0.1:17002`
  - Workflows probed: `go_live`, `condition`, `inquiry`, `withdrawal`, `fee`

## Production-path validation

- Transcript real-site smoke login:
  - Result: pass
  - Artifact dir: `_judicial_smoke/transcript_20260313_082708`
- File-review real-site case probe:
  - Result: pass
  - Case: `TPD 114年 訴字第83號`
  - Probe result: `Ready`
  - Artifact dir: `_judicial_smoke/file_review_probe_20260313_082756`
- LAF production probe-only comparison:
  - Command: `MAGI_LAF_PORTAL_ENV=production laf_portal_smoke_test.py --headless 1 --probe-only 1`
  - Result: pass
  - Executed base URL: `https://lawyer.laf.org.tw`
  - Workflows probed: `go_live`, `condition`, `inquiry`, `withdrawal`, `fee`

## Test vs production comparison

- LAF `test`:
  - probe action counts were non-zero across all five workflows, confirming the mock/test portal routing and selectors are active.
- LAF `production`:
  - login succeeded and all five workflow searches completed successfully.
  - action counts were zero for the sampled cases, so the run stayed read-only and skipped draft writes.

## Product hardening completed in this run

- Added shared per-product runtime profile with independent `codex_mode` and LAF `portal_env`
- Wired file-review, transcript, and LAF entrypoints to the shared product runtime
- Added context-aware Codex gating so each product can stay `local`, `auto`, or leave room for `codex`
- Updated `laf_portal_smoke_test.py` to honor product runtime targets and support `probe-only`

## Outcome

- Status: product-grade validation complete for the currently reachable environment
- Blocking failures: none
