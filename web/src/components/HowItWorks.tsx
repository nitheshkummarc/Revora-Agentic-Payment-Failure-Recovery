const STAGES = [
  {
    question: "What state is the payment really in?",
    who: "Code",
    detail:
      "Rebuilds the state from the webhooks that arrived, ordered by when events happened, with duplicates removed. Silence past five minutes counts as uncertainty.",
  },
  {
    question: "Why did it fail?",
    who: "Code",
    detail:
      "Quotes the cause straight from Razorpay's error object. If evidence is missing, it says so instead of guessing, and the model is not asked.",
  },
  {
    question: "What should we do?",
    who: "Model",
    detail:
      "The agent looks up the real status, the mandate facts, the retry history and what the rules would allow, then proposes one of four actions. It has no tool that moves money.",
  },
  {
    question: "Are we allowed to?",
    who: "Rules",
    detail:
      "The RBI rule engine has the final say: opt-out, missing compliance facts, 24-hour notice, mandate ceiling, extra authentication, retry and discount limits.",
  },
  {
    question: "Did it actually work?",
    who: "Code",
    detail:
      "Checks the gateway before writing and reads the charged payment back after. Nothing counts as recovered until it is confirmed captured.",
  },
];

const GUARDRAILS = [
  "Customer notes are cleaned, stripped of personal data and treated as untrusted text.",
  "A note that looks like an instruction always goes to a person.",
  "If removing the note changes the model's answer, a person decides.",
  "Every retry is a new payment attempt under an idempotency key, so a repeated request cannot charge twice.",
  "Any failure, timeout or invalid answer escalates. Nothing proceeds on a guess.",
];

export function HowItWorks() {
  return (
    <section id="how" className="mx-auto max-w-[1240px] px-5 py-24 md:px-8 lg:py-32">
      <h2 className="max-w-[22ch] text-[clamp(2rem,3.6vw,3rem)] font-medium leading-[1.08] tracking-[-0.035em]">
        The model answers one question. Code answers the rest.
      </h2>

      <ol className="mt-14 flex flex-col">
        {STAGES.map((stage) => (
          <li key={stage.question} className="grid gap-3 border-t border-line py-8 md:grid-cols-[1fr_120px_1.4fr] md:gap-10">
            <p className="text-xl font-medium tracking-[-0.02em]">{stage.question}</p>
            <p>
              <span
                className={
                  "inline-flex rounded-full px-3 py-1 text-xs font-medium " +
                  (stage.who === "Model" ? "bg-accent text-accent-ink" : "bg-surface text-muted shadow-[inset_0_0_0_1px_var(--line)]")
                }
              >
                {stage.who}
              </span>
            </p>
            <p className="max-w-[60ch] leading-relaxed text-muted">{stage.detail}</p>
          </li>
        ))}
      </ol>

      <div className="mt-16 grid gap-10 lg:grid-cols-[1fr_1.4fr]">
        <h3 className="text-2xl font-medium tracking-[-0.025em]">Guardrails around the model</h3>
        <ul className="flex flex-col gap-4">
          {GUARDRAILS.map((g) => (
            <li key={g} className="leading-relaxed text-muted">
              {g}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
