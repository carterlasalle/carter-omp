import { For, type JSX, Show } from "solid-js";

import { CONFIG } from "../config";
import { fmtAge, splitIssueKey } from "../format";
import { statusResource } from "../state";
import type { RecentEvent } from "../types";
import { GlassCard } from "./GlassCard";
import { IssueLink } from "./IssueLink";
import { Pill } from "./Pill";

// trace:v1 id=impl.web-src-components-events.events-props work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
export interface EventsProps {
  onRetry: (deliveryId: string) => void;
}

export function Events(props: EventsProps): JSX.Element {
  const events = (): RecentEvent[] => statusResource()?.recent_events ?? [];

  return (
    <GlassCard heading="recent events" accessory={<span class="tabular">{events().length}</span>}>
      <Show when={events().length} fallback={<div class="empty">no events recorded yet</div>}>
        <div class="overflow-x-auto scrollable">
          <table class="t">
            <thead>
              <tr>
                <th>received</th>
                <th>event</th>
                <th>where</th>
                <th>state</th>
                <th>actor</th>
                <th>trigger</th>
                <th>tries</th>
                <th>error</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <For each={events()}>
                {(event) => <EventRow event={event} onRetry={props.onRetry} />}
              </For>
            </tbody>
          </table>
        </div>
      </Show>
    </GlassCard>
  );
}

interface RowProps {
  event: RecentEvent;
  onRetry: (deliveryId: string) => void;
}

// trace:v1 id=impl.web-src-components-events.event-row work=WORK-CO-Q8Z1HJJJ implements=PLAN-CO-YFKQADAY satisfies=REQ-CO-9N23MPRP
function EventRow(props: RowProps): JSX.Element {
  const ref = (): { repo: string; number: string } => splitIssueKey(props.event.issue_key);
  // `skipped` is a deliberate drop (reason in the error column) and the backend
  // accepts it for retry: re-firing is how an operator re-triggers a mention the
  // bot ignored — e.g. one dropped while the row was still `reviewing`.
// trace:v1 id=impl.web-src-components-events.can-retry work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
  const canRetry = (): boolean =>
    props.event.state === "failed" || props.event.state === "done" || props.event.state === "skipped";

  return (
    <tr>
      <td class="text-ink-300 tabular whitespace-nowrap">{fmtAge(props.event.received_at)}</td>
      <td class="text-ink-200">{props.event.event_type}</td>
      <td>
        <Show
          when={ref().number}
          fallback={<span class="text-ink-300">{props.event.repo ?? "—"}</span>}
        >
          <IssueLink repo={ref().repo} number={ref().number} />
        </Show>
      </td>
      <td>
        <Pill state={props.event.state}>{props.event.state}</Pill>
      </td>
      <td class="text-ink-300 tabular">
        {props.event.actor_login ?? "—"}
        <Show when={props.event.actor_id != null}>
          <span class="text-ink-400"> #{props.event.actor_id}</span>
        </Show>
      </td>
      <td class="text-ink-300 tabular">{props.event.trigger_kind ?? "—"}</td>
      <td class="text-ink-300 tabular">{props.event.attempts}</td>
      <td class="err-cell">{props.event.last_error ?? ""}</td>
      <td>
        <Show
          when={CONFIG.replayEnabled && canRetry()}
          fallback={<span class="text-ink-400">—</span>}
        >
          <button class="tiny" onClick={() => props.onRetry(props.event.delivery_id)}>
            retry
          </button>
        </Show>
      </td>
    </tr>
  );
}
