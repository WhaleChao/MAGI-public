# 2026-05-10 — 30-day cooldown / dedup audit

## Result

Executable-code audit found three business dedup surfaces that still mention a 30-day window:

1. `file_review_automation.py` payment notices
   - `PAYMENT_NOTIFY_COOLDOWN_HOURS = 720`
   - Fixed: `web_payment:*` keys are now permanent; only non-payment notification keys can expire.

2. `laf_orchestrator.py` condition drafts
   - `_was_condition_drafted_recently(..., days=30)`
   - Already safe: successful draft / manual-done records are permanent dedup signals. The `days` argument remains only for caller compatibility.

3. `laf_orchestrator.py` closing drafts
   - `_was_closing_drafted_recently(..., days=30)`
   - Already safe: draft / success / pending records are permanent dedup signals. The `days` argument remains only for caller compatibility.

## Non-business 30-day uses

The remaining 30-day values are not user-facing dedup windows:

- export / job / backup cleanup retention
- OSC share-link maximum TTL
- Google Calendar lookahead horizon
- insight sync performance window
- recent-activity state pruning; source records are only loaded from recent days, so it does not replay old payment notices

## Guardrails

- `tests/test_file_review_notifications.py` prevents old payment-registry PDFs from being re-sent.
- `tests/test_laf_hardening_guards.py` prevents LAF condition / closing dedup from regressing to SQL date-window checks.
