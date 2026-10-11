import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AgentAnalyticsExplorer from "../src/AgentAnalyticsExplorer";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("AgentAnalyticsExplorer", () => {
  it("shows real aggregate data and run-level derived evidence", async () => {
    const fetchMock = vi.fn(async (input: string) => {
      if (input.startsWith("/agent-analytics?")) return new Response(JSON.stringify({
        project_id: "project-a", run_count: 1, groups: [{
          application_id: "app-a", agent_name: "Research", agent_version: "v2", run_count: 1,
          tool_usage_status: "AVAILABLE", tool_usage: [{ tool_name: "Search", calls: 2, succeeded: 1, failed: 1, outcome_unavailable: 0 }],
          retry_status: "UNAVAILABLE", retry_count: null, handoff_status: "UNAVAILABLE", handoff_count: null,
        }],
      }), { status: 200 });
      return new Response(JSON.stringify({ id: "analysis-1", analysis: {
        run_id: "run-a", trace_id: "trace-a", tool_usage_status: "AVAILABLE",
        tool_usage: [{ tool_name: "Search", calls: 2, succeeded: 1, failed: 1, outcome_unavailable: 0 }],
        retry_analysis: { status: "UNAVAILABLE", count: null, evidence: [] },
        handoffs: { status: "AVAILABLE", relationships: [{ from_agent: "Research", to_agent: "Verify" }] },
        loop_detection: { status: "AVAILABLE", step_count: 2, step_count_status: "AVAILABLE", events: [{ pattern: "REPEATED_TOOL_CALL" }] },
      } }), { status: 201 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AgentAnalyticsExplorer />);
    fireEvent.change(screen.getByLabelText("Project ID"), { target: { value: "project-a" } });
    fireEvent.click(screen.getByText("Query usage"));
    expect(await screen.findByText("Search: 2 (1 ok, 1 failed, 0 unknown)")).toBeInTheDocument();
    expect(screen.getAllByText("UNAVAILABLE")).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Run ID"), { target: { value: "run-a" } });
    fireEvent.click(screen.getByText("Analyze run"));
    expect(await screen.findByText(/Analysis analysis-1/)).toBeInTheDocument();
    expect(screen.getByText(/REPEATED_TOOL_CALL/)).toBeInTheDocument();
  });
});
