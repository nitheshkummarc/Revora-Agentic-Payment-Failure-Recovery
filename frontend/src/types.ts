/**
 * Shapes mirrored from the orchestrator's batch output.
 *
 * These are read-only projections. The dashboard never constructs one to send
 * back -- it is observability over a completed run, not a control panel.
 */

export type Outcome =
  | "recovered"
  | "blocked"
  | "escalated"
  | "needs_review"
  | "no_action";

export type Action =
  | "RETRY_SOFT"
  | "REQUEST_VERIFICATION"
  | "ESCALATE_HUMAN"
  | "NO_ACTION_COOLDOWN";

export interface ExecutionRecord {
  action: Action;
  gateway_called: boolean;
  calls: string[];
  expected_state: string | null;
  detail: string;
  cooldown_until: string | null;
  succeeded: boolean;
  reconciled: boolean | null;
}

export interface VerificationRecord {
  performed: boolean;
  expected_state: string | null;
  observed_state: string | null;
  matched: boolean | null;
  detail: string;
}

export interface EventTrace {
  batch_run_id: string;
  payment_id: string;
  amount: number;
  currency: string;
  outcome: Outcome;
  failed_stage: string | null;
  needs_review_reason: string | null;

  resolved_state: string | null;
  resolution_reason: string | null;
  resolution_confidence: number | null;
  root_cause: string | null;
  causal_chain: string[];
  trace_confidence: number | null;
  ambiguous: boolean | null;
  ambiguity_reasons: string[];

  recommended_action: Action | null;
  llm_called: boolean | null;
  recommendation_confidence: number | null;
  reasoning: string | null;
  injection_patterns_flagged: string[];
  /**
   * What the model returned before the deterministic guard replaced it, and
   * why. Both null unless the guard intervened, so their presence is itself the
   * signal that it did.
   */
  original_llm_action: Action | null;
  guard_override_reason: string | null;

  approved: boolean | null;
  final_action: Action | null;
  blocked_reason: string | null;
  rule_id: string | null;

  execution: ExecutionRecord | null;
  verification: VerificationRecord | null;

  processed_at: string;
}

export interface BatchSummary {
  batch_run_id: string;
  started_at: string;
  finished_at: string;
  total_events: number;
  recovered: number;
  blocked: number;
  escalated: number;
  needs_review: number;
  no_action: number;
}

export interface HumanReviewItem {
  payment_id: string;
  amount: number;
  reason: string;
  final_action: Action;
  root_cause: string | null;
  blocked_reason: string | null;
}

export interface BatchResults {
  batch_run_id: string;
  summary: BatchSummary;
  events: EventTrace[];
  needs_human_review: HumanReviewItem[];
}
