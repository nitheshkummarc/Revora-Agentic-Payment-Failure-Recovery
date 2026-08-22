/**
 * The full pipeline trace for one event, in the order the pipeline ran:
 *
 *   State Resolved -> RCA Diagnosed -> Policy Checked -> Action Taken/Blocked
 *
 * A policy block is rendered as its own distinct stage state rather than as
 * another line of log text -- it is the strongest thing this dashboard has to
 * show, and it should not have to be read for to be noticed.
 *
 * "Copy Trace JSON" copies the raw trace object for this event. Asked for a
 * specific case, that is one click instead of scrolling a log live.
 */

import { useState } from "react";
import type { EventTrace } from "../types";
import { formatRupees } from "../metrics";
import { OUTCOME_LABELS } from "./EventFeed";

interface Props {
  event: EventTrace | null;
}

export function traceJson(event: EventTrace): string {
  return JSON.stringify(event, null, 2);
}

export default function TraceView({ event }: Props) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);

  if (!event) {
    return (
      <section className="trace" data-testid="trace-view">
        <p className="empty" data-testid="trace-empty">
          Select an event to see its full decision trail.
        </p>
      </section>
    );
  }

  const blocked = event.approved === false;

  async function copy() {
    if (!event) return;
    try {
      await navigator.clipboard.writeText(traceJson(event));
      setCopied(true);
      setCopyError(null);
      window.setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      // Clipboard access can be refused outright. Say so rather than showing a
      // success state for something that did not happen.
      setCopyError(error instanceof Error ? error.message : "copy failed");
    }
  }

  return (
    <section className="trace" data-testid="trace-view">
      <div className="trace__head">
        <div>
          <h2>{event.payment_id}</h2>
          <p className="trace__meta">
            {formatRupees(event.amount)} {event.currency} ·{" "}
            <span className={`badge badge--${event.outcome}`}>
              {OUTCOME_LABELS[event.outcome]}
            </span>
          </p>
        </div>
        <button
          type="button"
          className="copy"
          onClick={copy}
          data-testid="copy-trace-json"
        >
          {copied ? "Copied" : "Copy Trace JSON"}
        </button>
      </div>
      {copyError && (
        <p className="trace__copy-error" data-testid="copy-error">
          Could not copy: {copyError}
        </p>
      )}

      <ol className="stages" data-testid="pipeline-stages">
        <Stage
          index={1}
          name="State Resolved"
          testId="stage-state"
          state={event.resolved_state ? "done" : "skipped"}
        >
          <Field label="Resolved state" value={event.resolved_state} />
          <Field label="Rule" value={event.resolution_reason} />
          <Field
            label="Confidence"
            value={fmtConfidence(event.resolution_confidence)}
          />
        </Stage>

        <Stage
          index={2}
          name="RCA Diagnosed"
          testId="stage-rca"
          state={event.root_cause ? (event.ambiguous ? "warn" : "done") : "skipped"}
        >
          <Field label="Root cause" value={event.root_cause} />
          <Field
            label="Confidence"
            value={fmtConfidence(event.trace_confidence)}
          />
          <Field
            label="Ambiguous"
            value={event.ambiguous === null ? null : String(event.ambiguous)}
          />
          {event.ambiguity_reasons.length > 0 && (
            <ul className="reasons" data-testid="ambiguity-reasons">
              {event.ambiguity_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
          {event.causal_chain.length > 0 && (
            <Field label="Causal chain" value={event.causal_chain.join(" → ")} />
          )}
        </Stage>

        <Stage
          index={3}
          name="Policy Checked"
          testId="stage-policy"
          state={
            event.approved === null ? "skipped" : blocked ? "blocked" : "done"
          }
        >
          <Field label="LLM recommended" value={event.recommended_action} />
          <Field
            label="Model called"
            value={event.llm_called === null ? null : String(event.llm_called)}
          />
          {event.injection_patterns_flagged.length > 0 && (
            <Field
              label="Injection patterns"
              value={event.injection_patterns_flagged.join(", ")}
              tone="warn"
            />
          )}
          <Field
            label="Decision"
            value={
              event.approved === null
                ? null
                : blocked
                  ? "BLOCKED"
                  : "APPROVED"
            }
            tone={blocked ? "blocked" : undefined}
          />
          {blocked && (
            <div className="block" data-testid="policy-block">
              <p className="block__rule">
                <code>{event.rule_id}</code>
              </p>
              <p className="block__reason" data-testid="blocked-reason">
                {event.blocked_reason}
              </p>
            </div>
          )}
        </Stage>

        <Stage
          index={4}
          name={blocked ? "Action Blocked" : "Action Taken"}
          testId="stage-action"
          state={blocked ? "blocked" : event.execution ? "done" : "skipped"}
        >
          <Field label="Final action" value={event.final_action} />
          {event.execution ? (
            <>
              <Field label="Detail" value={event.execution.detail} />
              <Field
                label="Gateway called"
                value={String(event.execution.gateway_called)}
              />
              {event.execution.calls.length > 0 && (
                <ul className="reasons" data-testid="gateway-calls">
                  {event.execution.calls.map((call) => (
                    <li key={call}><code>{call}</code></li>
                  ))}
                </ul>
              )}
            </>
          ) : (
            <p className="stage__note" data-testid="no-execution">
              Nothing executed. The block returned before the Execute stage, so
              the payment was never touched.
            </p>
          )}
          {event.verification && (
            <div className="verify" data-testid="verification">
              <Field
                label="Verified"
                value={`expected ${event.verification.expected_state ?? "-"}, observed ${
                  event.verification.observed_state ?? "-"
                }`}
              />
              <Field
                label="Matched"
                value={
                  event.verification.matched === null
                    ? null
                    : String(event.verification.matched)
                }
              />
            </div>
          )}
          {event.needs_review_reason && (
            <Field
              label="Needs review"
              value={event.needs_review_reason}
              tone="warn"
            />
          )}
        </Stage>
      </ol>
    </section>
  );
}

function fmtConfidence(value: number | null): string | null {
  return value === null ? null : value.toFixed(2);
}

function Stage({
  index,
  name,
  state,
  testId,
  children,
}: {
  index: number;
  name: string;
  state: "done" | "blocked" | "warn" | "skipped";
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <li className={`stage stage--${state}`} data-testid={testId}>
      <div className="stage__head">
        <span className="stage__index">{index}</span>
        <h3>{name}</h3>
      </div>
      <div className="stage__body">{children}</div>
    </li>
  );
}

function Field({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | null;
  tone?: string;
}) {
  if (value === null || value === undefined) return null;
  return (
    <p className={`field${tone ? ` field--${tone}` : ""}`}>
      <span className="field__label">{label}</span>
      <span className="field__value">{value}</span>
    </p>
  );
}
