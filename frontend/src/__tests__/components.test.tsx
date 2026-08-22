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
import SummaryHeader from "../components/SummaryHeader";
import TraceView, { traceJson } from "../components/TraceView";
import { blocked, blockedSip, escalated, noAction, recovered, sampleResults } from "./fixtures";

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
