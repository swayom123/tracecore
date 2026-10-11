import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ExperimentExplorer from "../src/ExperimentExplorer";

const experiment = {
  id: "experiment-1", project_id: "project-1", name: "prompt-check", version: 2,
  dataset_id: "dataset-1", dataset_version: 1, baseline_candidate_id: "base",
  evaluator_snapshot: [], candidates: [
    { id: "base", name: "Prompt A", configuration: { prompt_version: "v1" }, case_runs: { c1: "r1" } },
    { id: "candidate", name: "Prompt B", configuration: { prompt_version: "v2" }, case_runs: { c1: "r2" } },
  ], criteria: [{ metric: "evaluation_pass_rate", direction: "HIGHER", max_regression: 0, severity: "FAIL" }],
  created_at: "2026-01-01T00:00:00Z",
};
const comparison = {
  id: "comparison-1", experiment_id: "experiment-1", experiment_version: 2, dataset_version: 1,
  status: "WARN", created_at: "2026-01-01T00:00:01Z",
  comparison: { baseline_candidate_id: "base", winner: null, candidates: [
    { candidate_id: "base", candidate_name: "Prompt A", status: "BASELINE", metrics: { evaluation_pass_rate: 1, failure_count: 0, latency_ms: 20, recorded_cost: null, tool_calls: 1, evaluator_breakdown: {} }, evaluator_breakdown: {}, criteria: {} },
    { candidate_id: "candidate", candidate_name: "Prompt B", status: "WARN", metrics: { evaluation_pass_rate: 1, failure_count: 0, latency_ms: 30, recorded_cost: null, tool_calls: 1, evaluator_breakdown: {} }, evaluator_breakdown: {}, criteria: { recorded_cost: { baseline: null, candidate: null, delta: null, status: "UNAVAILABLE", reason: "Metric recorded_cost unavailable" } } },
  ] },
};

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("ExperimentExplorer", () => {
  it("shows empty state when no experiment definitions exist", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ experiments: [], total: 0 })));
    render(<ExperimentExplorer />);
    expect(await screen.findByText("No experiments have been defined.")).toBeInTheDocument();
  });

  it("inspects candidate config, runs a comparison, and shows unavailable metrics and results", async () => {
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.startsWith("/experiments?") ) return response({ experiments: [experiment], total: 1 });
      if (input === "/experiments/experiment-1/runs") return response({ runs: [], total: 0 });
      if (input === "/experiments/experiment-1/compare" && init?.method === "POST") return response(comparison);
      if (input === "/experiment-runs/comparison-1") return response({ ...comparison, results: [
        { id: "result-1", candidate_id: "candidate", case_id: "c1", run_id: "r2", evaluator_id: "eval-1", evaluator_version: 1, status: "FAILED", reason: "Output differed", details: {} },
      ] });
      return response({ runs: [], total: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ExperimentExplorer />);
    fireEvent.click(await screen.findByText("prompt-check"));
    expect(await screen.findByText("Candidate configurations")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Run comparison"));
    expect(await screen.findByText("Per case and evaluator")).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable")).toHaveLength(2);
    expect(screen.getByText("Output differed")).toBeInTheDocument();
  });
});
