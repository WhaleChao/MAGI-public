# Support

MAGI public is suitable for source review, local evaluation, and self-hosted
pilot deployments. Formal customer support, uptime commitments, onboarding, and
custom integration work require a separate commercial agreement.

## Before Asking for Help

Run the basic diagnostics and include their summaries:

```bash
python3 scripts/magi_doctor.py --json
python3 scripts/install_magi.py --dry-run --check-live --json
python3 scripts/public_release_audit.py --strict
```

For commercial readiness:

```bash
python3 scripts/ops/commercial_readiness_live.py --strict-public
```

Use `--skip-db` only when checking a public installability package without a
private database.

## What to Include

- Operating system and CPU architecture.
- Python version.
- MAGI commit SHA.
- Which entrypoint failed: web UI, CLI, LINE, Discord, Telegram, LAF, file
  review, transcript, PDF, or scheduler.
- Redacted command output.
- Whether the failure is reproducible with synthetic data.

## What Not to Share

Do not paste:

- `.env` content.
- API keys, webhook tokens, DB passwords, portal credentials, or OAuth tokens.
- Real client names, phone numbers, case files, court portal screenshots, DB
  dumps, NAS paths, or runtime logs containing sensitive content.

## Commercial Support Scope

A production support package should define:

- Named operator and escalation contacts.
- Supported deployment topology and hardware.
- Backup and restore responsibility.
- Response targets for severity levels.
- Data processing roles and retention requirements.
- Third-party portal and messaging account ownership.
- Acceptance tests required before each production upgrade.
