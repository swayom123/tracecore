# Governance policies

TraceCore governance evaluates deterministic rules against stored runs and spans. It is retrospective: a `BLOCK` result describes a decision for the observed run and does not stop an agent or deployment. Policy evaluation does not execute telemetry content or call an evaluator model.

## Policy contract

Create a policy with `POST /policies`:

```json
{
  "project_id": "project-123",
  "name": "production-models",
  "conditions": [
    {"field": "environment", "operator": "eq", "value": "production"},
    {"field": "model", "operator": "not_in", "value": ["approved-model"]}
  ],
  "effect": "BLOCK",
  "severity": "ERROR"
}
```

All conditions are combined with AND. Creating the same project/name creates the next immutable integer version. Supported fields are `environment`, `agent_name`, `agent_version`, `run_status`, `logging_mode`, `model`, `prompt_version`, `span_status`, and `tool_name`. Operators are `eq`, `neq`, `in`, `not_in`, `exists`, and `not_exists`; unknown fields/operators and malformed values are rejected. `exists` and `not_exists` take no value. `in` and `not_in` require a non-empty scalar array.

Effects are `ALLOW`, `WARN`, `BLOCK`, and `REQUIRE_REVIEW`. A matching `ALLOW` policy is recorded as an evaluation but does not create a violation. Matching other effects create an `OPEN` violation; `REQUIRE_REVIEW` additionally sets `review_required`. `NO_MATCH` and `UNAVAILABLE` never create violations.

## Evidence semantics

Evaluate selected policies with `POST /runs/{run_id}/policy-check` and inspect saved checks with `GET /runs/{run_id}/policy-checks` or `GET /policy-evaluations/{id}`. Each result stores the policy ID, version, full policy snapshot, and per-condition expected/observed values.

`MATCH`, `NO_MATCH`, and `UNAVAILABLE` are distinct. Run environment/status and the presence/absence of an agent version are available from run records. Model and prompt versions come from LLM span attributes (`model`, `model_name`, `llm.model`, `gen_ai.request.model`; `prompt_version`, `prompt.version`) or model usage metadata. Tool names and span statuses come from spans. Logging mode is available only when ingestion recorded `metadata.logging_mode`; the current server setting is not used as a substitute. If a field requires telemetry that is absent, the condition is `UNAVAILABLE`, and the whole policy evaluation is `UNAVAILABLE` unless another condition already fails.

For multi-valued fields, `eq`/`in` match if any recorded value is in the expected set; `neq`/`not_in` match if any recorded value is outside it. Use `not_in` for allowlists so one disallowed tool/model is enough to trigger the rule. `exists` means at least one value was recorded. Explicitly absent agent name/version is available with an empty value set, so `not_exists` can detect it.

No default production policy is silently applied. To check missing agent versions, create a production policy with `environment eq production` and `agent_version not_exists`. Model approval, restricted logging mode, and prompt version checks likewise require explicit policies and available telemetry/configured allowlists. A missing model/prompt/logging value is `UNAVAILABLE`, never an implicit pass.

## Violations and audit history

List project violations with `GET /violations?project_id=...`; filter by status and paginate with `limit`/`offset`. `PATCH /violations/{id}` records a state transition to `OPEN`, `ACKNOWLEDGED`, `RESOLVED`, or `WAIVED`; every transition needs an actor and reason. A waiver also requires a timezone-aware future `expires_at`. Actor, reason, waiver timestamp, expiration, and each transition are retained; waiver expiration is reported without automatically changing the violation state. Read append-oriented transitions at `GET /violations/{id}/history`.

Policy evaluations and violations retain their policy version snapshot and evidence. API/CLI do not provide deletion or in-place policy editing. The current violation status and waiver fields are a materialized current state; transition records retain the history of changes.

CLI examples:

```sh
tracecore policy create --file policy.json
tracecore policy list --project-id project-123
tracecore policy check run-123
tracecore policy show <evaluation-id>
tracecore policy violations --project-id project-123 --status OPEN
tracecore policy transition <violation-id> --status ACKNOWLEDGED --actor reviewer --reason "triaged"
tracecore policy history <violation-id>
```

## Security, privacy, and limitations

Policy conditions are validated and policy values are passed through the standard redaction service before persistence. Evidence contains only condition values and recorded model/prompt/tool identifiers, statuses, and environment metadata; prompt bodies and span input/output content are not read for policy evaluation. These records are protected by project scoping in policy creation, run evaluation, listing, and stored violation association. TraceCore Phase 1 does not yet implement authenticated principals or RBAC, so `actor` is an audit label supplied by the caller, not a verified identity. Deploy behind an authenticated trusted service until access control is implemented.

Policies are deterministic and limited to the listed telemetry fields. They are not deployment gates, do not prevent completed runs, do not infer missing values, do not assess policy meaning with a model, and do not validate allowlists against an external registry. A configured but unobserved field is unavailable, so policies depending on it require instrumentation to record it.
