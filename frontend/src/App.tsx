/**
 * Single page: summary header, then either the event feed or the policy block
 * log, with the selected event's trace beside it.
 *
 * Tab state is component state. No router, no storage, no write path back to
 * the backend -- a refresh re-fetches and starts clean, which is correct for a
 * read-only view of a completed run.
 */

import { useEffect, useState } from "react";
import AgentScorecard from "./components/AgentScorecard";
import EventFeed from "./components/EventFeed";
import PolicyBlockLog from "./components/PolicyBlockLog";
import SummaryHeader from "./components/SummaryHeader";
import TraceView from "./components/TraceView";
import { loadBatchResults, type LoadedResults } from "./api/client";
import type { EventTrace } from "./types";

type Tab = "feed" | "blocks";

interface Props {
  /** Injected by tests; production uses the real loader. */
  load?: typeof loadBatchResults;
}

export default function App({ load = loadBatchResults }: Props) {
  const [loaded, setLoaded] = useState<LoadedResults | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<EventTrace | null>(null);
  const [tab, setTab] = useState<Tab>("feed");

  useEffect(() => {
    let cancelled = false;
    load()
      .then((result) => {
        if (!cancelled) setLoaded(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [load]);

  if (error) {
    return (
      <main className="app app--message">
        <h1>Revora X-Ray</h1>
        <p className="error" data-testid="load-error">
          Could not load batch results: {error}
        </p>
      </main>
    );
  }

  if (!loaded) {
    return (
      <main className="app app--message">
        <h1>Revora X-Ray</h1>
        <p data-testid="loading">Loading batch results…</p>
      </main>
    );
  }

  return (
    <main className="app">
      <SummaryHeader results={loaded.results} sourceDetail={loaded.detail} />

      <AgentScorecard results={loaded.results} />

      <nav className="tabs" role="tablist" aria-label="Views">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "feed"}
          className={tab === "feed" ? "tab tab--on" : "tab"}
          onClick={() => setTab("feed")}
          data-testid="tab-feed"
        >
          Event feed
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "blocks"}
          className={tab === "blocks" ? "tab tab--on" : "tab"}
          onClick={() => setTab("blocks")}
          data-testid="tab-blocks"
        >
          Policy block log
        </button>
      </nav>

      <div className="columns">
        <div className="columns__left">
          {tab === "feed" ? (
            <EventFeed
              results={loaded.results}
              selectedPaymentId={selected?.payment_id ?? null}
              onSelect={setSelected}
            />
          ) : (
            <PolicyBlockLog results={loaded.results} onSelect={setSelected} />
          )}
        </div>
        <div className="columns__right">
          <TraceView event={selected} />
        </div>
      </div>
    </main>
  );
}
