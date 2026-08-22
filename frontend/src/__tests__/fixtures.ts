/**
 * Hand-built traces, one per outcome the dashboard renders.
 *
 * Small and explicit so a test's expected value is readable next to it. The
 * real 500-event run is exercised separately in `realData.test.tsx`, which
 * loads the committed snapshot rather than these.
 */

import type { BatchResults, EventTrace } from "../types";

export function makeEvent(overrides: Partial<EventTrace> = {}): EventTrace {
  return {
    batch_run_id: "run-1",
    payment_id: "pay_1",
    amount: 50000,
    currency: "INR",
    outcome: "recovered",
    failed_stage: null,
    needs_review_reason: null,
    resolved_state: "FAILED",
    resolution_reason: "clean_single_event",
    resolution_confidence: 0.98,
    root_cause:
      "Failure at step: payment_authentication, source: customer, reason: incorrect_otp",
    causal_chain: ["evt_pay_1_1"],
    trace_confidence: 0.98,
    ambiguous: false,
    ambiguity_reasons: [],
    recommended_action: "RETRY_SOFT",
    llm_called: true,
    recommendation_confidence: 0.8,
    reasoning: "transient failure, a single soft retry is reasonable",
    injection_patterns_flagged: [],
    approved: true,
    final_action: "RETRY_SOFT",
    blocked_reason: null,
    rule_id: null,
    execution: {
      action: "RETRY_SOFT",
      gateway_called: true,
      calls: ["GET /payments/{id}/status", "POST /payments/capture"],
      expected_state: "CAPTURED",
      detail: "re-attempted the payment and captured it",
      cooldown_until: null,
      succeeded: true,
      reconciled: null,
    },
    verification: {
      performed: true,
      expected_state: "CAPTURED",
      observed_state: "CAPTURED",
      matched: true,
      detail: "observed state matches the expected state",
    },
    processed_at: "2026-04-21T10:00:00Z",
    ...overrides,
  };
}

export const recovered = makeEvent({ payment_id: "pay_recovered", amount: 250000 });

export const blocked = makeEvent({
  payment_id: "pay_blocked",
  amount: 120000,
  outcome: "blocked",
  approved: false,
  final_action: "ESCALATE_HUMAN",
  rule_id: "MAX_DISCOUNT_EXCEEDED",
  blocked_reason:
    "MAX_DISCOUNT_EXCEEDED: proposed discount Rs.5,000.00 exceeds MAX_DISCOUNT of Rs.500",
  execution: null,
  verification: null,
});

export const blockedSip = makeEvent({
  payment_id: "pay_blocked_sip",
  amount: 15000000,
  outcome: "blocked",
  approved: false,
  final_action: "ESCALATE_HUMAN",
  rule_id: "AFA_SIP_INSURANCE_REQUIRED_AND_MISSING",
  blocked_reason:
    "AFA_SIP_INSURANCE_REQUIRED_AND_MISSING: action amount Rs.150,000.00 is above the AFA_REQUIRED_ABOVE_SIP_INSURANCE threshold of Rs.100,000 that applies to a 'sip' mandate [RBI/DPSS/2026-27/396]",
  execution: null,
  verification: null,
});

export const escalated = makeEvent({
  payment_id: "pay_escalated",
  amount: 99900,
  outcome: "escalated",
  resolved_state: "PENDING_WEBHOOK",
  resolution_reason: "silence_threshold_exceeded",
  root_cause: "Undetermined root cause for state PENDING_WEBHOOK",
  causal_chain: [],
  trace_confidence: 0.0,
  ambiguous: true,
  ambiguity_reasons: ["no_delivered_events", "confidence_below_threshold: 0.00 < 0.60"],
  recommended_action: "REQUEST_VERIFICATION",
  llm_called: false,
  final_action: "REQUEST_VERIFICATION",
  execution: {
    action: "REQUEST_VERIFICATION",
    gateway_called: true,
    calls: ["GET /payments/{id}/status"],
    expected_state: null,
    detail: "status query confirmed the failure",
    cooldown_until: null,
    succeeded: true,
    reconciled: false,
  },
  verification: null,
});

export const injected = makeEvent({
  payment_id: "pay_injected",
  amount: 49900,
  outcome: "escalated",
  injection_patterns_flagged: ["ignore_previous_instructions", "action_injection"],
  recommended_action: "ESCALATE_HUMAN",
  final_action: "ESCALATE_HUMAN",
  execution: {
    action: "ESCALATE_HUMAN",
    gateway_called: false,
    calls: [],
    expected_state: null,
    detail: "handed to a human",
    cooldown_until: null,
    succeeded: true,
    reconciled: null,
  },
  verification: null,
});

export const noAction = makeEvent({
  payment_id: "pay_no_action",
  amount: 14900,
  outcome: "no_action",
  resolved_state: "AUTHORIZED",
  resolution_reason: "late_authorization_flip",
  root_cause: null,
  causal_chain: [],
  trace_confidence: null,
  ambiguous: null,
  recommended_action: null,
  llm_called: null,
  recommendation_confidence: null,
  reasoning: null,
  approved: null,
  final_action: null,
  execution: {
    action: "NO_ACTION_COOLDOWN",
    gateway_called: false,
    calls: [],
    expected_state: null,
    detail: "payment resolved to AUTHORIZED; it did not fail",
    cooldown_until: null,
    succeeded: true,
    reconciled: null,
  },
  verification: null,
});

export const sampleResults: BatchResults = {
  batch_run_id: "run-1",
  summary: {
    batch_run_id: "run-1",
    started_at: "2026-04-21T10:00:00Z",
    finished_at: "2026-04-21T10:00:00Z",
    total_events: 6,
    recovered: 1,
    blocked: 2,
    escalated: 2,
    needs_review: 0,
    no_action: 1,
  },
  events: [recovered, blocked, blockedSip, escalated, injected, noAction],
  needs_human_review: [
    {
      payment_id: "pay_escalated",
      amount: 99900,
      reason: "tracer reported ambiguous=true",
      final_action: "REQUEST_VERIFICATION",
      root_cause: "Undetermined root cause for state PENDING_WEBHOOK",
      blocked_reason: null,
    },
  ],
};
