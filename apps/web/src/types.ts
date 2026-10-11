export interface Run {
  id: string;
  project_id: string;
  application_id: string;
  environment: string;
  agent_name: string | null;
  agent_version_id: string | null;
  agent_version: string | null;
  workflow_name: string | null;
  trace_id: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  token_usage: Record<string, unknown>;
  tags: string[];
  metadata: Record<string, unknown>;
  input: unknown;
  output: unknown;
}

export interface Span {
  id: string;
  trace_id: string;
  parent_span_id: string | null;
  span_type: string;
  name: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  status: string;
  input: unknown;
  output: unknown;
  attributes: Record<string, unknown>;
  error: string | null;
}

export interface RunPage {
  runs: Run[];
  limit: number;
  offset: number;
  total: number;
}
export interface TraceDetail {
  trace: {
    trace_id: string;
    run_id: string;
    root_span_id: string;
    started_at: string;
    ended_at: string | null;
    duration_ms: number | null;
    status: string;
  };
  run: Run | null;
  spans: Span[];
}
