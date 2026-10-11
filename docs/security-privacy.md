# Security and privacy (Phase 10)

TraceCore treats prompts, tool arguments, model outputs, evaluator data, and span attributes as untrusted telemetry. It validates them as data and never executes them. Production must be placed behind a trusted network boundary and TLS termination; this implementation does not claim a security certification.

## Authentication and principals

`TRACECORE_RUNTIME_ENVIRONMENT=production` requires `TRACECORE_AUTH_MODE=bearer`. An unset/invalid token configuration fails closed with HTTP 503; missing or invalid bearer tokens receive HTTP 401. `development` auth mode creates a local ADMIN principal and is intended only for loopback development. Never use development auth in a deployed environment.

`TRACECORE_API_TOKENS` is a JSON object keyed by high-entropy bearer tokens (minimum 32 characters). Keep it for bootstrap/break-glass access. Each token maps to a `subject`, role, `organization_id`, non-empty `project_ids`, and non-empty `environments` list:

```json
{
  "<at-least-32-character-random-token>": {
    "subject": "release-operator",
    "role": "PLATFORM_ENGINEER",
    "organization_id": "org-example",
    "project_ids": ["project-example"],
    "environments": ["staging", "production"]
  }
}
```

Use an ADMIN bootstrap principal to create scoped database credentials with `POST /auth/tokens`. TraceCore generates a high-entropy token and displays it once; the database stores only its SHA-256 digest. `expires_at` is optional but must be timezone-aware. `GET /auth/tokens` omits token material; `DELETE /auth/tokens/{id}` revokes immediately and is idempotent. Credential creation/revocation is recorded in the audit chain. ADMIN can issue credentials only within its own organization, project, and environment scopes. Continue to rotate the bootstrap token through the secret manager. SSO, interactive login, user directory lifecycle, and automated bootstrap-secret rotation are not implemented.

## RBAC and tenant/environment isolation

Roles are `ADMIN`, `PLATFORM_ENGINEER`, `AI_ENGINEER`, `EVALUATOR`, `AUDITOR`, and `VIEWER`. Role checks run before the API route. Admin has all route permissions; platform engineers manage monitoring and policies; AI engineers instrument and inspect agent/evaluation data; evaluators can create/read evaluations; auditors can inspect audit and governance history; viewers can read run summaries and trace content. Mutating endpoints are restricted by operation and role.

Authenticated SQLAlchemy sessions add organization/project criteria to project-owned records and environment criteria to runs and run-derived trace content. Evaluations, experiments, analytics, and governance records that reference a run are additionally restricted to that run's allowed environment. Writes validate the principal's project and environment scope. Ingestion assigns the authenticated organization to newly created projects and rejects existing projects outside that organization. Projects created before organization ownership was introduced must be mapped to an organization before bearer principals can access them.

These scopes are enforced in the API's SQLAlchemy session layer. Direct database access, arbitrary scripts using unscoped sessions, and other applications sharing the database remain the operator's responsibility. Use separate databases or strict credentials for separate trust boundaries.

## Request controls

- `TRACECORE_MAX_PAYLOAD_BYTES` caps request bodies on write methods (default 1 MiB, maximum 16 MiB).
- `TRACECORE_RATE_LIMIT_PER_MINUTE` applies a shared fixed 60-second per-principal/token-key cap (default 600), stored in the application database. All API instances using that database share counters. The fixed window can permit a burst across a minute boundary; use an upstream limiter for stricter edge shaping. If the shared store fails, requests fail closed with 429.
- Validation errors omit input values; generic internal errors do not return exception details or submitted payloads.
- Structured request logs include route, request ID, status, and duration, not bodies or authorization headers.


## Redaction and logging modes

`TRACECORE_LOGGING_MODE` is explicit and defaults to `REDACTED`:

- `METADATA_ONLY`: input/output content is omitted and represented by content metadata.
- `REDACTED`: recognized credential fields and inline patterns are masked before persistence/provider submission.
- `FULL_CONTENT`: retains ordinary content after credential redaction and should only be enabled for approved data classes and access scopes.

The default redactor handles common password/secret/token assignments, API-key-shaped values, JWT-shaped strings, selected cloud access key IDs, and common GitHub/OpenAI-style token prefixes. Sensitive field names are redacted recursively. `TRACECORE_REDACTION_PATTERNS` accepts a JSON array of up to 100 configured regular expressions, each bounded to 512 characters; Python SDK clients may pass the equivalent list as `redaction_patterns` for masking before transmission. Redaction is heuristic and does not guarantee detection of all credentials or personal data; regexes are trusted operator configuration and should be reviewed for pathological runtime behavior. Email/phone/organization-specific PII detection is not built in.

## Sensitive content access and audit history

Successful `GET /runs`, `GET /runs/{id}`, and `GET /traces/{trace_id}` requests append audit events with actor, target, route, time, and status. Deletion and credential actions also append audit events. Audit event values form an organization-scoped SHA-256 hash chain; `GET /audit/verify` and `tracecore audit verify --organization-id ORG` verify it and return the current `head_hash`. Record that hash independently in an append-only system and compare future checks to detect rewritten/deleted tails. PostgreSQL writes use a transaction advisory lock per organization to serialize chain extension. Without an independently retained checkpoint, a database administrator can rewrite the complete chain and recompute hashes. TraceCore does not export or enforce external checkpoints.

Audit metadata avoids storing prompt/output payloads and each event records its applicable environment. Event reads are filtered by the principal's project and environment scope. Chain verification requires an auditor/admin principal scoped to every project and environment represented in that organization's chain. The audit chain is not a cryptographic signature or external immutable ledger. Protect database write access and backups separately.

## Retention and deletion

`TRACECORE_RETENTION_DAYS_BY_ENVIRONMENT` maps environment names to positive day counts (example defaults are development 30, staging 60, production 90). `tracecore retention enforce --project-id ID` applies all configured environment ages and is intended for a systemd timer, Kubernetes CronJob, or trusted external scheduler; repeated `--environment` values restrict it. The ADMIN endpoint `POST /projects/{project_id}/retention/enforce?environment=...` is also available. Explicit deletion uses `DELETE /projects/{project_id}/runs?environment=...&before=<timezone-aware timestamp>`. Operations are scoped, remove run-linked records/mappings, append audit history, and are idempotent. Dataset/evaluator/experiment definitions and audit history are preserved.

Deletion is scoped at run/project/environment level and applies to the relational database. Clearing experiment mappings changes those definition records after a deletion; comparison snapshots that reference deleted runs are removed. Project-wide alert occurrences without an environment scope are also removed because they may aggregate the deleted run. Backups and replicas require separate lifecycle controls; deletion does not rewrite old backups. TraceCore does not implement legal holds, per-record retention overrides, or automatic backup scrubbing. The scheduler is an operator-managed deployment prerequisite.

## Browser security

The web console sends bearer tokens in the `Authorization` header. Its token field stores the token only in tab-scoped `sessionStorage`; it does not use browser cookies, so CSRF protection and cookie flags are not applicable. Serve the console and API over HTTPS, restrict origins/network access, and apply standard content security and reverse-proxy controls. A same-origin production deployment is recommended.

## Model-assisted evaluation

See [Model-based evaluation](model-based-evaluation.md). Provider calls remain opt-in and use provider allowlists and redaction. The same trace-data access controls apply to evaluator results.
