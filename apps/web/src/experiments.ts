import { apiFetch } from "./http";

export interface Experiment {
  id: string;
  project_id: string;
  name: string;
  version: number;
  dataset_id: string;
  dataset_version: number;
  baseline_candidate_id: string;
  evaluator_snapshot: Array<{ id: string; name: string; version: number; type: string }>;
  candidates: Array<{ id: string; name: string; configuration: Record<string, unknown>; case_runs: Record<string, string> }>;
  criteria: Array<{ metric: string; direction: string; max_regression: number; severity: string }>;
  created_at: string;
}

export interface ExperimentRun {
  id: string;
  experiment_id: string;
  experiment_version: number;
  dataset_version: number;
  status: string;
  comparison: {
    baseline_candidate_id: string;
    winner: null;
    candidates: Array<{
      candidate_id: string;
      candidate_name: string;
      status: string;
      metrics: Record<string, unknown>;
      evaluator_breakdown: Record<string, Record<string, number>>;
      criteria: Record<string, { baseline: number | null; candidate: number | null; delta: number | null; status: string; reason?: string }>;
    }>;
  };
  results?: Array<{ id: string; candidate_id: string; case_id: string; run_id: string; evaluator_id: string; evaluator_version: number; status: string; reason: string; details: Record<string, unknown> }>;
  created_at: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch { /* retain the status message */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export async function fetchExperiments(): Promise<Experiment[]> {
  const body = await request<{ experiments: Experiment[] }>("/experiments?limit=100&offset=0");
  return body.experiments;
}

export function fetchExperimentRuns(id: string): Promise<{ runs: ExperimentRun[]; total: number }> {
  return request(`/experiments/${encodeURIComponent(id)}/runs`);
}

export function compareExperiment(id: string): Promise<ExperimentRun> {
  return request(`/experiments/${encodeURIComponent(id)}/compare`, { method: "POST" });
}

export function fetchExperimentRun(id: string): Promise<ExperimentRun> {
  return request(`/experiment-runs/${encodeURIComponent(id)}`);
}
