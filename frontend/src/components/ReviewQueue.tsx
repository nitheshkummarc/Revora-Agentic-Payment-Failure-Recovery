/**
 * Human-review queue: every event the run handed to a person, with the reason.
 *
 * Read-only. Recording reviewer decisions would need storage and auth, which
 * this build does not have. The CSV export is a file download, not browser
 * storage.
 */

import { useMemo, useState } from "react";
import type { BatchResults, EventTrace, HumanReviewItem, Outcome } from "../types";
import { formatRupees } from "../metrics";
import { OUTCOME_LABELS } from "./EventFeed";

interface Props {
  results: BatchResults;
  onSelect: (event: EventTrace) => void;
}

export interface QueueRow {
  item: HumanReviewItem;
  event: EventTrace | undefined;
  outcome: Outcome | undefined;
}

/** Join each review item to its trace, so the queue can show the outcome. */
export function queueRows(results: BatchResults): QueueRow[] {
  const byPayment = new Map(results.events.map((event) => [event.payment_id, event]));
  return results.needs_human_review.map((item) => {
    const event = byPayment.get(item.payment_id);
    return { item, event, outcome: event?.outcome };
  });
}

function csvCell(value: string | number | null | undefined): string {
  const text = value === null || value === undefined ? "" : String(value);
  // RFC 4180: quote every cell and double embedded quotes.
  return `"${text.replace(/"/g, '""')}"`;
}

/** The queue as CSV: one row per item, amounts in rupees and in paise. */
export function reviewQueueCsv(rows: QueueRow[]): string {
  const header = [
    "payment_id",
    "outcome",
    "final_action",
    "amount_rupees",
    "amount_paise",
    "reason",
    "root_cause",
  ];
  const lines = rows.map(({ item, outcome }) =>
    [
      item.payment_id,
      outcome ?? "",
      item.final_action ?? "",
      (item.amount / 100).toFixed(2),
      item.amount,
      item.reason,
      item.root_cause,
    ]
      .map(csvCell)
      .join(","),
  );
  return [header.map(csvCell).join(","), ...lines].join("\r\n");
}

const FILTERS: Array<Outcome | "all"> = ["all", "escalated", "blocked", "needs_review"];

export default function ReviewQueue({ results, onSelect }: Props) {
  const rows = useMemo(() => queueRows(results), [results]);
  const [filter, setFilter] = useState<Outcome | "all">("all");
  const visible = filter === "all" ? rows : rows.filter((row) => row.outcome === filter);
  const totalPaise = visible.reduce((sum, row) => sum + row.item.amount, 0);

  function download() {
    const blob = new Blob([reviewQueueCsv(visible)], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `review-queue-${results.batch_run_id}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="blocklog review-queue" data-testid="review-queue">
      <div className="blocklog__head">
        <h2>Human review queue</h2>
        <p className="blocklog__summary" data-testid="review-summary">
          {visible.length} of {rows.length} item(s) · {formatRupees(totalPaise)}
        </p>
      </div>

      <div className="feed__filters" role="group" aria-label="Filter review queue">
        {FILTERS.map((option) => (
          <button
            key={option}
            type="button"
            className={filter === option ? "chip chip--on" : "chip"}
            onClick={() => setFilter(option)}
            data-testid={`review-filter-${option}`}
          >
            {option === "all" ? "All" : OUTCOME_LABELS[option]} (
            {option === "all"
              ? rows.length
              : rows.filter((row) => row.outcome === option).length}
            )
          </button>
        ))}
        <button
          type="button"
          className="chip"
          onClick={download}
          disabled={visible.length === 0}
          data-testid="review-download"
        >
          Download CSV
        </button>
      </div>

      {visible.length === 0 ? (
        <p className="empty" data-testid="review-empty">
          Nothing in this part of the queue.
        </p>
      ) : (
        <ul className="blocklog__list" data-testid="review-list">
          {visible.map(({ item, event, outcome }) => (
            <li key={item.payment_id}>
              <button
                type="button"
                className="blockcard"
                onClick={() => event && onSelect(event)}
                disabled={!event}
                data-testid={`review-row-${item.payment_id}`}
              >
                <div className="blockcard__head">
                  {outcome && (
                    <span className={`badge badge--${outcome}`}>
                      {OUTCOME_LABELS[outcome]}
                    </span>
                  )}
                  <span className="blockcard__amount">{formatRupees(item.amount)}</span>
                </div>
                <p className="blockcard__id">{item.payment_id}</p>
                <p className="blockcard__reason">{item.reason}</p>
                <p className="blockcard__override">
                  Final action <strong>{item.final_action ?? "none decided"}</strong>
                </p>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
