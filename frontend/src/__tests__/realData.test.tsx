/**
 * The dashboard against the real committed batch run, not fixtures.
 *
 * Fixtures prove the components behave; they cannot prove the components match
 * what the backend actually emits. This file loads
 * `data/batch_results.json` -- the same 500-event artefact the orchestrator
 * wrote and the dashboard fetches at runtime -- and checks that the figures the
 * header derives agree with figures computed independently here.
 *
 * If the backend's schema drifts, this is the test that fails.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "../App";
import SummaryHeader from "../components/SummaryHeader";
import TraceView from "../components/TraceView";
import { enforcementSummary, formatPercent, moneySummary } from "../metrics";
import type { BatchResults, EventTrace, Outcome } from "../types";

const REAL_RESULTS: BatchResults = JSON.parse(
  readFileSync(resolve(__dirname, "..", "..", "..", "data", "batch_results.json"), "utf-8"),
);

function loader() {
  return vi.fn().mockResolvedValue({
    results: REAL_RESULTS,
    source: "snapshot" as const,
    detail: `committed snapshot (run ${REAL_RESULTS.batch_run_id})`,
  });
}

describe("real batch results", () => {
  it("is the full 500-event run", () => {
    expect(REAL_RESULTS.events).toHaveLength(500);
    expect(REAL_RESULTS.summary.total_events).toBe(500);
  });

  it("carries every field the dashboard reads on every event", () => {
    for (const event of REAL_RESULTS.events) {
      expect(typeof event.payment_id).toBe("string");
      expect(Number.isInteger(event.amount)).toBe(true);
      expect(event.currency).toBe("INR");
      expect(Array.isArray(event.causal_chain)).toBe(true);
      expect(Array.isArray(event.injection_patterns_flagged)).toBe(true);
      expect(Array.isArray(event.ambiguity_reasons)).toBe(true);
    }
  });

  it("only carries outcomes the feed knows how to render", () => {
    const known: Outcome[] = [
      "recovered",
      "blocked",
      "escalated",
      "needs_review",
      "no_action",
    ];
    const seen = new Set(REAL_RESULTS.events.map((event) => event.outcome));
    for (const outcome of seen) {
      expect(known).toContain(outcome);
    }
  });

  it("derives counts that agree with the backend's own summary", () => {
    const summary = REAL_RESULTS.summary;
    const count = (outcome: Outcome) =>
      REAL_RESULTS.events.filter((event) => event.outcome === outcome).length;

    expect(count("recovered")).toBe(summary.recovered);
    expect(count("blocked")).toBe(summary.blocked);
    expect(count("escalated")).toBe(summary.escalated);
    expect(count("needs_review")).toBe(summary.needs_review);
    expect(count("no_action")).toBe(summary.no_action);
  });

  it("splits the batch value with no money unaccounted for", () => {
    const money = moneySummary(REAL_RESULTS);
    const parts =
      money.settledViaRetryPaise +
      money.preservedByPolicyPaise +
      money.escalatedPaise +
      money.needsReviewPaise +
      money.neverAtRiskPaise;
    expect(parts).toBe(money.totalPaise);
    expect(money.addressablePaise).toBeLessThan(money.totalPaise);
  });

  it("reports every rule the run actually enforced", () => {
    const enforcement = enforcementSummary(REAL_RESULTS);
    expect(enforcement.actionsBlocked).toBe(REAL_RESULTS.summary.blocked);
    expect(enforcement.rulesFired).toBeGreaterThanOrEqual(9);
    expect(enforcement.byRule.map((rule) => rule.ruleId)).toContain(
      "CUSTOMER_OPTED_OUT",
    );
    // Nothing blocked reached the gateway.
    expect(enforcement.unsafeActionsExecuted).toBe(0);
  });

  it("shows no injection attempt producing a money-moving action", () => {
    const enforcement = enforcementSummary(REAL_RESULTS);
    expect(enforcement.injectionAttempts).toBeGreaterThan(0);
    expect(enforcement.injectionAttemptsThatMovedMoney).toBe(0);
  });

  it("renders the header against real data with the derived rate", () => {
    render(<SummaryHeader results={REAL_RESULTS} sourceDetail="committed snapshot" />);
    const money = moneySummary(REAL_RESULTS);
    expect(screen.getByTestId("correctly-routed-rate")).toHaveTextContent(
      formatPercent(money.correctlyRoutedRate),
    );
    expect(screen.getByTestId("summary-header").textContent).not.toMatch(
      /recovery rate/i,
    );
  });

  it("renders every real event's trace without throwing", () => {
    // Real data has nulls in places fixtures do not -- an event that never
    // reached the policy stage, one with no execution record, one with no root
    // cause. Rendering all 500 is the cheapest way to prove none of them break
    // the view.
    for (const event of REAL_RESULTS.events) {
      const { unmount } = render(<TraceView event={event} />);
      expect(screen.getByTestId("pipeline-stages")).toBeInTheDocument();
      unmount();
    }
  });

  it("opens a real blocked event and shows its rule and reason", async () => {
    const user = userEvent.setup();
    render(<App load={loader()} />);
    await screen.findByTestId("event-feed");

    await user.click(screen.getByTestId("tab-blocks"));
    const list = screen.getByTestId("blocklog-list");
    const firstRow = within(list).getAllByRole("button")[0];
    await user.click(firstRow);

    const blockedEvent = REAL_RESULTS.events.find(
      (event: EventTrace) => event.outcome === "blocked",
    )!;
    expect(screen.getByTestId("policy-block")).toBeInTheDocument();
    expect(screen.getByTestId("blocked-reason").textContent).toBeTruthy();
    expect(blockedEvent.rule_id).toBeTruthy();
  });

  it("filters the real feed down to the escalated events", async () => {
    const user = userEvent.setup();
    render(<App load={loader()} />);
    await screen.findByTestId("event-feed");

    await user.click(screen.getByTestId("filter-escalated"));
    const rows = within(screen.getByTestId("event-list")).getAllByRole("listitem");
    expect(rows).toHaveLength(REAL_RESULTS.summary.escalated);
  });
});
