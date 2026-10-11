import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import MonitoringExplorer from "../src/MonitoringExplorer";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("MonitoringExplorer", () => {
  it("renders stored metrics and leaves unavailable cost explicit", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string) => input.startsWith("/monitoring/metrics")
      ? new Response(JSON.stringify({ project_id: "project-a", run_count: 2, metrics: {
        success_count: { value: 1, status: "AVAILABLE", sample_count: 2 },
        verified_cost: { value: null, status: "UNAVAILABLE", sample_count: 0 },
        latency_ms: { p50: null, p95: null, p99: null, sample_count: 2, minimum_samples: 20 },
      }, budgets: [] }), { status: 200 })
      : input.startsWith("/monitoring/alert-rules")
        ? new Response(JSON.stringify({ rules: [] }), { status: 200 })
        : new Response(JSON.stringify({ occurrences: [] }), { status: 200 })));
    render(<MonitoringExplorer />);
    fireEvent.change(screen.getByLabelText("Project ID"), { target: { value: "project-a" } });
    fireEvent.click(screen.getByText("Load monitoring"));
    expect(await screen.findByText("2")).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable", { selector: "strong" }).length).toBeGreaterThan(0);
    expect(screen.getByText(/minimum 20/)).toBeInTheDocument();
  });
});
