import { GithubLogo } from "@phosphor-icons/react";
import type { AgentRun } from "../types";
import { ACTION_LABELS } from "../lib/catalogue";
import { formatPaise } from "../lib/format";
import { LinkButton, SOURCE_URL } from "./ui";

/** The case the whole project exists for, taken from a real run: the
 * webhooks say the payment failed, the gateway says it was already captured. */
function EvidenceCard({ run }: { run: AgentRun }) {
  const t = run.trace;
  const status = t.agent_steps.find((s) => s.tool === "check_payment_status");
  const truth = String(status?.result.payment_state ?? "unknown");
  const cause = (t.root_cause?.split("reason: ")[1] ?? "unknown").replace(/_/g, " ").replace(/otp/i, "OTP");
  const decided = t.final_action ? ACTION_LABELS[t.final_action] : "No action";

  return (
    <figure className="bezel w-full max-w-[520px]">
      <div className="bezel-core overflow-hidden">
        <div className="grid grid-cols-2">
          <div className="p-5 sm:p-7">
            <p className="text-sm text-muted">The webhooks say</p>
            <p className="mt-3 text-2xl font-medium tracking-[-0.03em] text-stop sm:text-3xl">{t.resolved_state}</p>
            <p className="mt-2 text-sm text-muted">payment.failed, {cause}</p>
          </div>
          <div className="border-l border-line p-5 sm:p-7">
            <p className="text-sm text-muted">The gateway says</p>
            <p className="mt-3 text-2xl font-medium tracking-[-0.03em] text-ok sm:text-3xl">{truth}</p>
            <p className="mt-2 text-sm text-muted">the capture webhook was lost</p>
          </div>
        </div>
        <div className="border-t border-line bg-accent-wash p-6 sm:p-7">
          <p className="text-sm text-muted">The agent checked before acting</p>
          <p className="mt-2 text-lg font-medium tracking-[-0.01em]">{decided}, not a retry.</p>
          <p className="mt-1 text-sm text-muted">
            Found already paid. {formatPaise(t.amount)} stayed charged once.
          </p>
        </div>
      </div>
      <figcaption className="px-4 pb-2 pt-3 text-xs text-faint">
        A real case from the 500-payment test set, replayed through the pipeline.
      </figcaption>
    </figure>
  );
}

export function Hero({ featured }: { featured: AgentRun }) {
  return (
    <section id="top" className="mx-auto grid min-h-[100dvh] max-w-[1240px] items-center gap-14 px-5 pb-20 pt-28 md:px-8 lg:grid-cols-[1.1fr_1fr] lg:pt-24">
      <div>
        <h1 className="text-[clamp(2.5rem,4.2vw,3.75rem)] font-medium leading-[1.04] tracking-[-0.045em]">
          <span className="block lg:whitespace-nowrap">Recover payments.</span>
          <span className="block lg:whitespace-nowrap">Never charge twice.</span>
        </h1>
        <p className="mt-7 max-w-[44ch] text-lg leading-relaxed text-muted">
          Revora retries a failed payment only after checking its real state and India's mandate rules. Unclear
          cases go to a person.
        </p>
        <div className="mt-10 flex flex-wrap items-center gap-3">
          <LinkButton href="#agent">Run the agent</LinkButton>
          <a
            href={SOURCE_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-2 rounded-full px-4 py-3 text-sm font-medium text-muted transition-colors duration-300 hover:text-ink"
          >
            <GithubLogo size={16} weight="light" aria-hidden />
            Source
          </a>
        </div>
      </div>
      <div className="flex justify-center lg:justify-end">
        <EvidenceCard run={featured} />
      </div>
    </section>
  );
}
