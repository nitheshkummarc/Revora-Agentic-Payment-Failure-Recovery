import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import site from "../data/site.json";
import { SCENARIO_TITLES } from "../lib/catalogue";
import { formatPaise, humanizePaise } from "../lib/format";
import { buildTimeline, describeToolResult } from "../lib/timeline";
import type { AgentRun, SiteData } from "../types";

const data = site as unknown as SiteData;
const run = (scenario: string): AgentRun => {
  const found = data.scenarios.find((r) => r.scenario.scenario === scenario);
  if (!found) throw new Error(`no bundled run for ${scenario}`);
  return found;
};

describe("bundled data", () => {
  it("has one run per scenario, each with a human title", () => {
    const names = data.scenarios.map((r) => r.scenario.scenario);
    expect(new Set(names).size).toBe(names.length);
    expect(names).toHaveLength(29);
    for (const name of names) expect(SCENARIO_TITLES[name]).toBeDefined();
  });

  it("matches the committed run's headline figures", () => {
    const m = data.metrics;
    const outcomes = Object.values(m.outcomes).reduce((a, b) => a + b, 0);
    expect(outcomes).toBe(m.total_events);
    expect(m.injection_attempts_that_moved_money).toBe(0);
    expect(data.baseline.revora_charged_twice).toBe(0);
  });
});

describe("timeline", () => {
  it("shows the agent finding a stale success before it retries", () => {
    const steps = buildTimeline(run("verify_mismatch_stale_success"));
    const status = steps.find((s) => s.code === "check_payment_status");
    expect(status?.detail).toContain("CAPTURED");
    expect(steps.some((s) => s.title === "Proposed: Check the status first")).toBe(true);
    expect(steps.some((s) => s.kind === "action" && s.detail.includes("CAPTURED"))).toBe(true);
  });

  it("shows the rule the agent found when a retry would be blocked", () => {
    const steps = buildTimeline(run("amount_over_mandate_ceiling"));
    const policy = steps.find((s) => s.code?.startsWith("check_policy"));
    expect(policy?.detail).toContain("MANDATE_CEILING_EXCEEDED");
  });

  it("explains an ambiguous case without a model call", () => {
    const steps = buildTimeline(run("silent_drop"));
    expect(steps.some((s) => s.title === "The evidence is incomplete")).toBe(true);
    expect(steps.some((s) => s.kind === "proposal")).toBe(false);
  });

  it("stops at 'nothing to recover' for a payment that did not fail", () => {
    const steps = buildTimeline(run("flip"));
    expect(steps.at(-1)?.title).toBe("Nothing to recover");
  });

  it("names the override when the injection guard steps in", () => {
    const steps = buildTimeline(run("injection_note_active_customer"));
    expect(steps.some((s) => s.kind === "guard")).toBe(true);
  });

  it("describes a lookup error instead of crashing", () => {
    expect(describeToolResult({ step: 1, tool: "check_policy", arguments: {}, result: { error: "bad action" } })).toBe(
      "bad action",
    );
  });
});

describe("formatting", () => {
  it("formats paise in Indian grouping", () => {
    expect(formatPaise(200413400)).toBe("₹20,04,134");
    expect(humanizePaise("charged with a 50000 paise discount")).toBe("charged with a ₹500 discount");
  });
});

describe("page", () => {
  it("renders the hero, the console and the results", () => {
    render(<App />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Recover payments.Never charge twice.");
    expect(screen.getByRole("list", { name: "Agent steps" })).toBeInTheDocument();
    expect(screen.getByText(/failed payments, replayed through the pipeline/)).toBeInTheDocument();
  });

  it("switches the run when another scenario is picked", async () => {
    const user = userEvent.setup();
    render(<App />);
    const picker = screen.getByRole("navigation", { name: "Scenarios" });
    await user.click(within(picker).getByRole("button", { name: "Already paid, webhooks say failed" }));
    const steps = screen.getByRole("list", { name: "Agent steps" });
    expect(within(steps).getAllByText(/reports the payment as CAPTURED/).length).toBeGreaterThan(0);
  });

  it("opens the scenario named in the link and keeps the link in sync", async () => {
    window.history.replaceState(null, "", "/?scenario=flip");
    const user = userEvent.setup();
    render(<App />);
    expect(screen.getByRole("heading", { level: 3, name: "Failed, then authorized" })).toBeInTheDocument();
    const picker = screen.getByRole("navigation", { name: "Scenarios" });
    await user.click(within(picker).getByRole("button", { name: "Card declined" }));
    expect(window.location.search).toBe("?scenario=card_declined_clean_delivery");
    window.history.replaceState(null, "", "/");
  });

  it("keeps live mode off when no backend is configured", () => {
    render(<App />);
    expect(screen.getByRole("radio", { name: "Live model" })).toBeDisabled();
  });

  it("uses no em or en dashes in any visible text", () => {
    const { container } = render(<App />);
    expect(container.textContent).not.toMatch(/[–—]/);
  });
});
