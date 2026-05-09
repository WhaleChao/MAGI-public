# MAGI Commercial Readiness Guide

This guide turns MAGI from a public source package into a service that can be
operated for external users. It is an operational checklist, not legal advice.
Review customer-facing terms, privacy commitments, and professional-service
claims with counsel before selling a hosted or managed service.

## Release Position

Current status: **commercial candidate**.

MAGI has a repeatable live gate and strict public audit. A deployment is not
commercially ready merely because the repository builds; the operator must also
verify the actual machine, model services, DB, NAS/file storage, channel
integrations, and any portal automation used in production.

## Required Gates Before Each Commercial Release

Public source package:

```bash
python3 scripts/public_release_audit.py --strict
python3 scripts/ops/commercial_readiness_live.py --strict-public --skip-db
```

Private production checkout:

```bash
./venv/bin/python scripts/public_release_audit.py --strict
./venv/bin/python scripts/ops/commercial_readiness_live.py --strict-public
./venv/bin/python scripts/ops/smoke_three_channels.py --strict-warn
./venv/bin/python scripts/ops/smoke_core_routes.py --with-network --with-heavy
./venv/bin/python scripts/ops/skill_realworld_smoke.py
./venv/bin/python scripts/ops/smoke_test_full.py
```

Acceptance target:

- No public audit errors or warnings in strict mode.
- No failing live-gate checks.
- No warning in strict channel smoke.
- Backup drill verifies a readable local backup and keeps restore
  confirmation-gated.
- Portal automation uses draft-only or explicit-confirmation mode unless the
  operator intentionally runs a submission workflow.
- Runtime-generated artifacts stay out of git.

## Customer-Facing Claims Allowed

You may describe MAGI as:

- A local-first AI operations platform.
- A self-hostable automation framework with legal-workflow modules.
- A system with live diagnostics, install dry-run, public secret audit, process
  hygiene checks, and backup confirmation gates.
- A platform that can integrate with messaging channels, PDF/document tooling,
  calendar data, and operator-owned portals when configured.

Avoid saying that MAGI:

- Replaces a lawyer, professional judgment, or human review.
- Guarantees legal correctness, filing success, portal availability, or model
  accuracy.
- Provides regulated legal advice to end users by itself.
- Can submit third-party portal workflows without operator responsibility.
- Has an uptime or support SLA unless a signed commercial agreement defines it.

## Operator Responsibilities

For each commercial deployment, the operator must define:

- Legal entity, service owner, and support contact.
- Data controller/processor role for each customer.
- Where data is stored and backed up.
- Retention schedule and deletion procedure.
- Incident response contact and reporting timeline.
- Third-party account ownership for LINE, Discord, Telegram, Google, court
  portals, LAF, NAS, and model providers.
- Whether cloud fallback models are allowed.
- Whether customer data can be used for model evaluation or training.
- Human approval points for submissions, DB restore, bulk file moves, and
  destructive jobs.

## Production Configuration Baseline

Set unique values per deployment:

- `MAGI_API_KEY`
- `FLASK_SECRET_KEY`
- Channel bot tokens and webhook secrets
- Admin allowlists
- DB credentials
- Backup destination and retention
- CORS origins
- Model service host/port
- Feature flags for cloud fallback and destructive automation

Keep private:

- `.env`
- OAuth token files
- DB dumps and backups
- Runtime reports
- Downloaded case/client material
- Portal screenshots and HTML snapshots
- NAS paths and mount credentials

## Commercial Documentation Pack

Before launch, publish or provide:

- `README.md` and `README.zh-TW.md`
- `SECURITY.md`
- `SUPPORT.md`
- `docs/PRIVACY_POLICY.md`
- `docs/TERMS_OF_SERVICE.md`
- `docs/DATA_RETENTION_POLICY.md`
- `docs/THIRD_PARTY_BOM.md`
- `docs/OPERATOR_RUNBOOK.md`
- Latest live-gate summary generated from the production machine

## Go / No-Go Checklist

- [ ] Public repo audit passes strict mode.
- [ ] Production checkout audit passes strict mode.
- [ ] CI is green on the release branch.
- [ ] Commercial readiness live gate passes on the target machine.
- [ ] Channel smoke passes with no strict warnings.
- [ ] Core route smoke passes, including tool-confusion guards.
- [ ] Skill matrix passes.
- [ ] DB backup exists, is readable, and restore requires confirmation.
- [ ] NAS/file storage is mounted at the expected path.
- [ ] Portal automation is draft-only or explicitly confirmed.
- [ ] Terms, privacy policy, support path, and security disclosure path are
      published.
- [ ] Third-party license and API terms have been reviewed for paid use.
- [ ] A rollback plan and operator contact are documented.

## Residual Risks to Track

- Third-party portals and messaging APIs can change without notice.
- Local model quality can vary by prompt, model version, and hardware pressure.
- OCR/PDF layout handling requires live regression tests for important
  document formats.
- Commercial use of some dependencies may require separate licenses.
- Self-hosted customers can misconfigure secrets, storage, backups, or admin
  access outside repository control.
