/**
 * Read-only access to a completed batch run.
 *
 * `?run=<batch_run_id>` asks the backend for that run, which it serves from
 * disk. Without a run id the committed snapshot is loaded, so the dashboard
 * also works with the backend down. The header shows which source was used.
 *
 * Nothing here writes. There is no POST, PUT or DELETE path to the backend and
 * no browser storage of any kind: a refresh re-fetches from source.
 */

import type { BatchResults } from "../types";

export type ResultsSource = "backend-api" | "snapshot";

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

/** How long to wait for a response before showing an error. */
export const FETCH_TIMEOUT_MS = 15_000;

async function getJson(url: string): Promise<BatchResults> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText} from ${url}`);
    }
    return (await response.json()) as BatchResults;
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error(`no response from ${url} within ${FETCH_TIMEOUT_MS / 1000}s`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function loadBatchResults(
  search: string = typeof window === "undefined" ? "" : window.location.search,
): Promise<LoadedResults> {
  const runId = runIdFromLocation(search);

  if (runId) {
    const results = await getJson(batchResultsUrl(runId));
    return {
      results,
      source: "backend-api",
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
