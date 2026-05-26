# PDF Bookmarker OCR Follow-Up Flow

Date: 2026-05-26

## Summary

MAGI now separates PDF bookmarking into bounded stages so large court records, watermark-only PDFs, and scanned evidence files do not keep returning as ambiguous `no_boundary` failures.

The root operational issue was that Stage 1 could silently call Vision fallback on every regex miss. For large court records this made the fast pass slow, unpredictable, and likely to time out before it could record a useful next step.

## Changes

- `scripts/weekend_bookmark_batch.py`
  - Stage 1 disables hidden Vision fallback by default.
  - Adds per-file timeout handling.
  - Adds native text sampling that ignores court platform and lawyer watermark text.
  - Reclassifies poor-text large PDFs as `needs_ocr` instead of generic `no_boundary`.
  - Writes page-1 bookmarks for legitimate single-document PDFs.
  - Writes `.runtime/bookmark_followup_plan_latest.json`.
  - Can enqueue `ocr_then_bookmark` files into `~/.magi_nas_ocr_queue.db`.

- `tests/test_weekend_bookmark_batch.py`
  - Covers single-document bookmark fallback.
  - Covers large no-boundary PDF reclassification to OCR.
  - Covers timed-out OCR PDFs being routed to split/off-peak follow-up.

- `skills/pdf-bookmarker/SKILL.md`
  - Documents the Stage 1 / OCR follow-up / Stage 2 Vision workflow.

- `cron_jobs.json`
  - Nightly and weekend bookmark jobs now write the follow-up plan and enqueue OCR follow-ups.
  - Adds `job_nas_pdf_ocr_worker_offpeak`, which processes one OCR item at a time during off-peak slots and skips automatically when disk or memory headroom is too low.

## Live Verification

Target case:

`/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件/無償案件/刑事/2026-0054-李昆懋-再審-毒品危害防制條例/06_證據資料`

Result:

- Total PDFs: 147
- Completed for this case: 147
- Bookmarked PDFs: 142
- Total bookmarks recorded in state: 3854
- No-boundary PDFs: 0
- Stage 1 errors: 0
- Remaining OCR follow-ups: 5
- OCR queue status: 5 pending

The five remaining files are true scanned/poor-text large PDFs and are now queued for OCR rather than being treated as generic bookmark misses.

## Test Commands

```bash
venv/bin/python3 -m compileall -q scripts/weekend_bookmark_batch.py
venv/bin/python3 -m pytest tests/test_weekend_bookmark_batch.py tests/test_skill_contract_pdf_bookmarker.py -q
venv/bin/python3 skills/documents/nas_pdf_ocr_worker.py status
python3 -m json.tool cron_jobs.json
```

Results:

- `32 passed`
- OCR queue contains the five expected pending follow-ups.
- Cron JSON validates and has no duplicate job ids.

## Remaining Quality Note

Boundary routing and OCR classification are fixed. Some generated bookmark labels can still include misleading header or watermark dates, so date extraction quality should continue to be improved through validator training and examples.
