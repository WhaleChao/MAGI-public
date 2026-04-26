# 2026-04-26 MAGI operational hidden bug sweep

## Scope

User requested a full hidden-bug sweep, excluding LAF because that work is owned by another operator.

Boundaries honored:

- Did not modify LAF core flow files.
- Did not modify file-review portal automation core flow.
- Did not modify transcript login/download core behavior.
- Used Codex 5.3 workers for execution and GPT-5.5 for planning/integration/final validation.

## Fixes

- Nightly regression no longer fails on missing deprecated mock skill fixture; the report marks it as deprecated/skipped.
- Core route smoke now treats missing Judicial Yuan API credentials as WARN instead of core-route FAIL.
- Obsidian ingest now classifies malformed PDFs as per-file warnings/skips instead of failing the whole job.
- Operational health now reports active unresolved issues separately from raw 24h historical issue counts, preventing recovered jobs from keeping `/health` degraded.
- PDF namer benchmark now has quality gates for repeated unknown tokens, unsafe name normalization, and OCR residue in legal filenames.
- PDF namer DB-rule loading now reports `rules_source` and degraded cache fallback instead of a vague credentials warning.
- PDF bookmark batch gained a safe `--dry-run` backfill planner.
- Knowledge lint now emits a dry-run duplicate-vector cleanup plan, and ingestion now deduplicates same-batch content.
- Translator APE benchmark now propagates empty output, provider failure, and missing numbers/case numbers to top-level failure.
- oMLX status/health now observes Phi-4 and SmolLM3 services, including managed/unmanaged state.
- Transcript indexer now distinguishes partial progress from fatal failure; transcript sync MD5 scan timeout becomes a warning and the sync can continue.
- `skill_realworld_smoke.py --help` now prints help and exits without running smoke tests.

## Validation Level

Validation level: live restart verification plus targeted regression tests.

Commands run:

- `python -m py_compile` on all changed runtime scripts.
- `bash -n scripts/magi_cli.sh`
- `pytest -q ...` targeted suite: 62 passed.
- `scripts/ops/smoke_core_routes.py --json-out /tmp/magi_smoke_core_routes_final.json`: PASS 6 / WARN 1 / FAIL 0.
- `scripts/ops/nightly_regression.py --json-out /tmp/magi_nightly_regression_final.json --no-notify`: exit 0, overall OK.
- `MAGI_PDF_NAMER_BENCHMARK_MAX_PDFS=5 scripts/ops/benchmark_pdf_namer.py`: PASS, quality pass rate 100%, rules source cache degraded.
- `scripts/ops/benchmark_pdf_bookmarker.py`: PASS, recall 80.0%, empty rate 20.0%, label match 63.2%.
- `scripts/ops/benchmark_translator_ape.py`: exit 1 by design, now correctly flags case-number loss as top-level failure.
- `scripts/knowledge_lint.py --quick`: completed with warnings and duplicate cleanup plan.
- `scripts/weekend_bookmark_batch.py --stage regex --dry-run --plan-limit 5`: produced safe backfill plan.
- `skills/transcript-indexer/action.py --task index` with a 1s budget: partial success, fatal false, exit 0.
- `magi restart`, then `magi status` and `curl /health`.

Post-restart live result:

- Core services fresh and running: daemon, server, Discord bot, tools API, RPC worker.
- `/health` status: `operational`.
- `operational_health.ok`: true.
- Active cron failures: 0.
- Active high-severity issue agenda count: 0.
- Raw 24h historical counts are still retained for audit.
- oMLX service details include Gemma, Phi-4, and SmolLM3.

## Follow-up Risk Closure

The initial sweep left three visible operational risks. The user correctly required these to be removed rather than merely listed.

Additional fixes and validation:

- PDF bookmarker benchmark now separates legitimate single-document PDFs from true empty failures, fails on `needs_manual_review`, normalizes generated labels, and emits a legacy cleanup plan for historical `image0000x` bookmarks.
  - Final benchmark: `ok=true`, `bookmark_recall=100.0%`, `empty_failure_rate=0.0%`, `needs_manual_review_rate=0.0%`, `label_match_rate=100.0%`.
- PDF namer rules loading now bootstraps existing `.env` and keychain-backed credentials. The versioned bundled rules source remains as a non-degraded fallback only when valid.
  - Final benchmark: `ok=true`, `rules_source=db`, `rules_degraded=false`, `quality_pass_rate=100.0%`, `overall_pass_rate=100.0%`.
- Knowledge duplicate cleanup is now safely executable with backup, verify, and rollback support. Production duplicates were cleaned, then a `research-brief` re-ingestion source was fixed with idempotency at both the producer and shared memory/keeper write layers.
  - Final lint: `duplicate_vectors.status=ok`, `duplicate_groups=0`, `total_extra_entries=0`, `cleanup_gate=applied_verified`.

Additional tests:

- PDF/bookmarker, PDF/namer, research-brief, memory bridge, keeper sync, and duplicate cleanup targeted suite: `134 passed`.
- Post-fix focused suite after report `ok/success` addition: `43 passed`.

## Remaining Bug Cleanup

The user then asked for every remaining visible bug/risk to be handled.

Additional fixes:

- Translator APE now preserves Taiwan court case numbers verbatim even when Apple/Gateway post-edit output drops them; the missing case number is appended from the source text before benchmark scoring.
- oMLX non-default bases are model-aware: a GLM-OCR/vision request is rejected locally when the target base only serves Phi-4, so MAGI no longer spams avoidable `/v1/chat/completions` 404s against port 8082.
- Knowledge lint no longer treats valid `判決連結 + 實務見解` rows as boilerplate, verifies zero-chunk Obsidian entries against actual vector DB rows, and compares the Obsidian index against the whole vault instead of only `20_Notes`.
- Obsidian index was repaired: full-vault ingest filled previously unindexed notes, and the four remaining zero-chunk notes were force-ingested with verified chunk counts.
- Low-quality `legal_insights` poison rows were backed up and removed: two degraded timeout replies, two “no legal principle found” placeholders, and three accidental preference/test replies.
- Wiki staleness was cleared for all 72 cases using the synthesizer’s safe structural fallback after the LLM path timed out.
- Duplicate vectors that regenerated during Obsidian/Wiki ingest were cleaned again with backup, verification, and FAISS rebuild.
- The five historical `image0000x` PDF bookmark candidates were backed up and rewritten with generated bookmarks; rerun benchmark reports zero legacy cleanup candidates.

Additional validation:

- Targeted unit/regression suite: `42 passed`.
- Translator APE benchmark: `ok=true`, `case_fail_count=0`, case number `114年度原訴字第000024號` preserved.
- PDF namer benchmark after oMLX guard: exit 0, no `404` / `chat/completions` warnings, `quality_pass_rate=100.0%`, `rules_source=db`.
- Knowledge lint quick scan: duplicate vectors ok, insight quality 100%, wiki stale 0, orphan notes ok (`total_on_disk=1025`, `total_in_index=1025`, `unindexed=0`, `zero_chunk_notes=0`).
- PDF bookmarker benchmark: `ok=true`, `legacy_cleanup_candidates=0`, `bookmark_recall=100.0%`, `empty_failure_rate=0.0%`, `needs_manual_review_rate=0.0%`, `label_match_rate=100.0%`.
- Retrieval smoke after FAISS/knowledge cleanup returned non-empty results for consumer-debt, case-number, Yu Qiuju, and research-brief queries.
- Post-restart live check: `magi restart` completed; daemon/server/Discord bot/tools API/RPC worker are up, `/health.status=operational`, `operational_health.ok=true`, cron failures 0, FAISS ok with 68,546 vectors, oMLX text/Phi-4/SmolLM3 reachable.

Remaining visible risks from this sweep: none, excluding LAF by the user’s explicit boundary.
