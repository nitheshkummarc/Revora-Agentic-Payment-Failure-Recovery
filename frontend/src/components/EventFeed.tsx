/**
 * Scrollable list of processed events, colour-coded by final outcome.
 *
 * 500 rows is more than a judge can scan, so the feed filters by outcome. The
 * filter is component state and nothing else -- no storage, no URL rewriting.
 */

import { useMemo, useState } from "react";
import type { BatchResults, EventTrace, Outcome } from "../types";
import { formatRupees } from "../metrics";

export const OUTCOME_LABELS: Record<Outcome, string> = {
  recovered: "Recovered",
  blocked: "Blocked",
  escalated: "Escalated",
  needs_review: "Needs review",
  no_action: "No action",
};

const OUTCOME_ORDER: Outcome[] = [
  "recovered",
  "blocked",
  "escalated",
  "needs_review",
  "no_action",
];

interface Props {
  results: BatchResults;
  selectedPaymentId: string | null;
  onSelect: (event: EventTrace) => void;
}

export default function EventFeed({
  results,
  selectedPaymentId,
  onSelect,
}: Props) {
  const [filter, setFilter] = useState<Outcome | "all">("all");

  const visible = useMemo(
    () =>
      filter === "all"
        ? results.events
        : results.events.filter((event) => event.outcome === filter),
    [results.events, filter],
  );

  return (
    <section className="feed" data-testid="event-feed">
      <div className="feed__head">
        <h2>Event feed</h2>
        <div className="feed__filters" role="group" aria-label="Filter by outcome">
          <button
            type="button"
            className={filter === "all" ? "chip chip--on" : "chip"}
            onClick={() => setFilter("all")}
            data-testid="filter-all"
          >
            All ({results.events.length})
          </button>
          {OUTCOME_ORDER.map((outcome) => {
            const count = results.events.filter(
              (event) => event.outcome === outcome,
            ).length;
            return (
              <button
                key={outcome}
                type="button"
                className={
                  filter === outcome
                    ? `chip chip--on chip--${outcome}`
                    : `chip chip--${outcome}`
                }
                onClick={() => setFilter(outcome)}
                data-testid={`filter-${outcome}`}
              >
                {OUTCOME_LABELS[outcome]} ({count})
              </button>
            );
          })}
        </div>
      </div>

      <ul className="feed__list" data-testid="event-list">
        {visible.map((event) => (
          <li key={event.payment_id}>
            <button
              type="button"
              className={
                event.payment_id === selectedPaymentId
                  ? `row row--${event.outcome} row--selected`
                  : `row row--${event.outcome}`
              }
              onClick={() => onSelect(event)}
              data-testid={`event-row-${event.payment_id}`}
              aria-current={event.payment_id === selectedPaymentId}
            >
              <span className={`badge badge--${event.outcome}`}>
                {OUTCOME_LABELS[event.outcome]}
              </span>
              <span className="row__id">{event.payment_id}</span>
              <span className="row__amount">{formatRupees(event.amount)}</span>
              <span className="row__cause">
                {event.root_cause ?? "no failure to diagnose"}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {visible.length === 0 && (
        <p className="empty" data-testid="feed-empty">
          No events with this outcome.
        </p>
      )}
    </section>
  );
}
