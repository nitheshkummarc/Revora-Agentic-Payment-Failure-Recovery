/**
 * Figures derived from the per-event trace log.
 *
 * Every number here is summed from `events[].amount`, which the orchestrator
 * copies onto each trace precisely so the dashboard never has to re-join the
 * source dataset. Nothing is fabricated and nothing is hardcoded.
 *
 * On the naming, which is deliberate and not cosmetic: the rate below is a
 * **correctly routed rate**, not a recovery rate. A retry is executed against
 * the mock gateway, where it always succeeds, so this measures whether each
 * payment reached its correct decision -- not how often a real retry would
 * land. Presenting it as recovery would claim a measurement the project has no
 * data to support.
 */

import type { BatchResults, EventTrace, Outcome } from "./types";

export const PAISE_PER_RUPEE = 100;

/** Rupees, from paise. Money is integer paise everywhere upstream. */
export function formatRupees(paise: number): string {
  return `₹${(paise / PAISE_PER_RUPEE).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatCompactRupees(paise: number): string {
  const rupees = paise / PAISE_PER_RUPEE;
  if (rupees >= 10_000_000) return `₹${(rupees / 10_000_000).toFixed(2)} Cr`;
  if (rupees >= 100_000) return `₹${(rupees / 100_000).toFixed(2)} L`;
  return formatRupees(paise);
}

export function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export interface RuleEnforcement {
  ruleId: string;
  blocked: number;
  amountPaise: number;
}

export interface EnforcementSummary {
  /** Distinct rules that actually blocked something in this run. */
  rulesFired: number;
  /** Total actions the policy engine refused. */
  actionsBlocked: number;
  /**
   * Blocked events that nevertheless executed something. Structurally always
   * zero -- a block returns before the Execute stage -- and shown because a
   * guardrail claim is worth stating as a measured figure rather than asserted.
   */
  unsafeActionsExecuted: number;
  /** Rows whose customer note matched an instruction-like pattern. */
  injectionAttempts: number;
  /** Injection-carrying rows that produced a money-moving action. */
  injectionAttemptsThatMovedMoney: number;
  byRule: RuleEnforcement[];
}

export interface MoneySummary {
  totalPaise: number;
  /** no_action payments that had already succeeded (resolved AUTHORIZED/CAPTURED). */
  neverAtRiskPaise: number;
  addressablePaise: number;
  /** Settled via retry plus found already paid. The rate uses this. */
  recoveredPaise: number;
  /** What the retries actually charged, net of discounts. */
  settledViaRetryPaise: number;
  /** Discounts taken off retried payments. */
  discountGivenPaise: number;
  /** Recovered because a status query found the payment had succeeded. */
  foundAlreadyPaidPaise: number;
  preservedByPolicyPaise: number;
  escalatedPaise: number;
  needsReviewPaise: number;
  /** no_action payments still at risk: in flight, or cooling down after a failure. */
  noActionAtRiskPaise: number;
  correctlyRoutedRate: number;
}

/**
 * Actions that move money. Mirrors MONEY_MOVING_ACTIONS in
 * backend/app/intelligence/schemas.py; a test checks the two match.
 */
export const MONEY_MOVING_ACTIONS = new Set(["RETRY_SOFT"]);

const SUCCEEDED_STATES = new Set(["AUTHORIZED", "CAPTURED"]);

/** A no_action payment that had already succeeded and was never at risk. */
function alreadySucceeded(event: EventTrace): boolean {
  return (
    event.outcome === "no_action" &&
    event.resolved_state !== null &&
    SUCCEEDED_STATES.has(event.resolved_state)
  );
}

export function sumAmount(events: EventTrace[], outcome: Outcome): number {
  return events
    .filter((event) => event.outcome === outcome)
    .reduce((total, event) => total + event.amount, 0);
}

export function enforcementSummary(results: BatchResults): EnforcementSummary {
  const events = results.events;
  const blocked = events.filter((event) => event.outcome === "blocked");

  const byRuleMap = new Map<string, RuleEnforcement>();
  for (const event of blocked) {
    const ruleId = event.rule_id ?? "UNATTRIBUTED";
    const existing = byRuleMap.get(ruleId) ?? {
      ruleId,
      blocked: 0,
      amountPaise: 0,
    };
    existing.blocked += 1;
    existing.amountPaise += event.amount;
    byRuleMap.set(ruleId, existing);
  }

  const injectionRows = events.filter(
    (event) => event.injection_patterns_flagged.length > 0,
  );

  return {
    rulesFired: byRuleMap.size,
    actionsBlocked: blocked.length,
    unsafeActionsExecuted: blocked.filter(
      (event) => event.execution?.gateway_called === true,
    ).length,
    injectionAttempts: injectionRows.length,
    injectionAttemptsThatMovedMoney: injectionRows.filter(
      (event) =>
        event.final_action !== null && MONEY_MOVING_ACTIONS.has(event.final_action),
    ).length,
    byRule: [...byRuleMap.values()].sort((a, b) => b.blocked - a.blocked),
  };
}

export function moneySummary(results: BatchResults): MoneySummary {
  const events = results.events;
  const sum = (keep: (event: EventTrace) => boolean) =>
    events.filter(keep).reduce((total, event) => total + event.amount, 0);

  const totalPaise = sum(() => true);
  const neverAtRiskPaise = sum(alreadySucceeded);
  const addressablePaise = totalPaise - neverAtRiskPaise;

  const retried = events.filter(
    (event) =>
      event.outcome === "recovered" && event.execution?.action === "RETRY_SOFT",
  );
  const settledViaRetryPaise = retried.reduce(
    (total, event) => total + (event.execution?.charged_amount ?? event.amount),
    0,
  );
  const discountGivenPaise = retried.reduce(
    (total, event) => total + (event.execution?.discount_applied ?? 0),
    0,
  );
  const foundAlreadyPaidPaise =
    sumAmount(events, "recovered") -
    retried.reduce((total, event) => total + event.amount, 0);
  const recoveredPaise = settledViaRetryPaise + foundAlreadyPaidPaise;

  return {
    totalPaise,
    neverAtRiskPaise,
    addressablePaise,
    recoveredPaise,
    settledViaRetryPaise,
    discountGivenPaise,
    foundAlreadyPaidPaise,
    preservedByPolicyPaise: sumAmount(events, "blocked"),
    escalatedPaise: sumAmount(events, "escalated"),
    needsReviewPaise: sumAmount(events, "needs_review"),
    noActionAtRiskPaise: sum(
      (event) => event.outcome === "no_action" && !alreadySucceeded(event),
    ),
    correctlyRoutedRate:
      addressablePaise > 0 ? recoveredPaise / addressablePaise : 0,
  };
}
