import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../src/http";

afterEach(() => {
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe("apiFetch", () => {
  it("adds the session bearer token without using cookies", async () => {
    window.sessionStorage.setItem("tracecore_api_token", "local-test-token");
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await apiFetch("/runs");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer local-test-token");
    expect(init.credentials).toBeUndefined();
  });

  it("does not add an authorization header in local development", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await apiFetch("/runs");
    expect(fetchMock).toHaveBeenCalledWith("/runs");
  });
});
