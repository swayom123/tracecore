import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import GovernanceExplorer from "../src/GovernanceExplorer";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("GovernanceExplorer", () => {
  it("creates a policy, checks an ingested run, and shows persisted evidence", async () => {
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.startsWith("/policies?") || input.startsWith("/violations?")) {
        return new Response(JSON.stringify(input.startsWith("/policies?")
          ? { policies: [] } : { violations: [] }), { status: 200 });
      }
      if (input === "/policies" && init?.method === "POST") {
        return new Response(JSON.stringify({ id: "policy-1", version: 1 }), { status: 201 });
      }
      if (input === "/runs/run-1/policy-check") {
        return new Response(JSON.stringify({ evaluations: [{ id: "eval-1", policy_id: "policy-1", policy_version: 1,
          result: "MATCH", decision: "WARN", policy_snapshot: { name: "production" },
          evidence: [{ field: "environment", status: "MATCH", observed: ["production"] }], violation: null }] }), { status: 201 });
      }
      return new Response("{}", { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<GovernanceExplorer />);
    fireEvent.change(screen.getByLabelText("Project ID"), { target: { value: "project-1" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "production" } });
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "production" } });
    fireEvent.click(screen.getByText("Create version"));
    expect(await screen.findByText("No policies in this project.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Run ID"), { target: { value: "run-1" } });
    fireEvent.click(screen.getByText("Evaluate project policies"));
    expect(await screen.findByText("MATCH · WARN")).toBeInTheDocument();
    expect(screen.getByText(/"field": "environment"/)).toBeInTheDocument();
  });
});
