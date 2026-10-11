# Deployment guide (Phase 10)

## Database: SQLite or PostgreSQL

SQLite remains suitable for local development and small single-process installations. For production, use PostgreSQL and a dedicated database/user. The SQLAlchemy URL uses psycopg 3:

```sh
TRACECORE_DATABASE_URL='postgresql+psycopg://tracecore:REDACTED@db.internal:5432/tracecore'
```

Use TLS to PostgreSQL where traffic crosses hosts. Keep credentials in your secret manager. Back up the database and verify restore procedures before production use. PostgreSQL backup encryption, scheduling, and restore automation are deployment responsibilities.

## Migrations

Take a backup, then run migrations as a release step before starting new API instances:

```sh
alembic upgrade head
```

Do not use `Base.metadata.create_all()` for production schema management. Migration `0009_operational_hardening` adds credential and rate-limit tables without rewriting or dropping trace data. Review migration SQL in a staging database before production deployment. To inspect the current revision use `alembic current`; to inspect pending changes use `alembic history`.

## Authentication and credential lifecycle

Set at least:

```sh
TRACECORE_RUNTIME_ENVIRONMENT=production
TRACECORE_AUTH_MODE=bearer
TRACECORE_API_TOKENS='{"<random-token>":{"subject":"bootstrap-admin","role":"ADMIN","organization_id":"org-1","project_ids":["project-1"],"environments":["staging","production"]}}'
```

Use this high-entropy environment token only as a bootstrap or break-glass credential. After migration, issue normal credentials through `POST /auth/tokens` using an ADMIN principal. The secret is returned once; only its SHA-256 digest is stored. Credential records support expiry and immediate revocation through `DELETE /auth/tokens/{id}`. Listing never returns token material. Create narrow application credentials and do not put ADMIN credentials in agent workloads. Keep the bootstrap token in a secret manager and rotate/revoke it there; production with missing bearer configuration fails closed.

## RBAC and organization mapping

Create and map projects to organization IDs before issuing credentials. Provide only the environments a principal needs. Review role permissions and the operator responsibilities in [security and privacy](security-privacy.md). Database credentials have an explicit lifecycle, but TraceCore does not provide SSO, interactive login, user directory synchronization, or automated identity-provider deprovisioning.

## Retention

Set `TRACECORE_RETENTION_DAYS_BY_ENVIRONMENT` to your approved schedule and schedule the CLI command after deployment migrations. Example daily systemd timer or Kubernetes CronJob command: `tracecore retention enforce --project-id PROJECT_ID`. Add `--environment production` to limit a run to one configured environment; without it the command enforces all configured environments. The command uses each configured age, deletes only older records, and writes an audit event even when there is nothing to delete. Deletions remove run-linked evaluation/experiment results and aggregate snapshots, clear experiment mappings, remove matching alert occurrences, and preserve definitions and audit entries. Coordinate backups, replicas, legal holds, and backup expiration separately; database deletion cannot scrub existing backups.

## Network and application boundary

- Expose the API and console only through a TLS-terminating reverse proxy or private service mesh.
- Restrict ingress to instrumented applications and authorized console users.
- Restrict egress, especially if model-based evaluation providers are configured.
- Never log bearer headers or request bodies at the proxy.
- The application uses shared fixed 60-second rate-limit buckets in the TraceCore database. It fails closed when the limiter store is unavailable. Consider a proxy limiter as an additional perimeter control, and size/monitor the rate-limit table with the primary database.
- Keep PostgreSQL and backups on private networks with dedicated credentials.
- Trace content is untrusted data. Do not render it as HTML or execute it.

## Operational monitoring

Monitor API health, error rates, request latency, database connections, migration failures, disk usage, backup freshness, and rate-limit responses at the deployment boundary. TraceCore's production metrics are domain telemetry and do not replace monitoring of TraceCore itself. Run `tracecore audit verify --organization-id ORG_ID` on a schedule. It reports the verified event count and current head hash; save both in an independently controlled system. Later checks can use `--expected-head-hash HASH --expected-count COUNT` and exit nonzero on chain corruption or checkpoint mismatch. If verification fails, preserve the database and logs, compare against the last valid external checkpoint and tested backup, then restore into a clean database under incident procedure; do not manually rewrite audit rows. Verification without a prior external checkpoint only validates the chain as currently stored: a database administrator able to rewrite all audit rows can recompute the chain. TraceCore does not itself export or enforce immutable checkpoints.

## Limits

TraceCore supports database-backed API credential expiry/revocation, shared database rate limiting, and a retention CLI intended for a system scheduler. Operators still provide SSO/identity lifecycle, scheduler operation, database and backup encryption, backup/replica expiration, legal holds, independent audit checkpoint storage, key/secret-manager rotation, TLS/network boundaries, and security review. TraceCore does not claim security certification.
