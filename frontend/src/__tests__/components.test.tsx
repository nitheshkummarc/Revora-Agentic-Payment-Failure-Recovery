/**
 * Component behaviour, checked against what the prompt requires each one to
 * show: colour-coded outcomes in the feed, the four pipeline stages in order,
 * a visually distinct policy block, a working Copy Trace JSON, and a dedicated
 * blocked-only view.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import EventFeed from "../components/EventFeed";
import PolicyBlockLog from "../components/PolicyBlockLog";
import ReviewQueue, { queueRows, reviewQueueCsv } from "../components/ReviewQueue";
import SummaryHeader from "../components/SummaryHeader";
import TraceView, { traceJson } from "../components/TraceView";
import {
  blocked,
  blockedSip,
  escalated,
  makeEvent,
  noAction,
  recovered,
  sampleResults,
} from "./fixtures";

describe("EventFeed", () => {
  it("lists every event with its outcome badge", () => {
    render(
      <EventFeed results={sampleResults} selectedPaymentId={null} onSelect={vi.fn()} />,
    );
    const list = screen.getByTestId("event-list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(6);
    expect(screen.getByTestId("event-row-pay_recovered")).toHaveTextContent("Recovered");
    expect(screen.getByTestId("event-row-pay_blocked")).toHaveTextContent("Blocked");
    expect(screen.getByTestId("event-row-pay_escalated")).toHaveTextContent("Escalated");
  });

  it("colour-codes each row by outcome", () => {
    render(
      <EventFeed results={sampleResults} selectedPaymentId={null} onSelect={vi.fn()} />,
    );
    // The class is what carries the colour; asserting it keeps the code path
    // honest without asserting on computed styles.
    expect(screen.getByTestId("event-row-pay_recovered").className).toContain(
      "row--recovered",
    );
    expect(screen.getByTestId("event-row-pay_blocked").className).toContain(
      "row--blocked",
    );
    expect(screen.getByTestId("event-row-pay_no_action").className).toContain(
      "row--no_action",
    );
  });

  it("filters to one outcome and back", async () => {
    const user = userEvent.setup();
    render(
      <EventFeed results={sampleResults} selectedPaymentId={null} onSelect={vi.fn()} />,
    );

    await user.click(screen.getByTestId("filter-blocked"));
    expect(within(screen.getByTestId("event-list")).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByTestId("event-row-pay_recovered")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("filter-all"));
    expect(within(screen.getByTestId("event-list")).getAllByRole("listitem")).toHaveLength(6);
  });

  it("says so when a filter matches nothing", async () => {
    const user = userEvent.setup();
    render(
      <EventFeed results={sampleResults} selectedPaymentId={null} onSelect={vi.fn()} />,
    );
    await user.click(screen.getByTestId("filter-needs_review"));
    expect(screen.getByTestId("feed-empty")).toBeInTheDocument();
  });

  it("hands the selected event to its caller", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <EventFeed results={sampleResults} selectedPaymentId={null} onSelect={onSelect} />,
    );
    await user.click(screen.getByTestId("event-row-pay_blocked"));
    expect(onSelect).toHaveBeenCalledWith(blocked);
  });
});

describe("TraceView", () => {
  // jsdom exposes navigator.clipboard as a getter-only property, so it has to
  // be redefined rather than assigned.
  function stubClipboard(writeText: ReturnType<typeof vi.fn>) {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });
    return writeText;
  }

  beforeEach(() => {
    stubClipboard(vi.fn().mockResolvedValue(undefined));
  });

  it("prompts for a selection when nothing is selected", () => {
    render(<TraceView event={null} />);
    expect(screen.getByTestId("trace-empty")).toBeInTheDocument();
  });

  it("shows the four pipeline stages in the documented order", () => {
    render(<TraceView event={recovered} />);
    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((node) => node.textContent);
    expect(headings).toEqual([
      "State Resolved",
      "RCA Diagnosed",
      "Policy Checked",
      "Action Taken",
    ]);
  });

  it("shows each stage's output", () => {
    render(<TraceView event={recovered} />);
    expect(screen.getByTestId("stage-state")).toHaveTextContent("FAILED");
    expect(screen.getByTestId("stage-state")).toHaveTextContent("clean_single_event");
    expect(screen.getByTestId("stage-rca")).toHaveTextContent("incorrect_otp");
    expect(screen.getByTestId("stage-rca")).toHaveTextContent("0.98");
    expect(screen.getByTestId("stage-policy")).toHaveTextContent("APPROVED");
    expect(screen.getByTestId("stage-action")).toHaveTextContent("RETRY_SOFT");
    expect(screen.getByTestId("verification")).toHaveTextContent("CAPTURED");
  });

  it("shows the model's own recommendation alongside the guard's override", () => {
    const overridden = makeEvent({
      payment_id: "pay_injected",
      outcome: "escalated",
      recommended_action: "ESCALATE_HUMAN",
      original_llm_action: "RETRY_SOFT",
      guard_override_reason:
        "injection_guard: customer note matched instruction-like pattern(s) " +
        "['ignore_previous_instructions']; recommendation overridden from " +
        "RETRY_SOFT to ESCALATE_HUMAN",
      injection_patterns_flagged: ["ignore_previous_instructions"],
    });
    render(<TraceView event={overridden} />);

    const override = screen.getByTestId("guard-override");
    // Both halves, not just the safe outcome: a reader has to be able to see
    // that the model asked for a debit and was refused.
    expect(override).toHaveTextContent("RETRY_SOFT");
    expect(override).toHaveTextContent("ESCALATE_HUMAN");
    expect(override).toHaveTextContent("injection_guard");
    expect(screen.getByTestId("stage-policy")).toHaveTextContent("Model recommended");
  });

  it("shows no override block when the guard did not intervene", () => {
    render(<TraceView event={recovered} />);
    expect(screen.queryByTestId("guard-override")).not.toBeInTheDocument();
  });

  it("renders a policy block as its own distinct stage state, with the reason", () => {
    render(<TraceView event={blocked} />);
    const policy = screen.getByTestId("stage-policy");
    expect(policy.className).toContain("stage--blocked");
    expect(screen.getByTestId("policy-block")).toBeInTheDocument();
    expect(screen.getByTestId("blocked-reason")).toHaveTextContent(
      "exceeds MAX_DISCOUNT of Rs.500",
    );
    expect(policy).toHaveTextContent("MAX_DISCOUNT_EXCEEDED");
  });

  it("shows a blocked event as Action Blocked with nothing executed", () => {
    render(<TraceView event={blocked} />);
    expect(
      screen.getByRole("heading", { level: 3, name: "Action Blocked" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("no-execution")).toHaveTextContent(
      "the payment was never touched",
    );
  });

  it("surfaces the RBI citation carried in a block reason", () => {
    render(<TraceView event={blockedSip} />);
    expect(screen.getByTestId("blocked-reason")).toHaveTextContent(
      "RBI/DPSS/2026-27/396",
    );
  });

  it("shows ambiguity reasons and that the model was not called", () => {
    render(<TraceView event={escalated} />);
    expect(screen.getByTestId("ambiguity-reasons")).toHaveTextContent(
      "no_delivered_events",
    );
    expect(screen.getByTestId("stage-policy")).toHaveTextContent("false");
    expect(screen.getByTestId("stage-rca").className).toContain("stage--warn");
  });

  it("copies the full raw trace as JSON", async () => {
    // userEvent.setup() installs its own clipboard stub, so the spy has to be
    // put in place after it rather than before.
    const user = userEvent.setup();
    const writeText = stubClipboard(vi.fn().mockResolvedValue(undefined));
    render(<TraceView event={blocked} />);
    await user.click(screen.getByTestId("copy-trace-json"));

    expect(writeText).toHaveBeenCalledWith(traceJson(blocked));
    // Round-trips to the same object, so what was copied is the whole trace.
    expect(JSON.parse(writeText.mock.calls[0][0] as string)).toEqual(blocked);
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("reports a clipboard refusal instead of claiming success", async () => {
    const user = userEvent.setup();
    stubClipboard(vi.fn().mockRejectedValue(new Error("denied")));
    render(<TraceView event={blocked} />);
    await user.click(screen.getByTestId("copy-trace-json"));
    expect(await screen.findByTestId("copy-error")).toHaveTextContent("denied");
    expect(screen.queryByText("Copied")).not.toBeInTheDocument();
  });

  it("handles an event that never reached the policy stage", () => {
    render(<TraceView event={noAction} />);
    expect(screen.getByTestId("stage-state")).toHaveTextContent("AUTHORIZED");
    expect(screen.getByTestId("stage-policy").className).toContain("stage--skipped");
  });

  it("does not label a decision made without the model as the model's", () => {
    render(<TraceView event={escalated} />);
    const policy = screen.getByTestId("stage-policy");
    expect(policy).toHaveTextContent("Recommended (no model call)");
    expect(policy).not.toHaveTextContent("Model recommended");
    expect(policy).toHaveTextContent("tracer_ambiguous");
  });

  it("names the model that answered", () => {
    render(<TraceView event={recovered} />);
    const policy = screen.getByTestId("stage-policy");
    expect(policy).toHaveTextContent("Model recommended");
    expect(policy).toHaveTextContent("stub-model");
  });

  it("explains an unexecuted event by the stage that stopped it, not as a block", () => {
    const stopped = makeEvent({
      payment_id: "pay_stopped",
      outcome: "needs_review",
      approved: null,
      final_action: null,
      failed_stage: "observe",
      needs_review_reason: "payment pay_stopped not found",
      execution: null,
      verification: null,
    });
    render(<TraceView event={stopped} />);
    const note = screen.getByTestId("no-execution");
    expect(note).toHaveTextContent("stopped at the observe stage");
    expect(note).not.toHaveTextContent("block");
  });

});

describe("PolicyBlockLog", () => {
  it("shows only blocked events", () => {
    render(<PolicyBlockLog results={sampleResults} onSelect={vi.fn()} />);
    const list = screen.getByTestId("blocklog-list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByTestId("blocklog-row-pay_blocked")).toBeInTheDocument();
    expect(screen.queryByTestId("blocklog-row-pay_recovered")).not.toBeInTheDocument();
  });

  it("shows the rule, the reason and the override for each block", () => {
    render(<PolicyBlockLog results={sampleResults} onSelect={vi.fn()} />);
    const row = screen.getByTestId("blocklog-row-pay_blocked");
    expect(row).toHaveTextContent("MAX_DISCOUNT_EXCEEDED");
    expect(row).toHaveTextContent("exceeds MAX_DISCOUNT of Rs.500");
    expect(row).toHaveTextContent("RETRY_SOFT");
    expect(row).toHaveTextContent("ESCALATE_HUMAN");
  });

  it("filters to a single rule", async () => {
    const user = userEvent.setup();
    render(<PolicyBlockLog results={sampleResults} onSelect={vi.fn()} />);
    await user.click(screen.getByTestId("blocklog-filter-MAX_DISCOUNT_EXCEEDED"));
    expect(within(screen.getByTestId("blocklog-list")).getAllByRole("listitem")).toHaveLength(1);
  });

  it("says so when nothing was blocked", () => {
    render(
      <PolicyBlockLog
        results={{ ...sampleResults, events: [recovered] }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByTestId("blocklog-empty")).toBeInTheDocument();
  });
});

describe("SummaryHeader", () => {
  it("leads with enforcement, the claim the run actually proves", () => {
    render(<SummaryHeader results={sampleResults} sourceDetail="snapshot" />);
    const panels = screen.getByTestId("summary-header").querySelectorAll("section");
    // Enforcement first, money second, rate last.
    expect(panels[0]).toHaveAttribute("data-testid", "enforcement-panel");
    expect(panels[2]).toHaveAttribute("data-testid", "money-panel");
    expect(panels[3]).toHaveAttribute("data-testid", "rate-panel");
  });

  it("states rules fired, actions blocked and unsafe actions executed", () => {
    render(<SummaryHeader results={sampleResults} sourceDetail="snapshot" />);
    expect(screen.getByTestId("enforcement-claim")).toHaveTextContent(
      "2 policy rules fired, 2 actions blocked, 0 unsafe actions executed",
    );
  });

  it("lists every rule that fired with its count", () => {
    render(<SummaryHeader results={sampleResults} sourceDetail="snapshot" />);
    expect(
      screen.getByTestId("rule-row-AFA_SIP_INSURANCE_REQUIRED_AND_MISSING"),
    ).toHaveTextContent("₹1,50,000.00");
    expect(screen.getByTestId("rule-row-MAX_DISCOUNT_EXCEEDED")).toBeInTheDocument();
  });

  it("labels the money panel as verified-safe routing, not recovery", () => {
    render(<SummaryHeader results={sampleResults} sourceDetail="snapshot" />);
    const money = screen.getByTestId("money-panel");
    expect(money).toHaveTextContent("Money moved through verified-safe paths");
    expect(money).toHaveTextContent("Settled via retry");
    expect(money).toHaveTextContent("Found already paid");
    expect(money).toHaveTextContent("Preserved by policy");
    expect(money.textContent).not.toMatch(/recovered by/i);
  });

  it("names the rate Correctly Routed Rate and never Recovery Rate", () => {
    render(<SummaryHeader results={sampleResults} sourceDetail="snapshot" />);
    expect(screen.getByTestId("rate-panel")).toHaveTextContent("Correctly Routed Rate");
    expect(screen.getByTestId("summary-header").textContent).not.toMatch(
      /recovery rate/i,
    );
  });

  it("still shows the raw outcome counts", () => {
    render(<SummaryHeader results={sampleResults} sourceDetail="snapshot" />);
    const counts = screen.getByTestId("counts-panel");
    for (const label of ["Recovered", "Blocked", "Escalated", "Needs review"]) {
      expect(counts).toHaveTextContent(label);
    }
  });

  it("says where the data came from", () => {
    render(<SummaryHeader results={sampleResults} sourceDetail="committed snapshot" />);
    expect(screen.getByTestId("results-source")).toHaveTextContent(
      "6 events · committed snapshot",
    );
  });
});


describe("TraceView: guard, redaction and verification detail", () => {
  it("shows a guard escalation that replaced a status check, with no model answer", () => {
    const flaggedAmbiguous = makeEvent({
      payment_id: "pay_flagged_ambiguous",
      outcome: "escalated",
      ambiguous: true,
      llm_called: false,
      short_circuit_reason: "tracer_ambiguous",
      model: null,
      recommended_action: "ESCALATE_HUMAN",
      final_action: "ESCALATE_HUMAN",
      injection_patterns_flagged: ["ignore_previous_instructions"],
      original_llm_action: null,
      guard_override_reason: "injection_guard: replaced REQUEST_VERIFICATION",
      execution: null,
      verification: null,
    });
    render(<TraceView event={flaggedAmbiguous} />);
    expect(screen.getByTestId("guard-escalation")).toHaveTextContent(
      "injection_guard: replaced REQUEST_VERIFICATION",
    );
    expect(screen.queryByTestId("guard-override")).not.toBeInTheDocument();
  });

  it("names the kinds of personal data redacted, never values", () => {
    render(<TraceView event={makeEvent({ pii_redacted: ["email", "phone"] })} />);
    expect(screen.getByTestId("stage-policy")).toHaveTextContent("email, phone");
  });

  it("shows the retry's idempotency key", () => {
    const retried = makeEvent({
      execution: {
        ...recovered.execution!,
        idempotency_key: "revora-retry:pay_1:1",
      },
    });
    render(<TraceView event={retried} />);
    expect(screen.getByTestId("stage-action")).toHaveTextContent("revora-retry:pay_1:1");
  });

  it("explains a verification that had nothing to re-read", () => {
    const viaStatusQuery = makeEvent({
      verification: {
        performed: false,
        expected_state: null,
        observed_state: null,
        matched: null,
        detail: "REQUEST_VERIFICATION is itself the status query",
      },
    });
    render(<TraceView event={viaStatusQuery} />);
    const verify = screen.getByTestId("verification");
    expect(verify).toHaveTextContent("itself the status query");
    expect(verify).not.toHaveTextContent("expected -");
  });
});

describe("ReviewQueue", () => {
  it("lists every review item joined to its outcome", () => {
    render(<ReviewQueue results={sampleResults} onSelect={vi.fn()} />);
    expect(screen.getByTestId("review-row-pay_escalated")).toHaveTextContent("Escalated");
    expect(screen.getByTestId("review-summary")).toHaveTextContent("1 of 1 item(s)");
  });

  it("filters by outcome and says so when a filter is empty", async () => {
    const user = userEvent.setup();
    render(<ReviewQueue results={sampleResults} onSelect={vi.fn()} />);
    await user.click(screen.getByTestId("review-filter-blocked"));
    expect(screen.getByTestId("review-empty")).toBeInTheDocument();
    await user.click(screen.getByTestId("review-filter-escalated"));
    expect(screen.getByTestId("review-row-pay_escalated")).toBeInTheDocument();
  });

  it("opens the item's full trace", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<ReviewQueue results={sampleResults} onSelect={onSelect} />);
    await user.click(screen.getByTestId("review-row-pay_escalated"));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ payment_id: "pay_escalated" }),
    );
  });

  it("states when no action was decided rather than inventing one", () => {
    const results = {
      ...sampleResults,
      needs_human_review: [
        {
          payment_id: "pay_escalated",
          amount: 99900,
          reason: "payment pay_escalated not found",
          final_action: null,
          root_cause: null,
          blocked_reason: null,
        },
      ],
    };
    render(<ReviewQueue results={results} onSelect={vi.fn()} />);
    expect(screen.getByTestId("review-row-pay_escalated")).toHaveTextContent(
      "none decided",
    );
  });

  it("exports CSV with quoted cells, so commas and quotes survive", () => {
    const results = {
      ...sampleResults,
      needs_human_review: [
        {
          payment_id: "pay_escalated",
          amount: 99900,
          reason: 'status query returned "FAILED", escalated',
          final_action: "REQUEST_VERIFICATION" as const,
          root_cause: null,
          blocked_reason: null,
        },
      ],
    };
    const csv = reviewQueueCsv(queueRows(results));
    const [header, row] = csv.split("\r\n");
    expect(header).toBe(
      '"payment_id","outcome","final_action","amount_rupees","amount_paise","reason","root_cause"',
    );
    expect(row).toBe(
      '"pay_escalated","escalated","REQUEST_VERIFICATION","999.00","99900","status query returned ""FAILED"", escalated",""',
    );
  });
});

describe("TraceView: retry charge", () => {
  it("names the payment charged and the discount taken off", () => {
    const discounted = makeEvent({
      execution: {
        ...recovered.execution!,
        charged_payment_id: "pay_1_attempt1",
        charged_amount: 40000,
        discount_applied: 10000,
      },
    });
    render(<TraceView event={discounted} />);
    const action = screen.getByTestId("stage-action");
    expect(action).toHaveTextContent("pay_1_attempt1");
    expect(action).toHaveTextContent("₹400.00");
    expect(action).toHaveTextContent("after ₹100.00 discount");
  });
});
