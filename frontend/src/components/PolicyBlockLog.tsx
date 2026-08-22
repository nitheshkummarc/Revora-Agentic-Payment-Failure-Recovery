/**
 * Blocked events only, with the rule and the reason string that stopped each.
 *
 * A dedicated view rather than a filter on the feed: this is the panel to have
 * open while demonstrating a policy violation, and it should not need to be
 * re-filtered to get back to.
 */

import { useMemo, useState } from "react";
import type { BatchResults, EventTrace } from "../types";
import { formatRupees } from "../metrics";

interface Props {
  results: BatchResults;
  onSelect: (event: EventTrace) => void;
}

export default function PolicyBlockLog({ results, onSelect }: Props) {
  const blocked = useMemo(
    () => results.events.filter((event) => event.outcome === "blocked"),
    [results.events],
  );

  const rules = useMemo(() => {
    const counts = new Map<string, number>();
    for (const event of blocked) {
      const ruleId = event.rule_id ?? "UNATTRIBUTED";
      counts.set(ruleId, (counts.get(ruleId) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [blocked]);

  const [ruleFilter, setRuleFilter] = useState<string | "all">("all");
  const visible =
    ruleFilter === "all"
      ? blocked
      : blocked.filter((event) => (event.rule_id ?? "UNATTRIBUTED") === ruleFilter);

  const heldPaise = visible.reduce((total, event) => total + event.amount, 0);

  return (
    <section className="blocklog" data-testid="policy-block-log">
      <div className="blocklog__head">
        <h2>Policy block log</h2>
        <p className="blocklog__summary" data-testid="blocklog-summary">
          {blocked.length} actions blocked across {rules.length} rules ·{" "}
          {formatRupees(heldPaise)} held from the shown selection
        </p>
      </div>

      <div className="feed__filters" role="group" aria-label="Filter by rule">
        <button
          type="button"
          className={ruleFilter === "all" ? "chip chip--on" : "chip"}
          onClick={() => setRuleFilter("all")}
          data-testid="blocklog-filter-all"
        >
          All ({blocked.length})
        </button>
        {rules.map(([ruleId, count]) => (
          <button
            key={ruleId}
            type="button"
            className={ruleFilter === ruleId ? "chip chip--on chip--blocked" : "chip"}
            onClick={() => setRuleFilter(ruleId)}
            data-testid={`blocklog-filter-${ruleId}`}
          >
            {ruleId} ({count})
          </button>
        ))}
      </div>

      {blocked.length === 0 ? (
        <p className="empty" data-testid="blocklog-empty">
          Nothing was blocked in this run.
        </p>
      ) : (
        <ul className="blocklog__list" data-testid="blocklog-list">
          {visible.map((event) => (
            <li key={event.payment_id}>
              <button
                type="button"
                className="blockcard"
                onClick={() => onSelect(event)}
                data-testid={`blocklog-row-${event.payment_id}`}
              >
                <div className="blockcard__head">
                  <code className="blockcard__rule">{event.rule_id}</code>
                  <span className="blockcard__amount">
                    {formatRupees(event.amount)}
                  </span>
                </div>
                <p className="blockcard__id">{event.payment_id}</p>
                <p className="blockcard__reason">{event.blocked_reason}</p>
                <p className="blockcard__override">
                  LLM recommended <strong>{event.recommended_action}</strong> →
                  final <strong>{event.final_action}</strong>
                </p>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
