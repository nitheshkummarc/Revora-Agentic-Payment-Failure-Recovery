/**
 * The agent's own behaviour, scored from the run it just completed.
 *
 * Every other figure on this dashboard answers "what happened to the payments".
 * These answer "how did the agent conduct itself" -- how often it consulted the
 * model at all, how often the deterministic guard had to overrule what came
 * back, and whether the run's own arithmetic reconciles. Revora evaluates its
 * own agent's behaviour on every run, not just whether an action was taken.
 *
 * Nothing here is a new measurement. Every input is a field the orchestrator
 * already wrote to the trace log; this file only aggregates them.
 */

import type { BatchResults, EventTrace, Outcome } from "./types";
import { enforcementSummary } from "./metrics";

/** The five terminal outcomes. A trace carries exactly one. */
const OUTCOMES: Outcome[] = [
  "recovered",
  "blocked",
  "escalated",
  "needs_review",
  "no_action",
];

export interface ModelUse {
  /** Rows where the model was actually called. */
  reachedModel: number;
  /**
   * Rows that reached the recommendation layer but were answered without
   * calling the model, because the tracer marked the evidence too thin.
   */
  shortCircuited: number;
  /**
   * Rows that never reached the layer at all -- the payment had not failed, so
   * there was nothing to recommend.
   */
  neverConsulted: number;
  /** Rows where the guard replaced what the model returned. */
  overridden: number;

  overrideRate: number;
  shortCircuitRate: number;
  neverConsultedRate: number;
}

export interface InjectionScore {
  detected: number;
  movedMoney: number;
}

export interface ReconciliationCheck {
  label: string;
  detail: string;
  passed: boolean;
}

export interface ModelProvenance {
  /**
   * Whether the run data records which model produced these recommendations.
   * The trace log carries no model identity, so this is false for every run the
   * current pipeline writes.
   */
  recorded: boolean;
  label: string;
  detail: string;
}

export interface Scorecard {
  totalEvents: number;
  modelUse: ModelUse;
  injection: InjectionScore;
  reconciliation: ReconciliationCheck[];
  reconciliationPassed: boolean;
  provenance: ModelProvenance;
}

function rate(part: number, whole: number): number {
  return whole > 0 ? part / whole : 0;
}

/**
 * The three checks the naive-baseline comparison applies to its own
 * classification, applied here to the run on screen: the money adds up, every
 * event is accounted for, and no event is counted twice.
 *
 * Computed from the events each time rather than read from a stored result, so
 * a run whose arithmetic stopped reconciling would show it here.
 */
function reconcile(results: BatchResults): ReconciliationCheck[] {
  const events = results.events;
  const summary = results.summary;

  const totalPaise = events.reduce((sum, event) => sum + event.amount, 0);
  const byOutcomePaise = OUTCOMES.reduce(
    (sum, outcome) =>
      sum +
      events
        .filter((event) => event.outcome === outcome)
        .reduce((inner, event) => inner + event.amount, 0),
    0,
  );

  const countByOutcome = OUTCOMES.reduce(
    (sum, outcome) => sum + events.filter((e) => e.outcome === outcome).length,
    0,
  );

  const seen = new Set<string>();
  const duplicated: string[] = [];
  for (const event of events) {
    if (seen.has(event.payment_id)) duplicated.push(event.payment_id);
    seen.add(event.payment_id);
  }
  // An outcome outside the five would be silently dropped from both sums
  // above, so it is called out rather than inferred from a mismatch.
  const unclassified = events.filter(
    (event) => !OUTCOMES.includes(event.outcome),
  );

  return [
    {
      label: "Value reconciles",
      detail: `${byOutcomePaise} paise across outcomes = ${totalPaise} paise in the run`,
      passed: byOutcomePaise === totalPaise,
    },
    {
      label: "Every event accounted for",
      detail: `${countByOutcome} classified of ${summary.total_events} processed`,
      passed:
        countByOutcome === summary.total_events &&
        countByOutcome === events.length,
    },
    {
      label: "No event counted twice",
      detail:
        duplicated.length === 0 && unclassified.length === 0
          ? "each payment appears once, in exactly one outcome"
          : `${duplicated.length} duplicated, ${unclassified.length} unclassified`,
      passed: duplicated.length === 0 && unclassified.length === 0,
    },
  ];
}

/**
 * What the run records about which model answered.
 *
 * The trace log has no field for model identity, so the dashboard cannot tell a
 * stubbed run from a live one. That is stated plainly rather than guessed at:
 * inferring "stub" from the shape of the reasoning text would be a guess
 * presented as a fact, and the one thing this label must never do is imply the
 * model was evaluated when it was not.
 */
function provenance(events: EventTrace[]): ModelProvenance {
  const consulted = events.filter((event) => event.llm_called === true).length;
  return {
    recorded: false,
    label: "Model not identified by this run",
    detail:
      consulted > 0
        ? `${consulted} recommendation(s) came from a model this run does not name. ` +
          "The committed batch is produced by data/run_batch.py, which wires " +
          "StubLLMClient, so unless it was re-run against a live model these are " +
          "stub answers. Treat every figure here as a measurement of the " +
          "deterministic pipeline, not of model judgement."
        : "No model was consulted on this run.",
  };
}

export function scorecard(results: BatchResults): Scorecard {
  const events = results.events;
  const enforcement = enforcementSummary(results);

  // Three mutually exclusive populations, keyed off llm_called: true means the
  // model answered, false means the layer answered without it, and null means
  // the layer was never reached.
  const reachedModel = events.filter((e) => e.llm_called === true).length;
  const shortCircuited = events.filter((e) => e.llm_called === false).length;
  const neverConsulted = events.filter((e) => e.llm_called === null).length;
  const overridden = events.filter((e) => e.original_llm_action !== null).length;

  const checks = reconcile(results);

  return {
    totalEvents: events.length,
    modelUse: {
      reachedModel,
      shortCircuited,
      neverConsulted,
      overridden,
      // Denominator is the rows that actually reached the model: the guard can
      // only override an answer it was given.
      overrideRate: rate(overridden, reachedModel),
      shortCircuitRate: rate(shortCircuited, events.length),
      neverConsultedRate: rate(neverConsulted, events.length),
    },
    injection: {
      detected: enforcement.injectionAttempts,
      movedMoney: enforcement.injectionAttemptsThatMovedMoney,
    },
    reconciliation: checks,
    reconciliationPassed: checks.every((check) => check.passed),
    provenance: provenance(events),
  };
}
