import type { Baseline, Metrics, Outcome } from "../types";
import { OUTCOME_LABELS } from "../lib/catalogue";
import { formatPaise, formatPercent } from "../lib/format";
import { outcomeTextClass } from "./ui";

const ORDER: Outcome[] = ["recovered", "blocked", "escalated", "needs_review", "no_action"];

export function Results({ metrics, baseline }: { metrics: Metrics; baseline: Baseline }) {
  const total = metrics.total_events;
  const zeroes = [
    { value: metrics.injection_attempts_that_moved_money, label: `of ${metrics.injection_attempts} manipulation attempts moved money` },
    { value: baseline.revora_charged_twice, label: "customers charged twice" },
    { value: baseline.revora.counts.incorrectly_put_at_risk_paise, label: "retries that broke an RBI rule" },
  ];

  const comparison = [
    {
      label: "Customers charged twice",
      revora: String(baseline.revora_charged_twice),
      naive: `${baseline.naive_charged_twice} (${formatPaise(baseline.naive_charged_twice_paise)})`,
    },
    {
      label: "Retries the RBI rules forbid",
      revora: String(baseline.revora.counts.incorrectly_put_at_risk_paise),
      naive: String(baseline.naive.counts.incorrectly_put_at_risk_paise),
    },
    {
      label: "Cases handed to a person",
      revora: String(baseline.revora.counts.escalated_paise),
      naive: "0",
    },
  ];

  return (
    <section id="results" className="border-y border-line bg-surface">
      <div className="mx-auto max-w-[1240px] px-5 py-24 md:px-8 lg:py-32">
        <h2 className="max-w-[22ch] text-[clamp(2rem,3.6vw,3rem)] font-medium leading-[1.08] tracking-[-0.035em]">
          {total} failed payments, replayed through the pipeline.
        </h2>

        <div className="mt-14 grid gap-14 lg:grid-cols-[1.15fr_1fr]">
          <table className="w-full text-left">
            <caption className="sr-only">Outcome of each payment</caption>
            <thead>
              <tr className="text-sm text-faint">
                <th scope="col" className="pb-4 font-normal">Outcome</th>
                <th scope="col" className="pb-4 text-right font-normal">Payments</th>
                <th scope="col" className="pb-4 text-right font-normal">Payment value</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {ORDER.map((outcome) => (
                <tr key={outcome}>
                  <th scope="row" className={`py-4 font-medium ${outcomeTextClass(outcome)}`}>
                    {OUTCOME_LABELS[outcome]}
                  </th>
                  <td className="tabular py-4 text-right">
                    {metrics.outcomes[outcome]}
                    <span className="ml-2 text-sm text-faint">{formatPercent(metrics.outcomes[outcome], total)}</span>
                  </td>
                  <td className="tabular py-4 text-right text-muted">{formatPaise(metrics.outcome_paise[outcome] ?? 0)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={3} className="pt-5 text-sm text-faint">
                  Recovered payments were charged {formatPaise(metrics.money_paise.recovered)}, after{" "}
                  {formatPaise(metrics.money_paise.discount_given)} of approved recovery discounts.
                </td>
              </tr>
            </tfoot>
          </table>

          <div className="flex flex-col justify-between gap-10">
            <ul className="flex flex-col gap-6">
              {zeroes.map((z) => (
                <li key={z.label} className="flex items-baseline gap-5">
                  <span className="tabular w-14 text-5xl font-medium tracking-[-0.04em]">{z.value}</span>
                  <span className="text-muted">{z.label}</span>
                </li>
              ))}
            </ul>
            <p className="text-sm leading-relaxed text-muted">
              {metrics.decided_without_model} payments were decided without asking the model at all, because the
              evidence was incomplete. The safety checks overrode the model {metrics.guard_overrides} times.
            </p>
          </div>
        </div>

        <div className="bezel mt-20">
          <div className="bezel-core overflow-x-auto p-6 sm:p-10">
            <h3 className="text-xl font-medium tracking-[-0.02em]">Against a bot that retries everything</h3>
            <p className="mt-2 max-w-[60ch] text-sm text-muted">
              The same {total} payments, each policy on its own freshly reset gateway, every payment judged by its
              real state before either one acted.
            </p>
            <table className="mt-8 w-full min-w-[480px] text-left">
              <thead>
                <tr className="text-sm text-faint">
                  <th scope="col" className="pb-3 font-normal"><span className="sr-only">Measure</span></th>
                  <th scope="col" className="pb-3 text-right font-normal">Revora</th>
                  <th scope="col" className="pb-3 text-right font-normal">Retry everything</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {comparison.map((row) => (
                  <tr key={row.label}>
                    <th scope="row" className="py-4 font-normal text-muted">{row.label}</th>
                    <td className="tabular py-4 text-right text-lg font-medium">{row.revora}</td>
                    <td className="tabular py-4 text-right text-lg">{row.naive}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <p className="mt-10 max-w-[75ch] text-sm leading-relaxed text-muted">
          Honest scope: this is a simulation. The run uses a scripted model and a mock gateway where a retry always
          succeeds, so it shows that each payment was routed correctly and safely, not how much real money would be
          recovered. That is why the money figure is reported as a Correctly Routed Rate of{" "}
          <span className="tabular text-ink">
            {formatPercent(metrics.money_paise.recovered, metrics.money_paise.addressable)}
          </span>{" "}
          ({formatPaise(metrics.money_paise.recovered)} of {formatPaise(metrics.money_paise.addressable)} at risk), not
          a recovery rate.
        </p>
      </div>
    </section>
  );
}
