/**
 * Read-only access to a completed batch run.
 *
 * Two sources, and the dashboard says which one it used rather than hiding the
 * difference. A `?run=<batch_run_id>` in the URL fetches that run live from the
 * backend. With no run id there is nothing to ask the backend for -- the
 * endpoint is addressed by run id and there is no "latest" -- so the committed
 * snapshot is loaded instead. That is also what makes the dashboard runnable
 * with the backend down.
 *
 * Nothing here writes. There is no POST, PUT or DELETE path to the backend and
 * no browser storage of any kind: a refresh re-fetches from source.
 */

import type { BatchResults } from "../types";

export type ResultsSource = "live-api" | "snapshot";

export interface LoadedResults {
  results: BatchResults;
  source: ResultsSource;
  /** Where the data actually came from, shown in the header. */
  detail: string;
}

export const SNAPSHOT_URL = "/batch_results.json";

export function batchResultsUrl(runId: string): string {
  return `/api/batch-results/${encodeURIComponent(runId)}`;
}

/** The run id named in the page URL, if any. */
export function runIdFromLocation(search: string): string | null {
  const value = new URLSearchParams(search).get("run");
  return value && value.trim() ? value.trim() : null;
}

async function getJson(url: string): Promise<BatchResults> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText} from ${url}`);
  }
  return (await response.json()) as BatchResults;
}

export async function loadBatchResults(
  search: string = typeof window === "undefined" ? "" : window.location.search,
): Promise<LoadedResults> {
  const runId = runIdFromLocation(search);

  if (runId) {
    const results = await getJson(batchResultsUrl(runId));
    return {
      results,
      source: "live-api",
      detail: `GET /api/batch-results/${runId}`,
    };
  }

  const results = await getJson(SNAPSHOT_URL);
  return {
    results,
    source: "snapshot",
    detail: `committed snapshot (run ${results.batch_run_id})`,
  };
}
