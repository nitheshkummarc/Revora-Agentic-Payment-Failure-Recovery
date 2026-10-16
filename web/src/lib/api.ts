import type { AgentRun } from "../types";

/** Base URL of the backend, set at build time. Empty means live runs are off. */
export const API_URL = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

/** A free hosting instance sleeps when idle and can take close to a minute to
 * wake, so the timeout is generous. */
const LIVE_TIMEOUT_MS = 90_000;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!API_URL) throw new ApiError("Live runs are not configured for this site.");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), LIVE_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_URL}${path}`, { ...init, signal: controller.signal });
    if (response.status === 429) {
      throw new ApiError("Live runs are limited to a few per visitor. Try again in a few minutes.", 429);
    }
    if (!response.ok) {
      throw new ApiError(`The server answered ${response.status}.`, response.status);
    }
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("The server took too long to answer. It may still be starting; try again.");
    }
    throw new ApiError("Could not reach the server.");
  } finally {
    clearTimeout(timer);
  }
}

export async function liveAvailable(): Promise<boolean> {
  const body = await request<{ live_available: boolean }>("/api/agent/scenarios");
  return body.live_available;
}

export function runLive(paymentId: string): Promise<AgentRun> {
  return request<AgentRun>("/api/agent/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payment_id: paymentId, live: true }),
  });
}
