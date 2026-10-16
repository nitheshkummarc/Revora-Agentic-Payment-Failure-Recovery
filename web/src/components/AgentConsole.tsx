import {
  ArrowClockwise,
  CheckCircle,
  Eye,
  Gavel,
  HandPalm,
  Lightning,
  MagnifyingGlass,
  ShieldWarning,
  Sparkle,
  Wrench,
  XCircle,
} from "@phosphor-icons/react";
import { type CSSProperties, useEffect, useMemo, useState } from "react";
import type { AgentRun } from "../types";
import { API_URL, ApiError, runLive } from "../lib/api";
import { BUCKETS, OUTCOME_LABELS, SCENARIO_TITLES, scenarioTitle } from "../lib/catalogue";
import { formatPaise } from "../lib/format";
import { buildTimeline, type StepKind, type TimelineStep, type Tone } from "../lib/timeline";
import { ActionButton, OutcomeMark, outcomeTextClass } from "./ui";

const KIND_ICON: Record<StepKind, typeof Eye> = {
  evidence: Eye,
  cause: MagnifyingGlass,
  tool: Wrench,
  proposal: Sparkle,
  guard: ShieldWarning,
  rules: Gavel,
  action: HandPalm,
  verify: CheckCircle,
};

const TONE_CLASS: Record<Tone, string> = {
  neutral: "text-muted",
  good: "text-ok",
  warn: "text-person",
  stop: "text-stop",
};

function Step({ step, index }: { step: TimelineStep; index: number }) {
  const Icon = step.tone === "stop" ? XCircle : KIND_ICON[step.kind];
  return (
    <li className="step-in grid grid-cols-[32px_1fr] gap-4" style={{ "--i": index } as CSSProperties}>
      <span
        className={`mt-0.5 flex h-8 w-8 items-center justify-center rounded-full bg-surface shadow-[inset_0_0_0_1px_var(--line)] ${TONE_CLASS[step.tone]}`}
      >
        <Icon size={16} weight="light" aria-hidden />
      </span>
      <div className="min-w-0 pb-6">
        <p className="font-medium tracking-[-0.01em]">{step.title}</p>
        {step.code ? (
          <p translate="no" className="mt-1 break-words font-mono text-xs text-faint">{step.code}</p>
        ) : null}
        {step.detail ? <p className="mt-1.5 max-w-[68ch] text-sm leading-relaxed text-muted">{step.detail}</p> : null}
      </div>
    </li>
  );
}

function ScenarioPicker({
  runs,
  selected,
  onSelect,
}: {
  runs: AgentRun[];
  selected: string;
  onSelect: (paymentId: string) => void;
}) {
  return (
    <nav aria-label="Scenarios" className="hidden flex-col gap-7 lg:flex">
      {BUCKETS.map((bucket) => {
        const items = runs.filter((r) => r.scenario.bucket === bucket.id);
        if (items.length === 0) return null;
        return (
          <div key={bucket.id}>
            <p className="px-3 text-sm font-medium text-faint">{bucket.title}</p>
            <ul className="mt-2 flex flex-col gap-0.5">
              {items.map((run) => {
                const active = run.scenario.payment_id === selected;
                return (
                  <li key={run.scenario.payment_id}>
                    <button
                      type="button"
                      onClick={() => onSelect(run.scenario.payment_id)}
                      aria-pressed={active}
                      className={
                        "w-full rounded-2xl px-3 py-2 text-left text-sm transition-colors duration-300 " +
                        (active ? "bg-accent-wash text-ink" : "text-muted hover:bg-surface hover:text-ink")
                      }
                    >
                      {scenarioTitle(run.scenario.scenario)}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}

/** On small screens a native select replaces the long list. */
function ScenarioSelect({
  runs,
  selected,
  onSelect,
}: {
  runs: AgentRun[];
  selected: string;
  onSelect: (paymentId: string) => void;
}) {
  return (
    <div className="flex flex-col gap-2 lg:hidden">
      <label htmlFor="scenario-select" className="text-sm font-medium text-faint">
        Scenario
      </label>
      <select
        id="scenario-select"
        value={selected}
        onChange={(e) => onSelect(e.target.value)}
        className="w-full rounded-full bg-surface px-4 py-3 text-sm text-ink shadow-[inset_0_0_0_1px_var(--line-strong)]"
      >
        {BUCKETS.map((bucket) => (
          <optgroup key={bucket.id} label={bucket.title}>
            {runs
              .filter((r) => r.scenario.bucket === bucket.id)
              .map((run) => (
                <option key={run.scenario.payment_id} value={run.scenario.payment_id}>
                  {scenarioTitle(run.scenario.scenario)}
                </option>
              ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}

type Mode = "recorded" | "live";

function ModeSwitch({ mode, onChange }: { mode: Mode; onChange: (mode: Mode) => void }) {
  const liveDisabled = !API_URL;
  const option = (value: Mode, label: string, disabled = false) => (
    <button
      type="button"
      role="radio"
      aria-checked={mode === value}
      disabled={disabled}
      onClick={() => onChange(value)}
      title={disabled ? "Live runs need the backend URL configured" : undefined}
      className={
        "rounded-full px-4 py-2 text-sm transition-colors duration-300 disabled:cursor-not-allowed disabled:opacity-40 " +
        (mode === value ? "bg-core text-ink shadow-[inset_0_0_0_1px_var(--line)]" : "text-muted hover:text-ink")
      }
    >
      {label}
    </button>
  );
  return (
    <div role="radiogroup" aria-label="Run mode" className="inline-flex rounded-full bg-surface p-1">
      {option("recorded", "Recorded run")}
      {option("live", "Live model", liveDisabled)}
    </div>
  );
}

function RunSkeleton({ waking }: { waking: boolean }) {
  return (
    <div aria-live="polite" className="flex flex-col gap-5">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="grid grid-cols-[32px_1fr] gap-4">
          <span className="h-8 w-8 animate-pulse rounded-full bg-surface" />
          <div className="flex flex-col gap-2 pt-1">
            <span className="h-3 w-2/5 animate-pulse rounded-full bg-surface" />
            <span className="h-3 w-4/5 animate-pulse rounded-full bg-surface" />
          </div>
        </div>
      ))}
      <p className="text-sm text-muted">
        {waking
          ? "The free server sleeps when idle and is waking up. This can take up to a minute."
          : "Investigating this payment…"}
      </p>
    </div>
  );
}

/** The scenario named in `?scenario=`, so a case can be linked directly. */
function scenarioFromUrl(runs: AgentRun[]): string | undefined {
  const wanted = new URLSearchParams(window.location.search).get("scenario");
  return runs.find((r) => r.scenario.scenario === wanted)?.scenario.payment_id;
}

export function AgentConsole({ runs }: { runs: AgentRun[] }) {
  const [selected, setSelected] = useState(() => scenarioFromUrl(runs) ?? runs[0]?.scenario.payment_id ?? "");
  const [mode, setMode] = useState<Mode>("recorded");
  const [live, setLive] = useState<Record<string, AgentRun>>({});
  const [pending, setPending] = useState(false);
  const [waking, setWaking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runKey, setRunKey] = useState(0);

  const recorded = runs.find((r) => r.scenario.payment_id === selected) ?? runs[0];
  const shown = mode === "live" ? live[selected] : recorded;
  const timeline = useMemo(() => (shown ? buildTimeline(shown) : []), [shown]);
  const info = SCENARIO_TITLES[recorded.scenario.scenario];

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get("scenario") === recorded.scenario.scenario) return;
    url.searchParams.set("scenario", recorded.scenario.scenario);
    window.history.replaceState(null, "", url);
  }, [recorded.scenario.scenario]);

  async function runLiveNow() {
    setPending(true);
    setError(null);
    const wakeTimer = setTimeout(() => setWaking(true), 4000);
    try {
      const result = await runLive(selected);
      setLive((prev) => ({ ...prev, [selected]: result }));
      setRunKey((k) => k + 1);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong.");
    } finally {
      clearTimeout(wakeTimer);
      setWaking(false);
      setPending(false);
    }
  }

  function select(paymentId: string) {
    setSelected(paymentId);
    setError(null);
    setRunKey((k) => k + 1);
  }

  return (
    <section id="agent" className="mx-auto max-w-[1240px] px-5 py-24 md:px-8 lg:py-32">
      <h2 className="max-w-[20ch] text-[clamp(2rem,3.6vw,3rem)] font-medium leading-[1.08] tracking-[-0.035em]">
        Pick a failed payment. Watch the agent decide.
      </h2>
      <p className="mt-5 max-w-[60ch] text-lg leading-relaxed text-muted">
        The agent can look things up, but it cannot move money. Its proposal still has to pass the safety
        checks and the RBI rules before anything happens.
      </p>

      <div className="bezel mt-12">
        <div className="bezel-core grid lg:grid-cols-[300px_1fr]">
          <div className="border-b border-line p-4 lg:max-h-[760px] lg:overflow-y-auto lg:border-b-0 lg:border-r lg:p-5">
            <ScenarioPicker runs={runs} selected={selected} onSelect={select} />
            <ScenarioSelect runs={runs} selected={selected} onSelect={select} />
          </div>

          <div className="min-w-0 p-6 sm:p-8 lg:p-10">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0">
                <h3 className="text-2xl font-medium tracking-[-0.025em]">{scenarioTitle(recorded.scenario.scenario)}</h3>
                <p className="mt-1.5 text-muted">{info?.blurb}</p>
                <p className="mt-3 text-sm text-faint">
                  <span className="tabular">{formatPaise(recorded.scenario.amount)}</span> payment
                </p>
              </div>
              <ModeSwitch
                mode={mode}
                onChange={(next) => {
                  setMode(next);
                  setError(null);
                  setRunKey((k) => k + 1);
                }}
              />
            </div>

            {recorded.scenario.customer_note ? (
              <blockquote className="mt-6 rounded-2xl bg-surface px-5 py-4 text-sm">
                <p className="text-faint">Customer note, treated as untrusted data</p>
                <p className="mt-1 break-words">“{recorded.scenario.customer_note}”</p>
              </blockquote>
            ) : null}

            {mode === "live" ? (
              <div className="mt-6 flex flex-wrap items-center gap-4">
                <ActionButton
                  onClick={runLiveNow}
                  disabled={pending}
                  icon={pending ? <ArrowClockwise size={15} weight="light" className="animate-spin" aria-hidden /> : <Lightning size={15} weight="light" aria-hidden />}
                >
                  {live[selected] ? "Run again" : "Run with the live model"}
                </ActionButton>
                <p className="max-w-[44ch] text-sm text-muted">
                  Calls a real language model through the deployed backend. Limited to a few runs per visitor.
                </p>
              </div>
            ) : null}

            {error ? (
              <p role="alert" className="mt-6 rounded-2xl bg-surface px-5 py-4 text-sm text-stop">
                {error}
              </p>
            ) : null}

            <div className="mt-10">
              {pending ? (
                <RunSkeleton waking={waking} />
              ) : shown ? (
                <>
                  <ol key={`${selected}-${mode}-${runKey}`} aria-label="Agent steps">
                    {timeline.map((step, i) => (
                      <Step key={`${step.kind}-${i}`} step={step} index={i} />
                    ))}
                  </ol>
                  <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3 border-t border-line pt-6">
                    <span className="text-sm text-muted">Outcome</span>
                    <OutcomeMark outcome={shown.trace.outcome} />
                    {shown.single_call_outcome ? (
                      <span className="text-sm text-muted">
                        Single-call pipeline:{" "}
                        <span className={outcomeTextClass(shown.single_call_outcome)}>
                          {OUTCOME_LABELS[shown.single_call_outcome]}
                        </span>
                      </span>
                    ) : null}
                    <span className="text-sm text-faint">
                      {shown.mode === "live" ? `Live model: ${shown.trace.model ?? "none answered"}` : "Recorded with the scripted agent"}
                    </span>
                  </div>
                </>
              ) : (
                <p className="text-muted">Run the live model to see its steps for this payment.</p>
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
