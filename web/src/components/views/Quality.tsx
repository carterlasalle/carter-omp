import { createResource, For, type JSX, Show } from "solid-js";

import { GlassCard } from "../GlassCard";

// Quality — outcome-backed metrics from `/api/quality`. Unlike the other
// views this is a slow-changing aggregate, so it fetches once per mount
// rather than joining the 3s status poll.

interface QualityResponse {
  outcomes: Record<string, number>;
  resolved: number;
  decided: number;
  cost_usd: number;
  cost_per_resolution_usd: number | null;
  rework_rate: number | null;
  classification_revisions: number;
}

async function fetchQuality(): Promise<QualityResponse> {
  const resp = await fetch("/api/quality");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return (await resp.json()) as QualityResponse;
}

const OUTCOME_ORDER = ["resolved", "incomplete", "needs_input", "cancelled", "infra_failed"];

function Stat(props: { label: string; value: string; hint: string }): JSX.Element {
  return (
    <div
      style={{
        padding: "16px 18px",
        border: "1px solid var(--border, rgba(255,255,255,.08))",
        "border-radius": "10px",
      }}
    >
      <div style={{ "font-size": "12px", opacity: 0.6 }}>{props.label}</div>
      <div style={{ "font-size": "28px", "font-weight": 600, "line-height": 1.2 }}>{props.value}</div>
      <div style={{ "font-size": "12px", opacity: 0.6 }}>{props.hint}</div>
    </div>
  );
}

export function Quality(): JSX.Element {
  const [data] = createResource(fetchQuality);

  const pct = (v: number | null): string => (v == null ? "—" : `${Math.round(v * 100)}%`);
  const usd = (v: number | null): string => (v == null ? "—" : `$${v.toFixed(2)}`);

  return (
    <Show when={data()} fallback={<div style={{ padding: "24px", opacity: 0.6 }}>Loading quality metrics…</div>}>
      {(q) => (
        <div style={{ display: "flex", "flex-direction": "column", gap: "16px" }}>
          <div
            style={{
              display: "grid",
              "grid-template-columns": "repeat(auto-fit, minmax(180px, 1fr))",
              gap: "12px",
            }}
          >
            <Stat label="Resolved" value={String(q().resolved)} hint={`of ${q().decided} decided runs`} />
            <Stat
              label="Cost per resolution"
              value={usd(q().cost_per_resolution_usd)}
              hint={`$${q().cost_usd.toFixed(2)} total spend`}
            />
            <Stat label="Rework rate" value={pct(q().rework_rate)} hint="resolved runs with a follow-up fix" />
            <Stat
              label="Classification revisions"
              value={String(q().classification_revisions)}
              hint="labels corrected with new evidence"
            />
          </div>
          <GlassCard heading="Outcome breakdown" bare>
            <table style={{ width: "100%", "border-collapse": "collapse", "font-size": "13px" }}>
              <tbody>
                <For each={OUTCOME_ORDER}>
                  {(outcome) => (
                    <tr>
                      <td style={{ padding: "10px 16px", opacity: 0.75 }}>{outcome}</td>
                      <td style={{ padding: "10px 16px", "text-align": "right" }}>
                        {q().outcomes[outcome] ?? 0}
                      </td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
          </GlassCard>
        </div>
      )}
    </Show>
  );
}
