/**
 * Shapes of the data the site reads. They mirror the backend's response
 * models (backend/app/demo/schemas.py and backend/app/orchestrator/schemas.py);
 * only the fields the site uses are listed.
 */

export type Action =
  | "RETRY_SOFT"
  | "REQUEST_VERIFICATION"
  | "ESCALATE_HUMAN"
  | "NO_ACTION_COOLDOWN";

export type Outcome = "recovered" | "blocked" | "escalated" | "needs_review" | "no_action";

export interface AgentStep {
  step: number;
  tool: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
}

export interface ExecutionRecord {
  action: Action;
  gateway_called: boolean;
  calls: string[];
  detail: string;
  succeeded: boolean;
  reconciled: boolean | null;
  charged_payment_id: string | null;
  charged_amount: number | null;
}

export interface VerificationRecord {
  performed: boolean;
  expected_state: string | null;
  observed_state: string | null;
  matched: boolean | null;
  detail: string;
}

export interface Trace {
  payment_id: string;
  amount: number;
  outcome: Outcome;
  failed_stage: string | null;
  needs_review_reason: string | null;
  resolved_state: string | null;
  resolution_reason: string | null;
  resolution_confidence: number | null;
  root_cause: string | null;
  trace_confidence: number | null;
  ambiguous: boolean | null;
  ambiguity_reasons: string[];
  recommended_action: Action | null;
  llm_called: boolean | null;
  short_circuit_reason: string | null;
  model: string | null;
  recommendation_confidence: number | null;
  reasoning: string | null;
  injection_patterns_flagged: string[];
  original_llm_action: Action | null;
  guard_override_reason: string | null;
  agent_steps: AgentStep[];
  approved: boolean | null;
  final_action: Action | null;
  blocked_reason: string | null;
  rule_id: string | null;
  execution: ExecutionRecord | null;
  verification: VerificationRecord | null;
}

export interface Scenario {
  payment_id: string;
  scenario: string;
  bucket: string;
  intent: string;
  amount: number;
  customer_note: string | null;
  expected_policy_rule: string | null;
}

export interface AgentRun {
  scenario: Scenario;
  mode: "scripted" | "live";
  trace: Trace;
  single_call_outcome: Outcome | null;
  single_call_final_action: Action | null;
}

export interface Metrics {
  batch_run_id: string;
  total_events: number;
  outcomes: Record<Outcome, number>;
  money_paise: {
    total: number;
    never_at_risk: number;
    addressable: number;
    recovered: number;
    settled_via_retry: number;
    discount_given: number;
    found_already_paid: number;
    preserved_by_policy: number;
  };
  outcome_paise: Partial<Record<Outcome, number>>;
  rules_fired: Record<string, number>;
  injection_attempts: number;
  injection_attempts_that_moved_money: number;
  model_consultations: number;
  decided_without_model: number;
  guard_overrides: number;
}

interface PolicyTotals {
  counts: Record<string, number>;
  amounts_paise: Record<string, number>;
}

export interface Baseline {
  revora: PolicyTotals;
  naive: PolicyTotals;
  naive_retries_on_paid_payments: number;
  naive_charged_twice: number;
  naive_charged_twice_paise: number;
  revora_charged_twice: number;
}

export interface SiteData {
  metrics: Metrics;
  baseline: Baseline;
  scenarios: AgentRun[];
}
