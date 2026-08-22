/**
 * Always-visible run summary.
 *
 * Ordering is a decision, not a layout preference. Enforcement leads because
 * it is what the batch actually proves: every rule that fired, and the count of
 * unsafe actions that got through. The money figures come second and are
 * labelled as value routed through verified-safe paths -- not as money
 * recovered by intelligent retry, which the run cannot demonstrate. The rate
 * comes last, named for what it measures.
 */

import type { BatchResults } from "../types";
import {
  enforcementSummary,
  formatCompactRupees,
  formatPercent,
  formatRupees,
  moneySummary,
} from "../metrics";

interface Props {
  results: BatchResults;
  sourceDetail: string;
}

export default function SummaryHeader({ results, sourceDetail }: Props) {
  const enforcement = enforcementSummary(results);
  const money = moneySummary(results);
  const summary = results.summary;

  return (
    <header className="summary" data-testid="summary-header">
      <div className="summary__title">
        <h1>Revora X-Ray</h1>
        <p className="summary__source" data-testid="results-source">
          {summary.total_events} events · {sourceDetail}
        </p>
      </div>

      {/* (a) The proven claim. */}
      <section className="panel panel--primary" data-testid="enforcement-panel">
        <h2>Guardrail enforcement</h2>
        <p className="panel__lede" data-testid="enforcement-claim">
          {enforcement.rulesFired} policy rules fired,{" "}
          {enforcement.actionsBlocked} actions blocked,{" "}
          {enforcement.unsafeActionsExecuted} unsafe actions executed.
        </p>
        <p className="panel__lede panel__lede--muted">
          {enforcement.injectionAttempts} prompt-injection attempts detected,{" "}
          {enforcement.injectionAttemptsThatMovedMoney} produced a money-moving
          action.
        </p>
        <table className="rules" data-testid="rules-fired-table">
          <thead>
            <tr>
              <th scope="col">Rule</th>
              <th scope="col">Blocked</th>
              <th scope="col">Value held</th>
            </tr>
          </thead>
          <tbody>
            {enforcement.byRule.map((rule) => (
              <tr key={rule.ruleId} data-testid={`rule-row-${rule.ruleId}`}>
                <td><code>{rule.ruleId}</code></td>
                <td className="num">{rule.blocked}</td>
                <td className="num">{formatRupees(rule.amountPaise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel" data-testid="counts-panel">
        <h2>Outcomes</h2>
        <div className="tiles">
          <Tile label="Recovered" value={summary.recovered} tone="recovered" />
          <Tile label="Blocked" value={summary.blocked} tone="blocked" />
          <Tile label="Escalated" value={summary.escalated} tone="escalated" />
          <Tile
            label="Needs review"
            value={summary.needs_review}
            tone="needs_review"
          />
          <Tile label="No action" value={summary.no_action} tone="no_action" />
        </div>
      </section>

      {/* (b) Money, framed for what it is. */}
      <section className="panel" data-testid="money-panel">
        <h2>Money moved through verified-safe paths</h2>
        <p className="panel__note">
          Every figure is summed from the per-event trace log. Not a claim about
          how often a real retry would succeed.
        </p>
        <div className="tiles">
          <Tile
            label="Batch value"
            value={formatCompactRupees(money.totalPaise)}
          />
          <Tile
            label="Addressable at risk"
            value={formatCompactRupees(money.addressablePaise)}
          />
          <Tile
            label="Settled via retry"
            value={formatCompactRupees(money.settledViaRetryPaise)}
            tone="recovered"
          />
          <Tile
            label="Preserved by policy"
            value={formatCompactRupees(money.preservedByPolicyPaise)}
            tone="blocked"
          />
          <Tile
            label="Escalated"
            value={formatCompactRupees(money.escalatedPaise)}
            tone="escalated"
          />
        </div>
      </section>

      {/* (c) The rate, named for what it measures. */}
      <section className="panel" data-testid="rate-panel">
        <h2>Correctly Routed Rate</h2>
        <p className="rate" data-testid="correctly-routed-rate">
          {formatPercent(money.correctlyRoutedRate)}
        </p>
        <p className="panel__note">
          Share of addressable value that reached its correct decision. A retry
          always succeeds against the mock gateway, so this measures routing and
          guardrail enforcement, not retry success probability.
        </p>
      </section>
    </header>
  );
}

function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: string;
}) {
  return (
    <div className={`tile${tone ? ` tile--${tone}` : ""}`}>
      <span className="tile__value">{value}</span>
      <span className="tile__label">{label}</span>
    </div>
  );
}
