// Mirrors the JSON shapes emitted by `src/server.py`. Kept narrow on
// purpose: anything `unknown` here is something the backend explicitly does
// not promise to keep stable.

export type EventState = "queued" | "running" | "done" | "failed" | "skipped";

export type IssueState =
  | "new"
  | "reproducing"
  | "fixing"
  | "reviewing"
  | "opened"
  | "merged"
  | "closed"
  | "needs_info"
  | "abandoned";

export type ReleaseState = "awaiting_ci" | "fixing" | "green" | "failed" | "superseded";

export interface RuntimeInfo {
  bot_login: string;
  repo_allowlist: string[];
  repo_owners: string[];
  installation_ids: number[];
  max_concurrency: number;
  model: string;
  model_pool: string[];
  fallback_models: string[];
  thinking_level: string;
  trigger_mode: string;
  trigger_label: string;
  issue_index_sync_seconds: number;
  uptime_seconds: number;
}

export interface LatestEvent {
  delivery_id: string;
  event_type: string;
  state: EventState;
  attempts: number;
  received_at: string;
  last_error: string | null;
  actor_login?: string | null;
  actor_id?: number | null;
  authorized?: boolean | null;
  auth_reason?: string | null;
  trigger_kind?: string | null;
  run_id?: string | null;
  capabilities?: string[] | null;
}

export interface IssueRow {
  key: string;
  repo: string;
  number: number;
  branch: string | null;
  pr_number: number | null;
  state: IssueState;
  classification: string | null;
  updated_at: string;
  latest_event: LatestEvent | null;
}

export interface ReleaseRow {
  key: string;
  repo: string;
  tag: string;
  version: string;
  state: ReleaseState;
  current_sha: string;
  last_failed_sha: string | null;
  rounds: number;
  last_error: string | null;
  session_dir: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunningEvent {
  delivery_id: string;
  event_type: string;
  repo: string | null;
  issue_key: string | null;
  received_at: string;
  started_at: string | null;
  attempts: number;
  model: string | null;
  last_tool: string | null;
  last_tool_ts: string | null;
}

export interface RecentEvent {
  delivery_id: string;
  event_type: string;
  repo: string | null;
  issue_key: string | null;
  state: EventState;
  attempts: number;
  received_at: string;
  last_error: string | null;
  issue_state: IssueState | null;
  actor_login?: string | null;
  actor_id?: number | null;
  authorized?: boolean | null;
  auth_reason?: string | null;
  trigger_kind?: string | null;
  run_id?: string | null;
  capabilities?: string[] | null;
}

export interface StatusResponse {
  runtime: RuntimeInfo;
  event_counts: Record<EventState, number>;
  issue_event_counts: Record<EventState, number>;
  running_events: RunningEvent[];
  inflight: string[];
  issues: IssueRow[];
  releases: ReleaseRow[];
  recent_events: RecentEvent[];
  system: SystemInfo;
}

// ── System / telemetry (the "state of everything" surface) ────────────────

/** A queued event and when its next attempt fires (`available_at` = backoff). */
export interface PendingEvent {
  delivery_id: string;
  event_type: string;
  repo: string | null;
  issue_key: string | null;
  attempts: number;
  received_at: string;
  available_at: string | null;
}

/** A failed event that burned its whole retry budget — terminal, needs eyes. */
export interface DeadLetterEvent extends PendingEvent {
  finished_at: string | null;
  last_error: string | null;
  model: string | null;
  fallback_model: string | null;
}

/** One run's telemetry, as recorded by the worker when the run ended. */
export interface RunTelemetry {
  delivery_id: string;
  event_type: string;
  repo: string | null;
  issue_key: string | null;
  state: EventState;
  attempts: number;
  ended_at: string;
  model: string | null;
  fallback_model: string | null;
  duration_ms: number | null;
  cost_usd: number | null;
  cache_cost_usd: number | null;
  tokens_miss: number | null;
  tokens_out: number | null;
  tokens_cache_read: number | null;
  tokens_cache_write: number | null;
  last_error: string | null;
}

/** Aggregated spend/tokens over one window. */
export interface SpendBucket {
  runs: number;
  cost_usd: number;
  cache_cost_usd: number;
  miss_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  fallback_runs: number;
}

export interface IndexStatusRow {
  repo: string;
  rows: number;
  pull_requests: number;
  newest_issue_at: string;
  last_synced: string;
}

export interface SystemInfo {
  queue: {
    pending: PendingEvent[];
    dead_letters: DeadLetterEvent[];
    retry_budget: number;
  };
  index: IndexStatusRow[];
  runs: RunTelemetry[];
  spend: {
    today: SpendBucket;
    week: SpendBucket;
    all_time: SpendBucket;
  };
}

// Log entries carry arbitrary structured extras. We expose the known fields
// with concrete types and leave unknown extras as `unknown` so callers must
// narrow before using.
export interface LogEntry {
  ts?: string;
  level?: string;
  logger?: string;
  msg?: string;
  exc?: string;
  [key: string]: unknown;
}

export interface LogsResponse {
  entries: LogEntry[];
  count: number;
  limit: number;
}

export interface BrowseIssue {
  repo: string;
  number: number;
  title: string;
  state: "open" | "closed";
  author: string;
  labels: string[];
  comments: number;
  updated_at: string;
  created_at: string;
  html_url: string;
  processed: boolean;
}

export interface BrowseError {
  repo: string;
  error: string;
}

export interface BrowseCacheMeta {
  hit: boolean;
  fetched_at: number;
}

export interface BrowseResponse {
  issues: BrowseIssue[];
  errors: BrowseError[];
  repos: string[];
  cache: BrowseCacheMeta;
}

export interface TriggerResponse {
  delivery: string;
  state: string;
  mode?: string;
}

export interface CancelResponse {
  delivery: string;
  fired: boolean;
  previous_state: string;
}

export const TERMINAL_ISSUE_STATES: ReadonlySet<string> = new Set([
  "merged",
  "closed",
  "abandoned",
]);

export const LEVEL_ORDER: Readonly<Record<string, number>> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  RAW: 20,
};

export const EVENT_STATE_ORDER: readonly EventState[] = [
  "queued",
  "running",
  "done",
  "failed",
  "skipped",
];
