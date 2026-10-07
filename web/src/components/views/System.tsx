import { For, type JSX, Show } from "solid-js";

import {
  fmtAge,
  fmtCost,
  fmtDuration,
  fmtTokens,
  issueUrl,
  shortDelivery,
  shortText,
  splitIssueKey,
} from "../../format";
import { statusResource } from "../../state";
import type {
  DeadLetterEvent,
  PendingEvent,
  RunTelemetry,
  SpendBucket,
  SystemInfo,
} from "../../types";
import { GlassCard } from "../GlassCard";
import { Pill } from "../Pill";

// System — the operator's "state of everything" view. Everything here comes
// from the same 3s `/api/status` poll as the rest of the console; the extra
// blocks (queue, dead letters, spend, runs, index) are aggregated server-side
// so this stays one request per tick.

// trace:v1 id=impl.web-src-components-views-system.empty-bucket work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function emptyBucket(): SpendBucket {
  return {
    runs: 0,
    cost_usd: 0,
    cache_cost_usd: 0,
    miss_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    fallback_runs: 0,
  };
}

const EMPTY: SystemInfo = {
  queue: { pending: [], dead_letters: [], retry_budget: 0 },
  index: [],
  runs: [],
  spend: { today: emptyBucket(), week: emptyBucket(), all_time: emptyBucket() },
};

// trace:v1 id=impl.web-src-components-views-system.issue-label work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function issueLabel(key: string | null): string {
  if (!key) return "—";
  const ref = splitIssueKey(key);
  return ref.number ? `${ref.repo} #${ref.number}` : key;
}

/** "now" once the backoff elapsed, else a countdown to the next attempt. */
// trace:v1 id=impl.web-src-components-views-system.next-attempt work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function nextAttempt(availableAt: string | null): string {
  if (!availableAt) return "now";
  const at = Date.parse(availableAt);
  if (Number.isNaN(at)) return "—";
  const seconds = (at - Date.now()) / 1000;
  return seconds <= 0 ? "now" : `in ${fmtDuration(seconds)}`;
}

// trace:v1 id=impl.web-src-components-views-system.system work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
export function System(): JSX.Element {
// trace:exempt reason=component-local-closure
  const system = (): SystemInfo => statusResource()?.system ?? EMPTY;
// trace:exempt reason=component-local-closure
  const runtime = () => statusResource()?.runtime;

// trace:exempt reason=component-local-closure
  const fallbackShare = (bucket: SpendBucket): string => {
    if (!bucket.runs) return "—";
    return `${Math.round((bucket.fallback_runs / bucket.runs) * 100)}%`;
  };

  return (
    <>
      <SpendCard
        rows={[
          { label: "today", bucket: system().spend.today },
          { label: "7 days", bucket: system().spend.week },
          { label: "all time", bucket: system().spend.all_time },
        ]}
        share={fallbackShare}
      />
      <ProvidersCard
        modelPool={runtime()?.model_pool ?? []}
        fallbacks={runtime()?.fallback_models ?? []}
        thinking={runtime()?.thinking_level}
        triggerMode={runtime()?.trigger_mode}
        triggerLabel={runtime()?.trigger_label}
      />
      <RunsCard runs={system().runs} />
      <QueueCard pending={system().queue.pending} budget={system().queue.retry_budget} />
      <DeadLettersCard letters={system().queue.dead_letters} budget={system().queue.retry_budget} />
      <IndexCard rows={system().index} />
    </>
  );
}

// ── spend ─────────────────────────────────────────────────────────────────

interface SpendCardProps {
  rows: { label: string; bucket: SpendBucket }[];
  share: (bucket: SpendBucket) => string;
}

// trace:v1 id=impl.web-src-components-views-system.spend-card work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function SpendCard(props: SpendCardProps): JSX.Element {
// trace:exempt reason=component-local-closure
  const today = (): SpendBucket => props.rows[0]?.bucket ?? emptyBucket();
  return (
    <GlassCard
      heading="spend"
      accessory={
        <span class="text-[11px] text-ink-300">
          cache reads today <span class="font-mono text-ink-100">{fmtTokens(today().cache_read_tokens)}</span>
        </span>
      }
      contentClass="overflow-x-auto scrollable"
    >
      <table class="t">
        <thead>
          <tr>
            <th>window</th>
            <th>runs</th>
            <th>falls back</th>
            <th>cost</th>
            <th>of which cache</th>
            <th>uncached in</th>
            <th>out</th>
            <th>cache read</th>
            <th>cache write</th>
          </tr>
        </thead>
        <tbody>
          <For each={props.rows}>
            {(row) => (
              <tr>
                <td>{row.label}</td>
                <td class="tabular">{row.bucket.runs}</td>
                <td class="tabular" title={`${row.bucket.fallback_runs} run(s) answered by a fallback model`}>
                  {props.share(row.bucket)}
                </td>
                <td class="tabular font-mono text-ink-100">{fmtCost(row.bucket.cost_usd)}</td>
                <td class="tabular font-mono text-ink-300">{fmtCost(row.bucket.cache_cost_usd)}</td>
                <td class="tabular">{fmtTokens(row.bucket.miss_tokens)}</td>
                <td class="tabular">{fmtTokens(row.bucket.output_tokens)}</td>
                <td class="tabular">{fmtTokens(row.bucket.cache_read_tokens)}</td>
                <td class="tabular">{fmtTokens(row.bucket.cache_write_tokens)}</td>
              </tr>
            )}
          </For>
        </tbody>
      </table>
    </GlassCard>
  );
}

// ── providers ─────────────────────────────────────────────────────────────

interface ProvidersCardProps {
  modelPool: string[];
  fallbacks: string[];
  thinking?: string;
  triggerMode?: string;
  triggerLabel?: string;
}

// trace:v1 id=impl.web-src-components-views-system.providers-card work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function ProvidersCard(props: ProvidersCardProps): JSX.Element {
  return (
    <GlassCard heading="providers" accessory={<span class="text-[11px] text-ink-300">primary → fallbacks</span>}>
      <div class="flex flex-col gap-3">
        <div class="flex flex-wrap items-center gap-2">
          <Show
            when={props.modelPool.length + props.fallbacks.length}
            fallback={<span class="text-[12px] text-ink-300">no models configured</span>}
          >
            <For each={[...props.modelPool.filter(Boolean), ...props.fallbacks]}>
              {(model, index) => (
                <Pill class={index() === 0 ? "done" : "queued"} title={index() === 0 ? "primary" : "fallback"}>
                  <span class="font-mono">{model}</span>
                </Pill>
              )}
            </For>
          </Show>
        </div>
        <div class="flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-ink-300">
          <span>
            thinking <span class="font-mono text-ink-100">{props.thinking ?? "—"}</span>
          </span>
          <span>
            trigger <span class="font-mono text-ink-100">{props.triggerMode ?? "—"}</span>
            {props.triggerLabel ? (
              <span class="font-mono"> @{props.triggerLabel}</span>
            ) : null}
          </span>
        </div>
      </div>
    </GlassCard>
  );
}

// ── recent runs ───────────────────────────────────────────────────────────

interface RunsCardProps {
  runs: RunTelemetry[];
}

// trace:v1 id=impl.web-src-components-views-system.runs-card work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function RunsCard(props: RunsCardProps): JSX.Element {
  return (
    <GlassCard
      heading="recent runs"
      accessory={<span class="tabular">{props.runs.length}</span>}
      contentClass="overflow-x-auto scrollable"
    >
      <Show when={props.runs.length} fallback={<div class="empty">no runs recorded yet</div>}>
        <table class="t">
          <thead>
            <tr>
              <th>ended</th>
              <th>run</th>
              <th>model</th>
              <th>wall</th>
              <th>cost</th>
              <th>tokens in / out</th>
              <th>cache r / w</th>
              <th>state</th>
            </tr>
          </thead>
          <tbody>
            <For each={props.runs}>{(run) => <RunRow run={run} />}</For>
          </tbody>
        </table>
      </Show>
    </GlassCard>
  );
}

// trace:v1 id=impl.web-src-components-views-system.run-row work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function RunRow(props: { run: RunTelemetry }): JSX.Element {
  const run = props.run;
  const ref = splitIssueKey(run.issue_key ?? "");
// trace:exempt reason=component-local-closure
  const href = (): string | null => (ref.number ? issueUrl(ref.repo, ref.number) : null);
  return (
    <tr>
      <td class="text-ink-300">{fmtAge(run.ended_at)}</td>
      <td>
        <Show
          when={href()}
          fallback={
            <span class="font-mono text-[12px] text-ink-100">
              {issueLabel(run.issue_key) || shortDelivery(run.delivery_id)}
            </span>
          }
        >
          <a
            class="font-mono text-[12px] text-ink-100 hover:text-accent-2"
            href={href()!}
            target="_blank"
            rel="noreferrer"
          >
            {issueLabel(run.issue_key) || shortDelivery(run.delivery_id)}
          </a>
        </Show>
      </td>
      <td>
        <span class="font-mono text-[11px] text-ink-100" title={`configured: ${run.model ?? "—"}`}>
          {run.fallback_model ?? run.model ?? "—"}
        </span>
        <Show when={run.fallback_model}>
          <Pill class="queued" title="primary model did not answer; a fallback served this run">
            fallback
          </Pill>
        </Show>
      </td>
      <td class="tabular">{run.duration_ms == null ? "—" : fmtDuration(run.duration_ms / 1000)}</td>
      <td class="tabular font-mono text-ink-100">{fmtCost(run.cost_usd)}</td>
      <td class="tabular">
        {fmtTokens(run.tokens_miss)} / {fmtTokens(run.tokens_out)}
      </td>
      <td class="tabular text-ink-300">
        {fmtTokens(run.tokens_cache_read)} / {fmtTokens(run.tokens_cache_write)}
      </td>
      <td>
        <Pill state={run.state} dot>
          {run.state}
        </Pill>
      </td>
    </tr>
  );
}

// ── queue ─────────────────────────────────────────────────────────────────

interface QueueCardProps {
  pending: PendingEvent[];
  budget: number;
}

// trace:v1 id=impl.web-src-components-views-system.queue-card work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function QueueCard(props: QueueCardProps): JSX.Element {
  return (
    <GlassCard
      heading="queue"
      accessory={
        <span class="text-[11px] text-ink-300">
          retry budget <span class="tabular text-ink-100">{props.budget}</span> · next attempt per row
        </span>
      }
      contentClass="overflow-x-auto scrollable"
    >
      <Show when={props.pending.length} fallback={<div class="empty">queue empty — nothing waiting</div>}>
        <table class="t">
          <thead>
            <tr>
              <th>next attempt</th>
              <th>type</th>
              <th>item</th>
              <th>attempts</th>
              <th>received</th>
            </tr>
          </thead>
          <tbody>
            <For each={props.pending}>
              {(event) => (
                <tr>
                  <td class="tabular text-ink-100">{nextAttempt(event.available_at)}</td>
                  <td>{event.event_type}</td>
                  <td class="font-mono text-[12px]">{issueLabel(event.issue_key) || shortDelivery(event.delivery_id)}</td>
                  <td class="tabular">{event.attempts}</td>
                  <td class="text-ink-300">{fmtAge(event.received_at)}</td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </Show>
    </GlassCard>
  );
}

// ── dead letters ──────────────────────────────────────────────────────────

interface DeadLettersCardProps {
  letters: DeadLetterEvent[];
  budget: number;
}

// trace:v1 id=impl.web-src-components-views-system.dead-letters-card work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function DeadLettersCard(props: DeadLettersCardProps): JSX.Element {
  return (
    <GlassCard
      heading="dead letters"
      accessory={<span class="tabular">{props.letters.length}</span>}
      contentClass="overflow-x-auto scrollable"
    >
      <Show
        when={props.letters.length}
        fallback={<div class="empty">no terminal failures — nothing burned its retry budget</div>}
      >
        <table class="t">
          <thead>
            <tr>
              <th>ended</th>
              <th>item</th>
              <th>attempts</th>
              <th>model</th>
              <th>last error</th>
            </tr>
          </thead>
          <tbody>
            <For each={props.letters}>
              {(letter) => (
                <tr>
                  <td class="text-ink-300">{fmtAge(letter.finished_at ?? letter.received_at)}</td>
                  <td class="font-mono text-[12px]">
                    {issueLabel(letter.issue_key) || shortDelivery(letter.delivery_id)}
                  </td>
                  <td class="tabular" title={`retry budget ${props.budget}`}>
                    {letter.attempts}/{props.budget}
                  </td>
                  <td class="font-mono text-[11px] text-ink-300">{letter.fallback_model ?? letter.model ?? "—"}</td>
                  <td class="text-[12px] text-ink-300" title={letter.last_error ?? undefined}>
                    {shortText(letter.last_error, 90) || "—"}
                  </td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </Show>
    </GlassCard>
  );
}

// ── issue index ───────────────────────────────────────────────────────────

// trace:v1 id=impl.web-src-components-views-system.index-card work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function IndexCard(props: { rows: SystemInfo["index"] }): JSX.Element {
// trace:exempt reason=component-local-closure
  const runtime = () => statusResource()?.runtime;
  return (
    <GlassCard
      heading="issue index"
      accessory={
        <span class="text-[11px] text-ink-300">
          <Show
            when={(runtime()?.issue_index_sync_seconds ?? 0) > 0}
            fallback={<span>background resync off</span>}
          >
            resync every{" "}
            <span class="tabular text-ink-100">{fmtDuration(runtime()?.issue_index_sync_seconds)}</span>
          </Show>
        </span>
      }
      contentClass="overflow-x-auto scrollable"
    >
      <Show
        when={props.rows.length}
        fallback={<div class="empty">index empty — no repos synced yet</div>}
      >
        <table class="t">
          <thead>
            <tr>
              <th>repo</th>
              <th>issues</th>
              <th>PRs</th>
              <th>newest item</th>
              <th>last sync</th>
            </tr>
          </thead>
          <tbody>
            <For each={props.rows}>
              {(row) => (
                <tr>
                  <td class="font-mono text-[12px] text-ink-100">{row.repo}</td>
                  <td class="tabular">{row.rows - row.pull_requests}</td>
                  <td class="tabular">{row.pull_requests}</td>
                  <td class="text-ink-300">{fmtAge(row.newest_issue_at)}</td>
                  <td class="tabular text-ink-300">
                    {row.last_synced ? fmtAge(row.last_synced) : "never"}
                  </td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </Show>
    </GlassCard>
  );
}
