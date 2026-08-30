# MAGI RC643/R75 maintenance record

Date: 2026-08-30  
Public version: RC643/R75  
Production source commit: `ab398a44c99b9e83e6ba5c989312df29cf914006`

This is the de-identified public projection of the private maintenance
encyclopedia. It contains no case data, credentials, browser profiles,
production paths, private connector details, or runtime state.

## Fixed root causes

### Structured cron success was overridden by caught stderr

Some long-running legal workflow jobs intentionally retain per-item,
retryable diagnostics in stderr while returning exit code 0 and a final
structured receipt with `success=true`. The old policy could treat any
traceback-looking stderr as a whole-job failure.

The result policy now validates process exit status and the final complete
receipt first. Exit code 0 plus `success=true` is a successful schedule
terminal while partial or retry-pending business state remains visible.
Nonzero exits, unhandled tracebacks, missing receipts, and `success=false`
remain fail-closed.

### Production deployment and evidence validation used different roots

Production LaunchAgents were correctly bound to the canonical installed
release, while the evidence compiler still expected a candidate staging
root. The compiler and final gate now validate the canonical installed
release, release marker, manifest, production deployment mode, and immutable
candidate-equivalent identity. Historical evidence remains queryable but
cannot make the active release red.

## Promotion and LIVE verification

- Final release gate: 14/14 required evidence items passed; no missing,
  failed, or invalid evidence.
- Formal campaign: seven certifying workloads passed once; no duplicate full
  campaign was run.
- Atomic rotation completed without rollback; immutable predecessor and r59
  recovery points were retained.
- Production web login, authenticated workflow redirect, internal readiness,
  model topology, and Funnel edge HTTP/TLS probes passed.
- Ninety-six enabled schedules had zero failed terminal states after the
  active-release health rebuild. Normal queued/running work was not relabeled
  as failure.
- Legal business coverage passed: legal-aid attachment and mapping gaps were
  zero; the file-review portal's six downloadable signatures were all
  accounted in one snapshot, including one verified safe identity-mismatch
  deferral; transcript failures and notification backlog were zero.
- Doctor reported 56 pass, 0 warning, and 0 failure after an unused test-only
  temporary mirror was verified idle and removed. No release, rollback,
  evidence, case, or NAS data was deleted.
- Self-repair reported zero open, error, warning, or human-required issues.

The Funnel probe above is host-to-edge evidence. It must not be presented as
an independent off-host canary; a future green off-host claim still requires
external DNS, TLS, HTTP, and login-redirect evidence.

## Permanent regression coverage

- Final structured receipt versus caught per-item stderr.
- Nonzero exit, missing receipt, unhandled traceback, and false receipt.
- Canonical installed release versus candidate staging root.
- Installed manifest/marker tampering and wrong deployment mode.
- Historical release evidence excluded from active health.
- Maintenance manual routes remain authenticated and serve exact fixed
  assets.

Public R75 remains the release label. Private immutable promotion suffixes are
operational audit identities, not new public versions.
