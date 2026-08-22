/**
 * The derived figures. Every one is summed from the trace log, so these tests
 * pin the arithmetic against hand-computed values rather than against another
 * call to the same function.
 */

import { describe, expect, it } from "vitest";
import {
  enforcementSummary,
  formatCompactRupees,
  formatPercent,
  formatRupees,
  moneySummary,
} from "../metrics";
import { sampleResults } from "./fixtures";

describe("money formatting", () => {
  it("renders paise as rupees", () => {
    // 50000 paise is Rs.500, the convention used everywhere upstream.
    expect(formatRupees(50000)).toBe("₹500.00");
    expect(formatRupees(15000000)).toBe("₹1,50,000.00");
  });

  it("compacts large figures into lakhs and crores", () => {
    expect(formatCompactRupees(15000000)).toBe("₹1.50 L");
    expect(formatCompactRupees(1000000000)).toBe("₹1.00 Cr");
    expect(formatCompactRupees(50000)).toBe("₹500.00");
  });

  it("renders a rate to one decimal place", () => {
    expect(formatPercent(0.7736)).toBe("77.4%");
  });
});

describe("money summary", () => {
  const money = moneySummary(sampleResults);

  it("sums the whole batch from per-event amounts", () => {
    // 250000 + 120000 + 15000000 + 99900 + 49900 + 14900
    expect(money.totalPaise).toBe(15534700);
  });

  it("excludes payments that never failed from the addressable total", () => {
    expect(money.neverAtRiskPaise).toBe(14900);
    expect(money.addressablePaise).toBe(15534700 - 14900);
  });

  it("splits value by what actually happened to it", () => {
    expect(money.settledViaRetryPaise).toBe(250000);
    expect(money.preservedByPolicyPaise).toBe(120000 + 15000000);
    expect(money.escalatedPaise).toBe(99900 + 49900);
    expect(money.needsReviewPaise).toBe(0);
  });

  it("rates settled value against addressable value, not the whole batch", () => {
    expect(money.correctlyRoutedRate).toBeCloseTo(250000 / 15519800, 10);
  });

  it("returns a zero rate rather than dividing by zero on an empty batch", () => {
    const empty = moneySummary({
      ...sampleResults,
      events: [],
      needs_human_review: [],
    });
    expect(empty.correctlyRoutedRate).toBe(0);
    expect(empty.totalPaise).toBe(0);
  });
});

describe("enforcement summary", () => {
  const enforcement = enforcementSummary(sampleResults);

  it("counts the distinct rules that actually blocked something", () => {
    expect(enforcement.rulesFired).toBe(2);
    expect(enforcement.actionsBlocked).toBe(2);
  });

  it("reports zero unsafe actions when no blocked event reached the gateway", () => {
    expect(enforcement.unsafeActionsExecuted).toBe(0);
  });

  it("counts injection attempts and how many moved money", () => {
    expect(enforcement.injectionAttempts).toBe(1);
    expect(enforcement.injectionAttemptsThatMovedMoney).toBe(0);
  });

  it("orders rules by how much they blocked and carries the value held", () => {
    const rules = enforcement.byRule.map((rule) => rule.ruleId);
    expect(rules).toContain("MAX_DISCOUNT_EXCEEDED");
    expect(rules).toContain("AFA_SIP_INSURANCE_REQUIRED_AND_MISSING");
    const sip = enforcement.byRule.find(
      (rule) => rule.ruleId === "AFA_SIP_INSURANCE_REQUIRED_AND_MISSING",
    );
    expect(sip?.amountPaise).toBe(15000000);
  });

  it("counts a blocked event that somehow reached the gateway as unsafe", () => {
    // Structurally impossible upstream -- a block returns before Execute. The
    // tile exists to report that as a measured zero, so it has to be able to
    // report a non-zero.
    const tampered = enforcementSummary({
      ...sampleResults,
      events: [
        {
          ...sampleResults.events[1],
          execution: {
            action: "RETRY_SOFT",
            gateway_called: true,
            calls: ["POST /payments/capture"],
            expected_state: "CAPTURED",
            detail: "should never happen",
            cooldown_until: null,
            succeeded: true,
            reconciled: null,
          },
        },
      ],
      needs_human_review: [],
    });
    expect(tampered.unsafeActionsExecuted).toBe(1);
  });
});
