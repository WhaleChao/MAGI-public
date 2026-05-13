# MAGI Public Self-Install Guide

This guide is for external operators installing the public MAGI package on their own computer. The public package is designed as a single-host product: every operator brings their own database, model runtime, storage path, OAuth credentials, and messaging channels. No private runtime files, OAuth tokens, database dumps, client files, or law-firm specific paths should be committed to git.

## Supported Target

- macOS on Apple Silicon is the primary target.
- Windows and Linux can run the Flask daemon with an Ollama-compatible backend, but the production live gates are strongest on macOS.
- One installation equals one MAGI host. Multi-tenant hosting, public client portals, and electronic signature workflows are not enabled in this public package.

## Install

```bash
git clone https://github.com/WhaleChao/MAGI-public.git
cd MAGI-public
python3 scripts/first_run_setup.py --write-env --public
python3 scripts/install_magi.py --dry-run --check-live
python3 scripts/install_magi.py --yes
source .venv/bin/activate
python3 scripts/magi_doctor.py --json
```

The installer dry run is intentionally conservative. It prints the steps it would run, then stops. Run `--yes` only after the plan matches the target machine.

## Required Local Configuration

After `first_run_setup.py --write-env --public`, edit `.env` locally and fill the required values:

- `FLASK_SECRET_KEY` and `MAGI_API_KEY`
- database host, user, password, and database name
- model backend settings
- NAS or local storage roots
- Google OAuth credentials if calendar import is enabled
- messaging channel tokens only if notifications are enabled

The setup and audit tools never print secret values. Keep `.env`, OAuth token files, database dumps, runtime reports, and case/client folders outside git.

## Storage Paths

Public MAGI no longer assumes one law-firm specific NAS user. Set these when the defaults do not match the target machine:

```bash
MAGI_NAS_HOME_USER=home
MAGI_CANONICAL_ACTIVE_SHARE_PREFIX=Z:/home
MAGI_CANONICAL_ACTIVE_CASE_PREFIX=Z:/home/01_案件
MAGI_CANONICAL_CLOSED_SHARE_PREFIX=Y:/archive
MAGI_CANONICAL_CLOSED_CASE_PREFIX=Y:/archive/03_工作資料/10_結案
```

For a local-only test install, point the case roots at a test folder before enabling any file-moving workflow.

## Public Release Gates

Before giving the installation to an operator, all commands below should pass:

```bash
python3 scripts/public_release_audit.py --public-isolation --strict
python3 scripts/first_run_setup.py --public --json
python3 scripts/magi_doctor.py --json
python3 scripts/install_magi.py --dry-run --check-live
python3 scripts/ops/commercial_readiness_live.py --strict-public --skip-db
```

`--skip-db` is only for installability checks without a private database. A real production deployment must verify DB backup, DB restore, NAS/file storage, model routing, channels, and calendar OAuth before handoff.

## Operator Safety Rules

- Keep production submission workflows confirmation-gated.
- Use dry-run mode before bulk file movement, DB migration, OCR batch processing, or calendar import.
- Keep legal-aid, court-file-review, transcript, accounting, and calendar jobs separated by channel and task type.
- Keep NERV or the MAGI status page open during onboarding to watch model, disk, DB, NAS, queue, and background-job health.
- Treat any resource-governor `core_only` or `critical` status as a stop condition.

## Troubleshooting

- If `public_release_audit.py` fails, remove tracked runtime files or private markers first; do not bypass the audit for public release.
- If `magi_doctor.py` warns about disk space, clear local caches or move large archives to NAS before enabling background workers.
- If model live checks fail, confirm the active backend and ports before starting the daemon.
- If Google Calendar import is enabled, confirm OAuth credentials and imported calendar IDs with a dry run before writing todos.
