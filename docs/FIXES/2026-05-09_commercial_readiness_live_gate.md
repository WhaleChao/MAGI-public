# 2026-05-09 Commercial Readiness Live Gate

MAGI public now has a repeatable installability and release-safety gate:

```bash
python3 scripts/ops/commercial_readiness_live.py --strict-public --skip-db --json-out .runtime/commercial_readiness_live_public_strict_YYYYMMDD.json
```

The public gate verifies:

- doctor has no failures
- beginner installer dry-run includes the expected install steps
- public release audit is clean, with warnings treated as failures in strict mode
- process hygiene has no duplicate, stuck, orphan, zombie, or port-conflict issues
- 24-hour stability observer can produce a current snapshot

Use the private checkout without `--skip-db` to verify live DB backup readability and restore confirmation gating.

Latest public live result: 5 pass / 0 fail.
