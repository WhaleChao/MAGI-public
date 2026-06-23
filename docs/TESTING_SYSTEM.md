# MAGI Testing System

MAGI has many small tests, smoke scripts, portal probes, and release audits.
The source of truth for which checks belong to each gate is:

```bash
config/test_matrix.json
```

As of 2026-06-23 the matrix has four canonical suites plus one legacy alias:
`ci`, `smoke62`, `production-live`, `commercial-release`, and `smoke50`.
Treat direct pytest commands as focused diagnostics; they are not release
acceptance by themselves.

Run suites through:

```bash
./venv/bin/python scripts/ops/run_test_suite.py --list
./venv/bin/python scripts/ops/run_test_suite.py --suite ci
./venv/bin/python scripts/ops/run_test_suite.py --suite smoke62
./venv/bin/python scripts/ops/run_test_suite.py --suite production-live --json-out .runtime/production_live_latest.json
./venv/bin/python scripts/ops/run_test_suite.py --suite commercial-release --json-out .runtime/commercial_release_latest.json
```

## What Full Smoke Means

`smoke62` proves that the production checkout has the main runtime organs
online: Python, venv, config, DB, local services, inference, skills, channels,
notifications, LAF/file-review modules, cron, security, release hygiene, model
sidecars, NAS mount, judicial API pipeline, token health, share gateway, and
admin server. Public/commercial release guards are intentionally opt-in so a
private production checkout is not marked broken merely because it contains
private integrations.

It is not a complete proof that every workflow path has been exercised. It is a
fast live gate that should run often.

## Required Gates

`ci`
: Public-safe fast checks for every push. This must not require private
credentials, NAS mounts, or live portals.

`smoke62`
: Local full smoke for the private production machine. Run after code changes and after restarts.

`smoke50`
: Backward-compatible alias for the same full smoke gate. Do not cite it in new
release notes unless you are preserving old automation output.

`production-live`
: Real production-machine live validation. It runs doctor, judicial pipeline,
self-repair dry-run, smoke62, business modules, and commercial readiness.

`commercial-release`
: Release gate before sharing a build or selling service. It adds strict public
audit, cleanroom install checks, channel smoke, heavy route checks, and skill
real-world smoke. `scripts/ops/smoke_test_full.py --commercial` runs the same
public/commercial guard family from the smoke entry point.

Cleanroom installability validates the current worktree candidate, not merely
the last committed `HEAD`; this prevents false failures or false passes when a
release branch still has staged or uncommitted public-safe fixes.

## Acceptance Rule

A MAGI build can be called "live verified" only when:

- `ci` passes on GitHub.
- `smoke62` passes on the target machine.
- `production-live` passes on the target machine.
- For public/commercial releases, `commercial-release` also passes.
- The JSON output is saved in `.runtime/` or attached to the release note.
- Public release notes use a sanitized summary, not raw portal or business
  module stdout.

## Adding Coverage

Add a check to `config/test_matrix.json` when a workflow matters operationally.
Prefer commands that already exist as scripts or pytest files. Use environment
guards for checks that cannot run in public CI.

Each check should answer one concrete question, for example:

- Can the model endpoint answer?
- Can the tool route avoid confusing weather with calendar?
- Can the portal automation log in without submitting destructive actions?
- Can the PDF/OCR pipeline name a real sample correctly?
- Can a DB backup be created without auto-restoring?

## Current Known Boundary

Some workflows are intentionally not run by `smoke62` because they are slow,
destructive, or need human approval: real portal submissions, DB restore,
bulk NAS moves, calendar writes, and customer-facing message sends. Those
belong in `production-live` or a dedicated supervised suite with explicit
confirmation gates.
