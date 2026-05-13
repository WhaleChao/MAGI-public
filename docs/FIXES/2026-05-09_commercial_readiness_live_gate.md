# 2026-05-09 Commercial Readiness Live Gate

MAGI now has a repeatable live gate for final release checks:

```bash
./venv/bin/python scripts/ops/commercial_readiness_live.py --strict-public --json-out .runtime/commercial_readiness_live_strict_YYYYMMDD.json
```

For public/installability-only checkouts without a private DB:

```bash
python3 scripts/ops/commercial_readiness_live.py --strict-public --skip-db --json-out .runtime/commercial_readiness_live_public_strict_YYYYMMDD.json
```

The gate verifies:

- doctor has no failures
- beginner installer dry-run includes the expected install steps
- public release audit is clean, with warnings treated as failures in strict mode
- process hygiene has no duplicate, stuck, orphan, zombie, or port-conflict issues
- private checkout only: local DB backup is readable and restore remains confirmation-gated
- 24-hour stability observer can produce a current snapshot

Latest private live result: 6 pass / 0 fail, including DB backup verification.
Latest public live result: 5 pass / 0 fail with `--skip-db`.
