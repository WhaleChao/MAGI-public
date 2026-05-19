# PDF Bookmarker Batch No-Boundary Handling

Date: 2026-04-26

## Summary

MAGI had pdf-bookmarker schedules enabled, but historical state showed the full backfill was not completing: `.agent/bookmark_batch_state.json` had only 22 completed files and 1 vision-refined file, while a 2026-04-18 weekend batch notification reported 628 PDFs found, 17 processed, 4 skipped, and 607 errors.

Root cause addressed in this fix: `scripts/weekend_bookmark_batch.py` treated expected regex misses such as `未偵測到文件邊界，無法產生書籤` as errors and did not write them to state. The next scheduled run would retry the same no-boundary PDFs instead of advancing cleanly or allowing the vision stage to consider them.

Follow-up found during repeated smoke testing: after a successful bookmark write, Stage 1 stored the pre-write PDF `mtime`. Because writing bookmarks changes the PDF, the next run saw a newer `mtime` and reprocessed the same successful file. The success path now stores the post-write `mtime`.

## Changes

- `scripts/weekend_bookmark_batch.py`
  - Added `_is_stage1_no_hit_result()`.
  - Stage 1 now records normal no-boundary misses in `completed` with `stage1=True`, `stage1_bookmarks=0`, `no_boundary=True`, and the original message.
  - No-boundary misses are counted separately from real errors and shown in logs/summary.
  - Successful writes now store the post-write `mtime`, preventing repeated processing of already-successful files.

- `skills/pdf-bookmarker/action.py`
  - Added MAGI root to `sys.path` for direct CLI execution.
  - Fixed `doc_type_detector` self-test import path so scheduled self-test reflects the actual dependency state.

- `tests/test_weekend_bookmark_batch.py`
  - Added focused tests for no-boundary state recording and exception handling.
  - Added regression coverage for post-write `mtime` storage and same-state rerun skipping.

## Verification Level

測試, not live E2E.

Commands run:

```bash
venv/bin/python3 skills/pdf-bookmarker/action.py --task self_test
venv/bin/python3 -m pytest tests/test_weekend_bookmark_batch.py tests/test_skill_contract_pdf_bookmarker.py tests/test_bookmark_validator.py -q
venv/bin/python3 skills/pdf-bookmarker/action.py --task test --path '/Users/ai/Desktop/MAGI_v2/閱卷下載/20260414/114_偵_007016_DOC_002_1150206111523.pdf' --dry-run
venv/bin/python3 -m pytest tests/test_weekend_bookmark_batch.py tests/test_skill_contract_pdf_bookmarker.py tests/test_bookmark_validator.py tests/test_doc_type_detector.py -q
venv/bin/python3 -m compileall -q scripts/weekend_bookmark_batch.py skills/pdf-bookmarker/action.py tests/test_weekend_bookmark_batch.py
venv/bin/python3 scripts/ops/benchmark_pdf_bookmarker.py
```

Results:

- `self_test`: exit 0; all checks true; no warnings after switching the synthetic PDF to an extractable CJK font fixture.
- pytest subset: `32 passed`.
- Real no-boundary sample still returns the expected no-boundary message, which the batch classifier now treats as a non-error state entry.
- Isolated two-file Stage 1 smoke:
  - first run: `processed=1`, `bookmarks=2`, `no_boundary=1`, `errors=0`
  - second run with same state: `processed=0`, `skipped=2`, `errors=0`
- CLI smoke on temp copies:
  - `scan_file`: exit 0 and writes 2 bookmarks.
  - `batch`: exit 0; no-boundary PDF is skipped without failing the batch.
- `compileall`: passed.
- Live benchmark dry-run on real case PDFs: `[PASS] bookmark_recall=80.0% empty_rate=20.0% label_match_rate=63.2%`.

## Runtime Impact

No `magi restart` required. The changed files are invoked by scheduled jobs as standalone scripts, so the next run will pick up the patch.

## Current Coverage Finding

As of this investigation:

- `job_nightly_bookmark_regex`, `job_weekend_bookmark`, `job_benchmark_pdf_bookmarker`, and `job_pdf_bookmarker_self_test` are enabled.
- `.runtime/cron_state.json` shows recent runs:
  - `job_nightly_bookmark_regex`: 2026-04-26 02:15
  - `job_weekend_bookmark`: 2026-04-25 03:00
  - `job_pdf_bookmarker_self_test`: 2026-04-26 03:20
- Local `閱卷下載` sample count: 78 PDFs, 5 with bookmarks, 73 without bookmarks.

This fix removes one backfill blocker; it does not mean all PDFs are already bookmarked. A later live backfill run is still needed to move coverage toward full completion.
