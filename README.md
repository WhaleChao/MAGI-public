# MAGI V3

MAGI is a local-first AI operations platform built around explicit tool contracts, durable checkpoints, privacy boundaries, and rollback-ready releases. It combines interactive HTTP services, background supervision, business workflows, document tooling, and verifiable health evidence.

Current public baseline: **v3-20260820-rc627**.

## Architecture

- **Gateway** — interactive requests, authentication, security headers, and tool APIs.
- **Control** — administration, release state, and health views.
- **Supervisor** — schedules, workers, singleton ownership, retries, and recovery.
- **Workflow tools** — files, Drive/NAS, documents, calendars, notifications, and domain adapters.

MAGI treats model output as one component in a larger evidence chain. Side effects require identity binding, idempotency, a receipt, read-back verification, and a safe failure path.

## Reliability principles

1. External waiting is `deferred`, never silently reported as completed.
2. File synchronization uses content and source identity, not path similarity.
3. Controlled schemas reject unknown fields and unsafe type coercion.
4. Installed releases are immutable and every candidate rebuilds its own evidence.
5. Runtime state, credentials, case data, and private integrations are excluded from public releases.

## rc627 public evidence summary

- Fresh sealed source bundle: 2,034 files.
- Full quality certification: exact/full passed; 2,446 tests collected.
- Privacy audit: passed with zero violations.
- LIVE health: business, function, Doctor, guardian, and Funnel passed.
- Cookie Cutter: three synthetic shapes passed printable/manifold/no-persist/no-external checks.

## Documentation

- [MAGI V3 maintenance encyclopedia (PDF, 203 pages)](docs/MAGI_V3_維修百科全書_rc627.pdf)
- [Maintainable encyclopedia source (Markdown)](docs/MAGI_V3_維修百科全書_rc627.md)
- [Machine-readable source index](docs/MAGI_V3_原始碼索引_rc627.json)
- [Public technical manual](docs/MAGI_V3_技術手冊_rc627_公版.md)
- [Traditional Chinese README](README.zh-TW.md)
- [Security policy](SECURITY.md)
- [Support](SUPPORT.md)

## Development

Install dependencies from `requirements.txt` or `requirements-selfhost.txt`, then run the relevant unit and contract tests under `tests/`. Before publishing a public branch, run:

```bash
python3 scripts/public_release_audit.py --public-isolation --strict
```

Configuration belongs in environment variables or placeholder examples. Never commit credentials, cookies, tokens, personal data, runtime databases, or production receipts.

## Repository boundary

`MAGI-public` contains the public architecture, tests, examples, and documentation. The private `MAGI-v3` repository preserves the complete engineering history, including the original V2 commit history, without publishing private runtime data.

## License

See [LICENSE](LICENSE).
