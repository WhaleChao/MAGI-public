# 2026-04-26 Desktop MD Cleanup

## Scope

The user asked to confirm whether Markdown files on `/Users/ai/Desktop` were completed, then record completed items in shared project records and remove them from the Desktop.

`CLAUDE.md` was read before and after the cleanup. Per the current CLAUDE.md logging rule, one-time document cleanup details are recorded here instead of appending a date log to CLAUDE.md.

## Archived Completed Desktop Files

The following files were completed or already superseded by `CLAUDE.md` standing records / existing fix records. They were moved off the Desktop to:

`docs/archive/desktop_plans_20260426/`

- `CLAUDE_md_重構設計報告_20260426.md` — completed; current `CLAUDE.md` is already the restructured standing-rule file.
- `MAGI_對話智慧改善計畫.md` — completed; intent classification / search trigger / deterministic memory-summary work is already implemented and tracked.
- `MAGI_v2_Codex下線_Gemma蒸餾_執行計劃_20260425.md` — completed through the Codex-offline/Gemma distillation records in `CLAUDE.md`.
- `MAGI_v2_LAF_progress零次數自動填_改善計劃_20260426.md` — completed and live-verified; standing impact recorded in `CLAUDE.md` section 5.3.
- `MAGI_v2_LAF已報結狀態流轉改善計劃_20260426.md` — completed and migrated; standing impact recorded in `CLAUDE.md` section 5.3.

## Kept On Desktop

These files are not completed or are outside MAGI scope, so they remain on the Desktop:

- `AcroPDF_開發計劃.md` — separate AcroPDF product plan, not a MAGI completed item.
- `MAGI_v2_LAF_progress二階段確認碼_執行計劃_20260426.md` — implementation tests are done, but live verification requires a real progress submission chosen by the user.
- `MAGI_v2_未完成項目彙整_20260426.md` — updated to list only remaining active / pending items.

## Follow-up

- `Gemini_CLI_Subscription_Fallback_Plan.md` was later removed from the Desktop at the user's request; Gemini CLI is no longer tracked as a pending MAGI plan.
- Calendar Phase 3 was later verified complete: Calendar duplicate audit found zero duplicate groups / zero delete candidates; `case_todos` duplicate source check found zero groups; live `gcal_sync` ran with `MAGI_GCAL_DEDUP_DRY_RUN=0`.

## Validation

- Desktop Markdown files reduced from 9 to 4.
- Archived files are present under `docs/archive/desktop_plans_20260426/`.
- `CLAUDE.md` references for completed moved plans were updated away from Desktop paths.
- `CLAUDE.md` line count remained below the 500-line guard.
