/**
 * Agent Scorecard.
 *
 * Every figure is checked twice: once against a hand-built fixture whose answer
 * is obvious by inspection, and once against the real 500-event run, where the
 * expected value is recomputed from the raw events by a second, independent
 * pass. A test that called `scorecard()` and compared it to itself would prove
 * nothing, so the recomputations here deliberately do not share its helpers.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import AgentScorecard from "../components/AgentScorecard";
import { scorecard } from "../scorecard";
import { makeEvent } from "./fixtures";
import raw from "../../public/batch_results.json";
import type { BatchResults, EventTrace, Outcome } from "../types";

const real = raw as unknown as BatchResults;

function wrap(events: EventTrace[]): BatchResults {
  const count = (outcome: Outcome) =>
    events.filter((e) => e.outcome === outcome).length;
  return {
    batch_run_id: "run-scorecard",
    summary: {
      batch_run_id: "run-scorecard",
      started_at: "2026-04-21T10:00:00Z",
      finished_at: "2026-04-21T10:00:00Z",
      total_events: events.length,
      recovered: count("recovered"),
      blocked: count("blocked"),
      escalated: count("escalated"),
      needs_review: count("needs_review"),
      no_action: count("no_action"),
    },
    events,
    needs_human_review: [],
  };
}

describe("scorecard arithmetic", () => {
  it("splits model use into three populations that account for every row", () => {
    const events = [
      makeEvent({ payment_id: "a", llm_called: true }),
      makeEvent({ payment_id: "b", llm_called: true }),
      makeEvent({ payment_id: "c", llm_called: false }),
      makeEvent({ payment_id: "d", llm_called: null, outcome: "no_action" }),
    ];
    const card = scorecard(wrap(events));

    expect(card.modelUse.reachedModel).toBe(2);
    expect(card.modelUse.shortCircuited).toBe(1);
    expect(card.modelUse.neverConsulted).toBe(1);
    // The three are mutually exclusive and exhaustive, which is what makes the
    // two rates below comparable against the same denominator.
    expect(
      card.modelUse.reachedModel +
        card.modelUse.shortCircuited +
        card.modelUse.neverConsulted,
    ).toBe(events.length);
    expect(card.modelUse.shortCircuitRate).toBeCloseTo(0.25);
    expect(card.modelUse.neverConsultedRate).toBeCloseTo(0.25);
  });

  it("measures the override rate against answers the model actually gave", () => {
    const events = [
      makeEvent({ payment_id: "a", llm_called: true }),
      makeEvent({ payment_id: "b", llm_called: true }),
      makeEvent({
        payment_id: "c",
        llm_called: true,
        original_llm_action: "RETRY_SOFT",
        guard_override_reason: "injection_guard: overridden",
        recommended_action: "ESCALATE_HUMAN",
      }),
      // Short-circuited rows never gave the guard anything to override, so they
      // must not dilute the denominator.
      makeEvent({ payment_id: "d", llm_called: false }),
    ];
    const card = scorecard(wrap(events));

    expect(card.modelUse.overridden).toBe(1);
    expect(card.modelUse.reachedModel).toBe(3);
    expect(card.modelUse.overrideRate).toBeCloseTo(1 / 3);
  });

  it("reports no override rate rather than dividing by zero", () => {
    const card = scorecard(
      wrap([makeEvent({ payment_id: "a", llm_called: null, outcome: "no_action" })]),
    );
    expect(card.modelUse.reachedModel).toBe(0);
    expect(card.modelUse.overrideRate).toBe(0);
  });

  it("counts injection attempts and whether any of them moved money", () => {
    const events = [
      makeEvent({
        payment_id: "a",
        injection_patterns_flagged: ["ignore_previous_instructions"],
        outcome: "escalated",
        final_action: "ESCALATE_HUMAN",
      }),
      // An injection row whose final action debits is the failure case; it has
      // to be counted, not assumed impossible.
      makeEvent({
        payment_id: "b",
        injection_patterns_flagged: ["role_reassignment"],
        outcome: "recovered",
        final_action: "RETRY_SOFT",
      }),
      makeEvent({ payment_id: "c", injection_patterns_flagged: [] }),
    ];
    const card = scorecard(wrap(events));

    expect(card.injection.detected).toBe(2);
    expect(card.injection.movedMoney).toBe(1);
  });

  it("fails the value check when the money does not add up", () => {
    // An outcome outside the five is dropped from the per-outcome sum, which is
    // exactly the silent divergence the check exists to catch.
    const events = [
      makeEvent({ payment_id: "a", amount: 1000, outcome: "recovered" }),
      makeEvent({
        payment_id: "b",
        amount: 500,
        outcome: "impossible" as Outcome,
      }),
    ];
    const card = scorecard(wrap(events));

    expect(card.reconciliationPassed).toBe(false);
    expect(card.reconciliation[0].passed).toBe(false);
    expect(card.reconciliation[2].passed).toBe(false);
  });

  it("fails the duplicate check when one payment appears twice", () => {
    const events = [
      makeEvent({ payment_id: "same" }),
      makeEvent({ payment_id: "same" }),
    ];
    const card = scorecard(wrap(events));

    expect(card.reconciliation[2].passed).toBe(false);
    expect(card.reconciliationPassed).toBe(false);
  });

  it("never claims the run identified its model", () => {
    const card = scorecard(wrap([makeEvent({ llm_called: true })]));
    expect(card.provenance.recorded).toBe(false);
    expect(card.provenance.detail).toContain("StubLLMClient");
  });
});

describe("scorecard against the real run", () => {
  it("matches an independent recomputation of every model-use figure", () => {
    const card = scorecard(real);
    const events = real.events;

    // Recomputed here from the raw fields, not via the scorecard's helpers.
    const reached = events.filter((e) => e.llm_called === true).length;
    const shortCircuited = events.filter((e) => e.llm_called === false).length;
    const never = events.filter((e) => e.llm_called === null).length;
    const overridden = events.filter(
      (e) => e.original_llm_action !== null && e.original_llm_action !== undefined,
    ).length;

    expect(card.modelUse.reachedModel).toBe(reached);
    expect(card.modelUse.shortCircuited).toBe(shortCircuited);
    expect(card.modelUse.neverConsulted).toBe(never);
    expect(card.modelUse.overridden).toBe(overridden);
    expect(card.modelUse.overrideRate).toBeCloseTo(overridden / reached);
    expect(reached + shortCircuited + never).toBe(events.length);
  });

  it("agrees that every override carries both halves of the divergence", () => {
    const overridden = real.events.filter((e) => e.original_llm_action !== null);
    expect(overridden.length).toBeGreaterThan(0);
    for (const event of overridden) {
      expect(event.guard_override_reason).toBeTruthy();
      expect(event.original_llm_action).not.toBe(event.recommended_action);
    }
  });

  it("reconciles the committed run on all three checks", () => {
    const card = scorecard(real);
    expect(card.reconciliation.map((c) => c.passed)).toEqual([true, true, true]);
    expect(card.reconciliationPassed).toBe(true);
  });

  it("counts no injection attempt that moved money", () => {
    const card = scorecard(real);
    const flagged = real.events.filter(
      (e) => e.injection_patterns_flagged.length > 0,
    );
    expect(card.injection.detected).toBe(flagged.length);
    expect(card.injection.movedMoney).toBe(0);
  });
});

describe("AgentScorecard rendering", () => {
  it("shows the three rates, the injection count and the reconciliation", () => {
    render(<AgentScorecard results={real} />);
    const panel = screen.getByTestId("agent-scorecard");

    expect(panel).toHaveTextContent("Agent Scorecard");
    expect(panel).toHaveTextContent(
      "evaluates its own agent's behaviour on every run",
    );
    expect(screen.getByTestId("override-rate")).toBeInTheDocument();
    expect(screen.getByTestId("short-circuit-rate")).toBeInTheDocument();
    expect(screen.getByTestId("never-consulted-rate")).toBeInTheDocument();
    expect(screen.getByTestId("injection-score")).toBeInTheDocument();
    expect(screen.getAllByTestId("check-pass")).toHaveLength(3);
  });

  it("discloses that the run does not identify its model", () => {
    render(<AgentScorecard results={real} />);
    const note = screen.getByTestId("model-provenance");
    expect(note).toHaveTextContent("Model not identified by this run");
    expect(note).toHaveTextContent("StubLLMClient");
    // The disclosure must not read as a live-model evaluation.
    expect(note).toHaveTextContent("not of model judgement");
  });
});
