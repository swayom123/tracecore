# Remaining production hardening plan

Repository review found that the platform features from the ten phases are present and the existing suite passes (53 tests under `.venv`). The remaining material production gaps documented in the deployment/security guides are:

1. Bearer credentials are held in a static settings map, so issuance, expiry, and revocation require secret-map replacement and restart.
2. Rate limits are process-local and reset when a process restarts.
3. Retention is configured by environment but requires an operator to invoke the endpoint; backups and replicas are outside database deletion.
4. Audit verification detects edits in the database chain, but the chain has no external checkpoint and cannot prove integrity against a database administrator who can rewrite all rows.

Implementation plan:

- Add hashed, database-backed API credentials with scoped principals, expiry, revocation, one-time secret display, audit entries, and an ADMIN management flow. Keep static configured tokens as an explicitly documented bootstrap/compatibility mechanism.
- Move rate-limit counters to shared database state so API processes use the same fixed-window counters. Fail closed on limiter storage errors in production.
- Add a retention CLI command suitable for a system scheduler, including dry-run/report support where possible, and document a concrete timer/CronJob pattern. Preserve the existing audited deletion semantics and avoid a built-in multi-worker scheduler that could race or duplicate work.
- Add an audit verification CLI command and periodic external checkpoint export/verification guidance; document that database-only hash chaining is tamper-evident only while prior checkpoints remain independently held.
- Add focused tests and update deployment/security docs. Run checks and migration verification available in the environment, then refresh graphify.

No trace, evaluation, cost, latency, or alert fixtures will be generated. No SSO or external immutable ledger will be claimed as implemented.
