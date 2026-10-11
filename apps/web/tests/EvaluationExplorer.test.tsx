import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import EvaluationExplorer from "../src/EvaluationExplorer";

const evaluation = {
  id: "evaluation-1",
  dataset_id: "dataset-1",
  dataset_version: 2,
  evaluator_snapshot: [
    {
      id: "evaluator-1",
      name: "Exact answer",
      version: 3,
      type: "EXACT_MATCH",
      config: {},
    },
  ],
  status: "FAIL",
  total_results: 2,
  passed: 1,
  failed: 1,
  unavailable: 0,
  assessed: 0,
  created_at: "2026-01-01T00:00:00Z",
  completed_at: "2026-01-01T00:00:01Z",
};

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("EvaluationExplorer", () => {
  it("shows an empty state when there are no persisted evaluations", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response({ evaluations: [], total: 0 })),
    );
    render(<EvaluationExplorer />);
    expect(
      await screen.findByText("No evaluations have been run."),
    ).toBeInTheDocument();
  });

  it("loads persisted summaries and failed case reasons", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        url.startsWith("/evaluations?")
          ? response({ evaluations: [evaluation], total: 1 })
          : response({
              ...evaluation,
              results: [
                {
                  id: "result-1",
                  case_id: "case-1",
                  dataset_version: 2,
                  case: {
                    input: { prompt: "Question" },
                    expected_output: "Expected answer",
                    expected_output_present: true,
                    expected_properties: {},
                    metadata: {},
                  },
                  run_id: "run-1",
                  evaluator_id: "evaluator-1",
                  evaluator_version: 3,
                  status: "FAILED",
                  reason: "Output does not exactly match expected_output",
                  details: { expected_type: "dict", actual_type: "dict" },
                },
                {
                  id: "result-2",
                  case_id: "case-2",
                  dataset_version: 2,
                  run_id: null,
                  evaluator_id: "evaluator-1",
                  evaluator_version: 3,
                  status: "UNAVAILABLE",
                  reason: "No ingested run was assigned to this case",
                  details: {},
                },
              ],
            }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<EvaluationExplorer />);
    fireEvent.click(await screen.findByText("evaluation-1"));
    expect(await screen.findByText("Case results")).toBeInTheDocument();
    expect(
      screen.getByText("Output does not exactly match expected_output"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No ingested run was assigned to this case"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Exact answer v3")).toHaveLength(2);
    fireEvent.click(screen.getByText("Case data"));
    expect(screen.getByText(/Expected answer/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Status"), {
      target: { value: "FAILED" },
    });
    await waitFor(() =>
      expect(
        screen.queryByText("No ingested run was assigned to this case"),
      ).not.toBeInTheDocument(),
    );
  });

  it("displays model judge metadata separately from deterministic pass/fail", async () => {
    const assessedEvaluation = { ...evaluation, status: "ASSESSED", assessed: 1, total_results: 1 };
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(
      url.startsWith("/evaluations?")
        ? response({ evaluations: [assessedEvaluation], total: 1 })
        : response({
            ...assessedEvaluation,
            evaluator_snapshot: [{ id: "judge-1", name: "Relevance judge", version: 1, type: "MODEL_JUDGE", config: {} }],
            results: [{
              id: "result-judge", case_id: "case-judge", dataset_version: 2, run_id: "run-judge",
              evaluator_id: "judge-1", evaluator_version: 1, status: "ASSESSED",
              reason: "Model-assisted assessment recorded; score is not ground truth",
              details: { provider: "openai_compatible", model: "judge-small", prompt_version: "rubric-1",
                score: 0.8, confidence: 0.6, reason: "Relevant" },
            }],
          }),
    )));
    render(<EvaluationExplorer />);
    fireEvent.click(await screen.findByText("evaluation-1"));
    expect(await screen.findByText("Model assessment")).toBeInTheDocument();
    expect(screen.getAllByText(/judge-small/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Score 0.8 · confidence 0.6/)).toBeInTheDocument();
    expect(screen.getAllByText(/model assessed/).length).toBeGreaterThan(0);
  });
});
