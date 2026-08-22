/**
 * End-to-end check against running servers. Not part of the standing suite --
 * it needs both a live backend and a served dashboard, so it is excluded in
 * `vite.config.ts` and run deliberately.
 *
 * What this proves that the offline tests cannot: the dashboard's real fetch
 * path reaches the real endpoint, the payload the backend actually serializes
 * satisfies the component types, and the rendered page shows real figures.
 *
 *   Terminal 1: cd backend && uvicorn app.main:app --port 8000
 *   Terminal 2: cd frontend && npm run build && npx vite preview --port 4173
 *   Terminal 3: cd frontend && npx vitest run --dir src/__live__ --exclude ''
 */

import { describe, expect, it, beforeAll, afterAll, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "../App";
import { loadBatchResults } from "../api/client";
import { moneySummary, enforcementSummary, formatPercent } from "../metrics";
import type { BatchResults } from "../types";

const BACKEND = "http://localhost:8000";
const DASHBOARD = "http://localhost:4173";

/** Relative URLs are what the app issues; resolve them against a real server. */
function absoluteFetch(base: string) {
  const real = globalThis.fetch;
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : String(input);
    return real(url.startsWith("/") ? `${base}${url}` : url, init);
  });
}

let runId = "";

beforeAll(async () => {
  const response = await fetch(`${DASHBOARD}/batch_results.json`);
  if (!response.ok) {
    throw new Error(
      `dashboard not serving the snapshot (${response.status}). Start it first.`,
    );
  }
  runId = ((await response.json()) as BatchResults).batch_run_id;
});

afterAll(() => {
  vi.unstubAllGlobals();
});

describe("live backend", () => {
  it("serves the batch run over the documented endpoint", async () => {
    const response = await fetch(`${BACKEND}/api/batch-results/${runId}`);
    expect(response.status).toBe(200);
    const results = (await response.json()) as BatchResults;
    expect(results.events).toHaveLength(500);
    expect(results.batch_run_id).toBe(runId);
  });

  it("404s an unknown run rather than returning an empty batch", async () => {
    const response = await fetch(`${BACKEND}/api/batch-results/not-a-real-run`);
    expect(response.status).toBe(404);
  });

  it("allows the dashboard's origin to read it", async () => {
    const response = await fetch(`${BACKEND}/api/batch-results/${runId}`, {
      headers: { Origin: DASHBOARD },
    });
    expect(response.headers.get("access-control-allow-origin")).toBeTruthy();
  });
});

describe("dashboard against live data", () => {
  it("loads the run through the app's own client and renders real figures", async () => {
    vi.stubGlobal("fetch", absoluteFetch(BACKEND));

    const loaded = await loadBatchResults(`?run=${runId}`);
    expect(loaded.source).toBe("live-api");
    expect(loaded.results.events).toHaveLength(500);

    render(<App load={() => loadBatchResults(`?run=${runId}`)} />);
    await screen.findByTestId("summary-header");

    const money = moneySummary(loaded.results);
    const enforcement = enforcementSummary(loaded.results);

    // Figures on the page match figures derived from the served payload.
    expect(screen.getByTestId("correctly-routed-rate")).toHaveTextContent(
      formatPercent(money.correctlyRoutedRate),
    );
    expect(screen.getByTestId("enforcement-claim")).toHaveTextContent(
      `${enforcement.rulesFired} policy rules fired`,
    );
    expect(screen.getByTestId("results-source")).toHaveTextContent("500 events");

    // The feed rendered real rows, and a real trace opens.
    const rows = within(screen.getByTestId("event-list")).getAllByRole("listitem");
    expect(rows.length).toBe(500);

    const user = userEvent.setup();
    await user.click(screen.getByTestId("tab-blocks"));
    const blockRows = within(screen.getByTestId("blocklog-list")).getAllByRole(
      "button",
    );
    expect(blockRows.length).toBe(loaded.results.summary.blocked);
    await user.click(blockRows[0]);
    expect(screen.getByTestId("policy-block")).toBeInTheDocument();
    expect(screen.getByTestId("blocked-reason").textContent).toBeTruthy();
  });

  it("serves the built page itself", async () => {
    const response = await fetch(`${DASHBOARD}/`);
    expect(response.status).toBe(200);
    const html = await response.text();
    expect(html).toContain("Revora X-Ray");
    expect(html).toMatch(/<script[^>]+src="\/assets\/index-[^"]+\.js"/);
  });
});
