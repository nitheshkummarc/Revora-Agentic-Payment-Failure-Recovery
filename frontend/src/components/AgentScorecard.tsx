/**
 * How the agent conducted itself on this run.
 *
 * Everything else on the page reports what happened to the payments. This
 * section reports what the agent did about them: when it consulted the model,
 * when it declined to, when the guard had to overrule the answer, and whether
 * the run's own arithmetic still adds up.
 *
 * The provenance line leads on purpose. Every rate below describes the
 * deterministic pipeline around the model, not the model's judgement, and a
 * reader who takes them for the latter has been misled by the layout.
 */

import type { BatchResults } from "../types";
import { formatPercent } from "../metrics";
import { scorecard } from "../scorecard";

export default function AgentScorecard({ results }: { results: BatchResults }) {
  const card = scorecard(results);
  const { modelUse, injection, provenance } = card;

  return (
    <section className="panel" data-testid="agent-scorecard">
      <h2>Agent Scorecard</h2>
      <p className="panel__note">
        Revora evaluates its own agent's behaviour on every run, not just
        whether an action was taken.
      </p>

      <p className="panel__note" data-testid="model-provenance">
        <strong>{provenance.label}.</strong> {provenance.detail}
      </p>

      <div className="tiles">
        <Stat
          label="Override rate"
          value={formatPercent(modelUse.overrideRate)}
          detail={`${modelUse.overridden} of ${modelUse.reachedModel} answers the model gave`}
          testId="override-rate"
          tone={modelUse.overridden > 0 ? "escalated" : undefined}
        />
        <Stat
          label="Ambiguity short-circuit rate"
          value={formatPercent(modelUse.shortCircuitRate)}
          detail={`${modelUse.shortCircuited} of ${card.totalEvents} had evidence too thin to ask`}
          testId="short-circuit-rate"
        />
        <Stat
          label="Never-consulted rate"
          value={formatPercent(modelUse.neverConsultedRate)}
          detail={`${modelUse.neverConsulted} of ${card.totalEvents} needed no recommendation`}
          testId="never-consulted-rate"
        />
        <Stat
          label="Injection attempts"
          value={injection.detected}
          detail={`${injection.movedMoney} moved money`}
          testId="injection-score"
          tone={injection.movedMoney > 0 ? "blocked" : "recovered"}
        />
      </div>

      <h3 className="scorecard__subhead">Reconciliation</h3>
      <p className="panel__note">
        Recomputed from the events on screen each time this renders, so a run
        whose arithmetic stopped adding up would say so here rather than
        reporting a stored pass.
      </p>
      <ul className="checks" data-testid="reconciliation-checks">
        {card.reconciliation.map((check) => (
          <li
            key={check.label}
            className={check.passed ? "check check--pass" : "check check--fail"}
            data-testid={`check-${check.passed ? "pass" : "fail"}`}
          >
            <span className="check__mark">{check.passed ? "PASS" : "FAIL"}</span>
            <span className="check__label">{check.label}</span>
            <span className="check__detail">{check.detail}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Stat({
  label,
  value,
  detail,
  testId,
  tone,
}: {
  label: string;
  value: string | number;
  detail: string;
  testId: string;
  tone?: string;
}) {
  return (
    <div className={`tile${tone ? ` tile--${tone}` : ""}`} data-testid={testId}>
      <span className="tile__value">{value}</span>
      <span className="tile__label">{label}</span>
      <span className="tile__detail">{detail}</span>
    </div>
  );
}
