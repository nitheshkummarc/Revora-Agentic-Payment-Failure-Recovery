import type { AgentRun, AgentStep } from "../types";
import { ACTION_LABELS, RESOLUTION_LABELS, TOOL_LABELS } from "./catalogue";
import { formatPaise, humanizePaise } from "./format";

export type StepKind =
  | "evidence"
  | "cause"
  | "tool"
  | "proposal"
  | "guard"
  | "rules"
  | "action"
  | "verify";

export type Tone = "neutral" | "good" | "warn" | "stop";

export interface TimelineStep {
  kind: StepKind;
  title: string;
  detail: string;
  tone: Tone;
  /** Literal identifiers shown in monospace, e.g. a tool name or API call. */
  code?: string;
}

function yesNo(value: unknown): string {
  if (value === true) return "yes";
  if (value === false) return "no";
  return "not on file";
}

/** One line describing what a lookup returned, in plain words. */
export function describeToolResult(step: AgentStep): string {
  const r = step.result;
  if ("error" in r) return String(r.error);
  switch (step.tool) {
    case "check_payment_status": {
      const attempts = (r.retry_attempts as { payment_id: string; state: string }[] | undefined) ?? [];
      const base = `The gateway reports the payment as ${String(r.payment_state)}.`;
      if (attempts.length === 0) return `${base} No earlier retry attempts.`;
      return `${base} Earlier attempts: ${attempts.map((a) => `${a.payment_id} ${a.state}`).join(", ")}.`;
    }
    case "get_mandate_facts": {
      const amount = typeof r.amount_paise === "number" ? formatPaise(r.amount_paise) : "unknown";
      const ceiling =
        typeof r.mandate_ceiling_paise === "number" ? formatPaise(r.mandate_ceiling_paise) : "not on file";
      const notice =
        typeof r.hours_since_pre_debit_notice === "number"
          ? `${r.hours_since_pre_debit_notice} hours ago`
          : "not on file";
      return `${amount} against a ${ceiling} ceiling. Pre-debit notice: ${notice}. Extra authentication: ${yesNo(
        r.afa_flag,
      )}. Opted out: ${yesNo(r.opted_out)}.`;
    }
    case "get_retry_history":
      return `${String(r.attempts_counted_by_policy)} of ${String(r.max_retries)} recovery attempts used.`;
    case "check_policy": {
      const action = ACTION_LABELS[r.action as keyof typeof ACTION_LABELS] ?? String(r.action);
      if (r.approved) return `“${action}” would be allowed.`;
      return `“${action}” would be blocked by ${String(r.rule_id)}. ${String(r.blocked_reason ?? "")}`.trim();
    }
    case "rejected_tool_call":
      return "The provider rejected the call, so the agent moved on to its proposal.";
    default:
      return JSON.stringify(r);
  }
}

function stripGuardPrefix(reason: string): string {
  return reason.replace(/^(injection_guard|note_influence_guard|grounding_guard):\s*/, "");
}

function guardTitle(reason: string): string {
  if (reason.startsWith("injection_guard")) return "The note looked like an instruction, so a person decides";
  if (reason.startsWith("note_influence_guard")) return "The note changed the model's answer, so a person decides";
  if (reason.startsWith("grounding_guard")) return "No error behind the proposal, so a person decides";
  return "A safety check overrode the proposal";
}

/** The steps a reader sees for one run, in the order they happened. */
export function buildTimeline(run: AgentRun): TimelineStep[] {
  const t = run.trace;
  const steps: TimelineStep[] = [];

  if (t.resolved_state) {
    const how = t.resolution_reason ? RESOLUTION_LABELS[t.resolution_reason] ?? t.resolution_reason : "the webhooks";
    steps.push({
      kind: "evidence",
      title: "Read the webhooks that arrived",
      detail: `They resolve to ${t.resolved_state}, from ${how}.`,
      tone: "neutral",
    });
  }

  if (t.recommended_action === null && t.outcome === "no_action") {
    steps.push({
      kind: "action",
      title: "Nothing to recover",
      detail: t.execution?.detail ?? "The payment did not fail.",
      tone: "good",
    });
    return steps;
  }

  if (t.ambiguous) {
    steps.push({
      kind: "cause",
      title: "The evidence is incomplete",
      detail: "The model is not asked to guess when evidence is missing. The real status is checked instead.",
      tone: "warn",
    });
  } else if (t.root_cause) {
    steps.push({
      kind: "cause",
      title: "Quoted the cause from Razorpay's error",
      detail: t.root_cause,
      tone: "neutral",
    });
  }

  for (const step of t.agent_steps) {
    if (step.tool === "propose_action") continue;
    steps.push({
      kind: "tool",
      title: TOOL_LABELS[step.tool] ?? step.tool,
      detail: describeToolResult(step),
      tone: step.tool === "rejected_tool_call" ? "warn" : "neutral",
      code: step.tool === "check_policy" ? `check_policy(${String(step.arguments.action)})` : step.tool,
    });
  }

  if (t.llm_called) {
    const proposed = t.original_llm_action ?? t.recommended_action;
    steps.push({
      kind: "proposal",
      title: `Proposed: ${proposed ? ACTION_LABELS[proposed] : "no action"}`,
      detail: t.reasoning ?? "",
      tone: "neutral",
      code: t.model ?? undefined,
    });
  } else if (t.short_circuit_reason && t.short_circuit_reason !== "tracer_ambiguous") {
    steps.push({
      kind: "proposal",
      title: "No model answer, so a person decides",
      detail: `Reason: ${t.short_circuit_reason.replace(/_/g, " ")}.`,
      tone: "warn",
    });
  }

  if (t.guard_override_reason) {
    steps.push({
      kind: "guard",
      title: guardTitle(t.guard_override_reason),
      detail: stripGuardPrefix(t.guard_override_reason),
      tone: "warn",
    });
  }

  if (t.approved !== null && t.final_action) {
    steps.push(
      t.approved
        ? {
            kind: "rules",
            title: "The RBI rules allowed it",
            detail: `Approved: ${ACTION_LABELS[t.final_action]}.`,
            tone: "good",
          }
        : {
            kind: "rules",
            title: `Blocked by ${t.rule_id ?? "a rule"}`,
            detail: t.blocked_reason ?? "",
            tone: "stop",
            code: t.rule_id ?? undefined,
          },
    );
  }

  if (t.execution && t.approved) {
    steps.push({
      kind: "action",
      title: t.execution.gateway_called ? "Acted at the gateway" : "Recorded the decision",
      detail: humanizePaise(t.execution.detail),
      tone: t.execution.succeeded === false ? "warn" : "neutral",
      code: t.execution.calls.length ? t.execution.calls.join("  ") : undefined,
    });
  }

  if (t.verification?.performed) {
    steps.push({
      kind: "verify",
      title: t.verification.matched ? "Verified at the gateway" : "Verification failed",
      detail: t.verification.detail,
      tone: t.verification.matched ? "good" : "stop",
    });
  }

  return steps;
}
