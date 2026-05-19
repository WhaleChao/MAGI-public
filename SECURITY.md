# Security Policy

MAGI is a local-first AI operations platform. Security expectations differ
between the public source package and a managed commercial deployment, so this
policy separates code security from operator responsibilities.

## Supported Versions

Security fixes are accepted for the current public branch and the latest tagged
release once tags are published. Older experimental branches are not supported
unless a separate maintenance agreement says otherwise.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately. Do not open a public issue
that contains tokens, customer records, legal case material, portal credentials,
private URLs, screenshots of sensitive pages, or exploit steps against a live
third-party service.

Preferred channels:

- GitHub Security Advisory for this repository, when available.
- A private maintainer contact channel designated by the deployment operator.

Include:

- A short impact summary.
- Affected commit, branch, or release.
- Reproduction steps using synthetic data.
- Whether the issue requires local access, authenticated access, or external
  network access.
- Any suggested mitigation.

## Response Targets

These are operational targets, not a warranty:

| Severity | Example | Target first response | Target fix or mitigation |
|----------|---------|-----------------------|--------------------------|
| Critical | Secret disclosure, unauthenticated data access, remote code execution | 1 business day | 3 business days |
| High | Auth bypass, privilege escalation, destructive action without confirmation | 2 business days | 7 business days |
| Medium | Cross-site scripting, CSRF, sensitive metadata exposure | 5 business days | 14 business days |
| Low | Hardening, logging, documentation gaps | 10 business days | Next planned release |

## Commercial Deployment Requirements

Before using MAGI in a paid or client-facing environment, operators must:

- Run `python3 scripts/public_release_audit.py --strict`.
- Run `python3 scripts/ops/commercial_readiness_live.py --strict-public`.
- Use `--skip-db` only for public installability checks that intentionally do
  not include a private production database.
- Keep `.env`, runtime folders, downloaded case material, portal credentials,
  DB backups, and channel tokens out of git.
- Set unique API keys, webhook secrets, session keys, and admin allowlists for
  each deployment.
- Keep destructive actions confirmation-gated, especially DB restore, portal
  submission, file moves, and bulk downloads.
- Verify third-party licenses in `docs/THIRD_PARTY_BOM.md`, especially PDF/OCR
  and messaging dependencies used in commercial service.

## Out of Scope

The following are generally outside this repository's security scope:

- Compromise of an operator's NAS, email, messaging account, or court/legal
  portal account unrelated to MAGI code.
- Issues caused by publishing `.env`, DB dumps, runtime logs, or private files.
- Abuse of third-party APIs when credentials are shared outside MAGI.
- Social engineering of an operator or customer.

## Safe Harbor

Good-faith research that avoids data exfiltration, persistence, service
disruption, and access to real case material is welcome. Stop testing and
report immediately if you encounter sensitive data.
