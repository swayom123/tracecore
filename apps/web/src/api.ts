import type { RunPage, TraceDetail } from "./types";
import { apiFetch } from "./http";

export interface RunFilters {
  environment: string;
  application_id: string;
  agent_name: string;
  agent_version: string;
  status: string;
  started_after: string;
  started_before: string;
}

export async function fetchRuns(
  filters: RunFilters,
  offset: number,
  limit = 25,
): Promise<RunPage> {
  const params = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  for (const [key, value] of Object.entries(filters)) {
    if (value)
      params.set(
        key,
        key.startsWith("started_") ? new Date(value).toISOString() : value,
      );
  }
  const response = await apiFetch(`/runs?${params.toString()}`);
  if (!response.ok) throw new Error(await responseMessage(response));
  return response.json() as Promise<RunPage>;
}

export async function fetchTrace(traceId: string): Promise<TraceDetail> {
  const response = await apiFetch(`/traces/${encodeURIComponent(traceId)}`);
  if (!response.ok) throw new Error(await responseMessage(response));
  return response.json() as Promise<TraceDetail>;
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
