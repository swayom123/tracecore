# Model-based evaluation

`MODEL_JUDGE` is an opt-in evaluator for relevance, completeness, groundedness, and instruction adherence. It runs only when a stored evaluator of that type is explicitly included in an evaluation request. Judge output is an assessment, not ground truth; model scores and confidence are reported separately from deterministic pass/fail results.

## Provider setup

TraceCore includes an OpenAI-compatible Chat Completions adapter using `httpx`. Configure the provider allowlist, endpoint, and key in the API environment:

```dotenv
TRACECORE_EVALUATION_ALLOWED_PROVIDERS=openai_compatible
TRACECORE_EVALUATION_OPENAI_BASE_URL=https://api.openai.com/v1
TRACECORE_EVALUATION_OPENAI_API_KEY=...
```

The allowed-provider list defaults to empty, which disables provider calls. The endpoint and key are server configuration and are never accepted from evaluator JSON or emitted in result snapshots. Use a provider endpoint approved for the data TraceCore may send. The endpoint is a trusted operator setting; protect it as configuration because it can designate a network destination.

Data marked `restricted_data: true` or with `data_classification` of `RESTRICTED`, `CONFIDENTIAL`, or `SENSITIVE` in case/run metadata is not sent unless the provider is separately listed in `TRACECORE_EVALUATION_RESTRICTED_DATA_PROVIDERS`. If the provider is disallowed, required content is missing/omitted, or credentials are absent, the result is `UNAVAILABLE` and no provider call is made. Provider failures and invalid judge responses are also `UNAVAILABLE`, with a safe error category in result details.

## Evaluator contract

Create through the existing `POST /evaluations/evaluators` endpoint:

```json
{
  "project_id": "support-project",
  "name": "answer-relevance",
  "evaluator_type": "MODEL_JUDGE",
  "config": {
    "provider": "openai_compatible",
    "model": "configured-model-name",
    "dimension": "RELEVANCE",
    "prompt_version": "team-rubric-v1",
    "timeout_seconds": 20,
    "max_input_chars": 12000
  }
}
```

Supported dimensions are `RELEVANCE`, `COMPLETENESS`, `GROUNDEDNESS`, and `INSTRUCTION_ADHERENCE`. `provider`, `model`, and `dimension` are required. `prompt_version` defaults to `tracecore-judge-v1`. Timeout must be 0.1–60 seconds and `max_input_chars` 256–40,000. Unknown configuration fields are rejected. User-supplied Python or prompt templates are not accepted.

The evaluation input is a JSON object containing the dataset case input, expected output/properties, reference context, and stored run output. Relevance and instruction adherence require input/output; completeness requires output and at least one reference (expected output, expected properties, or reference context); groundedness requires output/reference context. Missing or content-omitted fields make that case unavailable.

The fixed system instruction and the untrusted JSON data are sent as separate chat messages. The instruction explicitly tells the judge to treat all JSON values as data and not follow embedded instructions. The standard credential redactor runs immediately before provider submission. Trace content is not executed. The request uses temperature 0, caps generated output at 512 tokens, and applies the configured timeout and input-size cap.

## Results and reproducibility

Successful results have status `ASSESSED`, with a score and confidence in `[0, 1]`, a judge reason, and provider/model/dimension/prompt version. `EvaluationRun.assessed` counts these separately from deterministic `passed`, `failed`, and `unavailable`. A run summary with successful model assessments and no deterministic failures/unavailable checks is `ASSESSED`, not `PASS`. The console labels these results as model assessments and displays provider, model, prompt version, score, confidence, reason, and the details snapshot.

Per-result details include the evaluator ID/version and config, timeout/input limit, temperature/output-token/response-format settings, provider endpoint (without credentials), prompt text and hash, redacted submitted-data snapshot and hash, response score/confidence/reason, and provider error indicator when applicable. The result snapshot is stored for auditing and can contain application content after credential redaction. Treat evaluation result access as access to trace content.

Use the existing API workflow to create a `MODEL_JUDGE` evaluator and include its ID in `POST /evaluations`. The same workflow is available through `tracecore eval evaluator create --file evaluator.json` and `tracecore eval run --dataset-id ... --evaluator-id ... --case-run CASE_ID=RUN_ID`. Inspect the complete result with `GET /evaluations/{id}` or `tracecore eval show ID`.

## Limits and security

- The judge is probabilistic, can be biased or wrong, and its score/confidence are not calibrated truth. TraceCore does not select a winner or merge model and deterministic results into one metric.
- An allowlisted provider is an operator assertion that sending data there is acceptable; provider-specific contractual, residency, and retention requirements must be reviewed outside TraceCore.
- Credential redaction does not detect all personal or regulated data. Mark restricted runs/cases explicitly and leave restricted-provider authorization disabled unless approved.
- The stored submitted-data snapshot supports auditability but is sensitive content. Current deployment has no authentication/RBAC; do not expose it to untrusted networks.
- This phase does not implement human review, evaluator calibration, cross-provider policy, deployment gates, async job processing, or automatic retrying of provider requests.
- The OpenAI-compatible adapter expects Chat Completions JSON-object output. Other provider protocols need an explicit adapter; no provider call is made for unconfigured or disallowed providers.
