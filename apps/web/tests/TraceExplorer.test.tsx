import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TraceExplorer from "../src/TraceExplorer";

const run = {
  id: "run-1",
  project_id: "project-1",
  application_id: "application-1",
  environment: "test",
  agent_name: "ResearchAgent",
  agent_version_id: "version-1",
  agent_version: "v1",
  workflow_name: null,
  trace_id: "trace-1",
  status: "SUCCEEDED",
  started_at: "2026-01-01T00:00:00Z",
  completed_at: "2026-01-01T00:00:00.125Z",
  duration_ms: 125,
  token_usage: {},
  tags: [],
  metadata: {},
  input: null,
  output: null,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("TraceExplorer", () => {
  it("shows the required empty state from an empty live API response", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ runs: [], total: 0, limit: 25, offset: 0 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    render(<TraceExplorer />);
    expect(
      await screen.findByText("No runs have been ingested."),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/runs?offset=0&limit=25");
  });

  it("opens trace details and displays the span tree and recorded detail fields", async () => {
    const detail = {
      trace: {
        trace_id: "trace-1",
        run_id: "run-1",
        root_span_id: "root",
        started_at: run.started_at,
        ended_at: run.completed_at,
        duration_ms: 125,
        status: "SUCCEEDED",
      },
      run,
      spans: [
        {
          id: "root",
          trace_id: "trace-1",
          parent_span_id: null,
          span_type: "AGENT",
          name: "ResearchAgent",
          started_at: run.started_at,
          ended_at: run.completed_at,
          duration_ms: 125,
          status: "OK",
          input: null,
          output: null,
          attributes: { steps: 1 },
          error: null,
        },
        {
          id: "child",
          trace_id: "trace-1",
          parent_span_id: "root",
          span_type: "TOOL",
          name: "LocalSearch",
          started_at: "2026-01-01T00:00:00.020Z",
          ended_at: "2026-01-01T00:00:00.080Z",
          duration_ms: 60,
          status: "ERROR",
          input: { query: "topic" },
          output: null,
          attributes: {},
          error: "connection refused",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          url.startsWith("/runs?")
            ? jsonResponse({ runs: [run], total: 1, limit: 25, offset: 0 })
            : jsonResponse(detail),
        ),
      ),
    );
    render(<TraceExplorer />);
    fireEvent.click(await screen.findByText("run-1"));
    expect(await screen.findByText("Execution timeline")).toBeInTheDocument();
    expect(screen.getAllByText("LocalSearch")).toHaveLength(2);
    fireEvent.click(screen.getAllByText("LocalSearch")[0]);
    expect(await screen.findByLabelText("Span details")).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
    expect(screen.getByText("Started")).toBeInTheDocument();
    expect(screen.getByText(/topic/)).toBeInTheDocument();
  });

  it("applies run filters and uses real pagination totals", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(jsonResponse({ runs: [run], total: 31, limit: 25, offset: 0 })),
      );
    vi.stubGlobal("fetch", fetchMock);
    render(<TraceExplorer />);
    await screen.findByText("run-1");
    fireEvent.change(screen.getByLabelText("Environment"), {
      target: { value: "production" },
    });
    fireEvent.change(screen.getByLabelText("Agent"), {
      target: { value: "ResearchAgent" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith(
        "/runs?offset=0&limit=25&environment=production&agent_name=ResearchAgent",
      ),
    );
    expect(screen.getByText("Showing 1–25 of 31")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith(
        "/runs?offset=25&limit=25&environment=production&agent_name=ResearchAgent",
      ),
    );
  });
});
