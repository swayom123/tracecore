import { apiFetch } from "./http";

export interface EvaluatorSnapshot {
  id: string;
  name: string;
  version: number;
  type: string;
  config: Record<string, unknown>;
}

export interface EvaluationSummary {
  id: string;
  dataset_id: string;
  dataset_version: number;
  evaluator_snapshot: EvaluatorSnapshot[];
  status: string;
  total_results: number;
  passed: number;
  failed: number;
  unavailable: number;
  assessed: number;
  created_at: string;
  completed_at: string | null;
}

export interface EvaluationResult {
  id: string;
  case_id: string;
  dataset_version: number;
  case: {
    input: unknown;
    expected_output: unknown;
    expected_output_present: boolean;
    expected_properties: Record<string, unknown>;
    reference_context: unknown;
    metadata: Record<string, unknown>;
  } | null;
  run_id: string | null;
  evaluator_id: string;
  evaluator_version: number;
  status: "PASSED" | "FAILED" | "UNAVAILABLE" | "ASSESSED";
  reason: string;
  details: Record<string, unknown>;
}

export interface EvaluationDetail extends EvaluationSummary {
  results: EvaluationResult[];
}

export async function fetchEvaluations(): Promise<{
  evaluations: EvaluationSummary[];
  total: number;
}> {
  const response = await apiFetch("/evaluations?limit=100&offset=0");
  if (!response.ok) throw new Error(await responseMessage(response));
  return response.json() as Promise<{
    evaluations: EvaluationSummary[];
    total: number;
  }>;
}

export async function fetchEvaluation(id: string): Promise<EvaluationDetail> {
  const response = await apiFetch(`/evaluations/${encodeURIComponent(id)}`);
  if (!response.ok) throw new Error(await responseMessage(response));
  return response.json() as Promise<EvaluationDetail>;
}

async function responseMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    return typeof body.detail === "string"
      ? body.detail
      : `Request failed (${response.status})`;
  } catch {
    return `Request failed (${response.status})`;
  }
}
