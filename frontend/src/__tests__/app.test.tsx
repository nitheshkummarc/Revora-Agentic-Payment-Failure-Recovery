/**
 * The page as a whole: loading, failure, navigating between the two views, and
 * the do-not-build constraints, which are worth pinning as tests because they
 * are easy to reintroduce by accident.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "../App";
import {
  batchResultsUrl,
  loadBatchResults,
  runIdFromLocation,
  SNAPSHOT_URL,
} from "../api/client";
import { sampleResults } from "./fixtures";

function loader() {
  return vi.fn().mockResolvedValue({
    results: sampleResults,
    source: "snapshot" as const,
    detail: "committed snapshot (run run-1)",
  });
}

describe("App", () => {
  it("shows a loading state until results arrive", async () => {
    render(<App load={() => new Promise(() => {})} />);
    expect(screen.getByTestId("loading")).toBeInTheDocument();
  });

  it("renders the summary and feed once loaded", async () => {
    render(<App load={loader()} />);
    expect(await screen.findByTestId("summary-header")).toBeInTheDocument();
    expect(screen.getByTestId("event-feed")).toBeInTheDocument();
  });

  it("reports a load failure instead of rendering an empty dashboard", async () => {
    render(<App load={() => Promise.reject(new Error("404 from /api"))} />);
    expect(await screen.findByTestId("load-error")).toHaveTextContent("404 from /api");
    expect(screen.queryByTestId("event-feed")).not.toBeInTheDocument();
  });

  it("shows a selected event's trace beside the feed", async () => {
    const user = userEvent.setup();
    render(<App load={loader()} />);
    await screen.findByTestId("event-feed");

    expect(screen.getByTestId("trace-empty")).toBeInTheDocument();
    await user.click(screen.getByTestId("event-row-pay_blocked"));

    const trace = screen.getByTestId("trace-view");
    expect(within(trace).getByText("pay_blocked")).toBeInTheDocument();
    expect(screen.getByTestId("policy-block")).toBeInTheDocument();
  });

  it("switches to the dedicated policy block log and back", async () => {
    const user = userEvent.setup();
    render(<App load={loader()} />);
    await screen.findByTestId("event-feed");

    await user.click(screen.getByTestId("tab-blocks"));
    expect(screen.getByTestId("policy-block-log")).toBeInTheDocument();
    expect(screen.queryByTestId("event-feed")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("tab-feed"));
    expect(screen.getByTestId("event-feed")).toBeInTheDocument();
  });

  it("opens a trace from the block log too", async () => {
    const user = userEvent.setup();
    render(<App load={loader()} />);
    await screen.findByTestId("event-feed");
    await user.click(screen.getByTestId("tab-blocks"));
    await user.click(screen.getByTestId("blocklog-row-pay_blocked_sip"));
    expect(
      within(screen.getByTestId("trace-view")).getByText("pay_blocked_sip"),
    ).toBeInTheDocument();
  });
});

describe("api client", () => {
  it("reads the run id from the page URL", () => {
    expect(runIdFromLocation("?run=abc-123")).toBe("abc-123");
    expect(runIdFromLocation("?run=")).toBeNull();
    expect(runIdFromLocation("")).toBeNull();
  });

  it("addresses the documented endpoint when a run id is given", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => sampleResults,
    });
    vi.stubGlobal("fetch", fetchMock);

    const loaded = await loadBatchResults("?run=run-1");
    expect(fetchMock).toHaveBeenCalledWith("/api/batch-results/run-1");
    expect(loaded.source).toBe("live-api");
    vi.unstubAllGlobals();
  });

  it("escapes a run id rather than pasting it into the path", () => {
    expect(batchResultsUrl("a b/c")).toBe("/api/batch-results/a%20b%2Fc");
  });

  it("falls back to the committed snapshot when no run is named", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => sampleResults,
    });
    vi.stubGlobal("fetch", fetchMock);

    const loaded = await loadBatchResults("");
    expect(fetchMock).toHaveBeenCalledWith(SNAPSHOT_URL);
    expect(loaded.source).toBe("snapshot");
    vi.unstubAllGlobals();
  });

  it("raises on a non-ok response rather than returning empty results", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404, statusText: "Not Found" }),
    );
    await expect(loadBatchResults("?run=missing")).rejects.toThrow("404");
    vi.unstubAllGlobals();
  });
});

describe("do-not-build constraints", () => {
  it("never touches browser storage", async () => {
    const localSet = vi.spyOn(Storage.prototype, "setItem");
    const localGet = vi.spyOn(Storage.prototype, "getItem");

    const user = userEvent.setup();
    render(<App load={loader()} />);
    await screen.findByTestId("event-feed");
    await user.click(screen.getByTestId("event-row-pay_blocked"));
    await user.click(screen.getByTestId("tab-blocks"));

    expect(localSet).not.toHaveBeenCalled();
    expect(localGet).not.toHaveBeenCalled();
    localSet.mockRestore();
    localGet.mockRestore();
  });

  it("only ever issues GET requests, and none that write", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => sampleResults,
    });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    render(<App />);
    await screen.findByTestId("event-feed");
    await user.click(screen.getByTestId("event-row-pay_blocked"));

    for (const call of fetchMock.mock.calls) {
      const init = call[1] as RequestInit | undefined;
      // No init at all is a GET; anything else must not carry a body or a
      // mutating method.
      expect(init?.method ?? "GET").toBe("GET");
      expect(init?.body).toBeUndefined();
    }
    vi.unstubAllGlobals();
  });

  it("waits for results without rendering a partial dashboard", async () => {
    let resolve: (value: unknown) => void = () => {};
    const pending = new Promise((r) => {
      resolve = r;
    });
    render(<App load={() => pending as never} />);

    expect(screen.queryByTestId("summary-header")).not.toBeInTheDocument();
    resolve({
      results: sampleResults,
      source: "snapshot",
      detail: "committed snapshot",
    });
    await waitFor(() => expect(screen.getByTestId("summary-header")).toBeInTheDocument());
  });
});
