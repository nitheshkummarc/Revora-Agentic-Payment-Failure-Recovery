import type { Action, Outcome } from "../types";

/** Plain-language names for the dataset's scenarios, grouped as a reader
 * would think of them. Unknown scenarios fall back to their id. */
export const SCENARIO_TITLES: Record<string, { title: string; blurb: string }> = {
  incorrect_otp_clean_delivery: { title: "Wrong OTP", blurb: "The customer typed the wrong OTP. One clean failure webhook." },
  incorrect_otp_duplicate_delivery: { title: "Wrong OTP, sent twice", blurb: "Same failure, but the webhook arrived twice." },
  insufficient_funds_clean_delivery: { title: "Insufficient funds", blurb: "The bank declined for low balance." },
  insufficient_funds_duplicate_delivery: { title: "Insufficient funds, sent twice", blurb: "A duplicate of the decline webhook arrived." },
  card_declined_clean_delivery: { title: "Card declined", blurb: "The issuing bank declined the card." },
  card_declined_duplicate_delivery: { title: "Card declined, sent twice", blurb: "The decline webhook arrived twice." },
  gateway_technical_error_clean_delivery: { title: "Gateway error", blurb: "A technical error at the gateway during authentication." },
  gateway_technical_error_duplicate_delivery: { title: "Gateway error, sent twice", blurb: "The same gateway error, delivered twice." },
  silent_drop: { title: "Webhook never arrived", blurb: "No webhook for over five minutes. The evidence is silent." },
  delay_failed: { title: "Late failure webhook", blurb: "The failure webhook is still in flight when the agent looks." },
  delay_authorized: { title: "Late success webhook", blurb: "The payment succeeded, but the webhook saying so is late." },
  flip: { title: "Failed, then authorized", blurb: "Razorpay's documented flip: the bank approved it after all." },
  chain_gap: { title: "Missing event in the chain", blurb: "One event in the sequence never arrived." },
  out_of_order: { title: "Events out of order", blurb: "Webhooks arrived in a different order than they happened." },
  verify_mismatch_stale_success: { title: "Already paid, webhooks say failed", blurb: "The payment was captured, but those webhooks were dropped." },
  missing_pre_debit_notice: { title: "No pre-debit notice on file", blurb: "The record of the 24-hour notice is missing." },
  missing_mandate_ceiling: { title: "No mandate ceiling on file", blurb: "The customer's mandate limit is missing." },
  missing_afa_flag: { title: "No authentication flag on file", blurb: "Whether extra authentication was done is unknown." },
  pre_debit_notice_too_recent: { title: "Notice sent too recently", blurb: "The pre-debit notice went out less than 24 hours ago." },
  amount_over_mandate_ceiling: { title: "Over the mandate ceiling", blurb: "The amount is above what the customer authorised." },
  afa_required_but_flag_false: { title: "Above ₹15,000, no extra authentication", blurb: "RBI requires extra authentication above ₹15,000." },
  sip_mandate_over_one_lakh_without_afa: { title: "SIP above ₹1 lakh, no authentication", blurb: "The higher limit for SIPs is ₹1 lakh. This is above it." },
  discount_over_cap: { title: "Discount over the cap", blurb: "The proposed recovery discount is above ₹500." },
  retry_budget_exhausted: { title: "Retries used up", blurb: "Three recovery attempts have already been made." },
  injection_note_active_customer: { title: "Note tries to instruct the AI", blurb: "The customer note contains instructions aimed at the model." },
  opted_out_clean_failure: { title: "Customer opted out", blurb: "A clean failure, but the customer has opted out." },
  opted_out_silent_drop: { title: "Opted out, webhook missing", blurb: "The customer opted out and the evidence is silent." },
  opted_out_with_injection_note: { title: "Opted out, note tries to instruct the AI", blurb: "An opt-out combined with a manipulative note." },
  opted_out_flip: { title: "Opted out, then authorized", blurb: "The customer opted out, and the payment succeeded anyway." },
};

export const BUCKETS: { id: string; title: string }[] = [
  { id: "standard_failure", title: "Ordinary failures" },
  { id: "ambiguous", title: "Unclear evidence" },
  { id: "policy_violation", title: "Rule breaks" },
  { id: "adversarial", title: "Manipulation and opt-outs" },
];

export const ACTION_LABELS: Record<Action, string> = {
  RETRY_SOFT: "Retry the payment",
  REQUEST_VERIFICATION: "Check the status first",
  ESCALATE_HUMAN: "Hand to a person",
  NO_ACTION_COOLDOWN: "Wait, do nothing now",
};

export const OUTCOME_LABELS: Record<Outcome, string> = {
  recovered: "Recovered",
  blocked: "Blocked by the rules",
  escalated: "Sent to a person",
  needs_review: "Held for review",
  no_action: "No action needed",
};

export const TOOL_LABELS: Record<string, string> = {
  check_payment_status: "Checked the payment at the gateway",
  get_mandate_facts: "Read the mandate and compliance facts",
  get_retry_history: "Read the retry history",
  check_policy: "Asked the RBI rules about an action",
  propose_action: "Proposed an action",
  rejected_tool_call: "Tried a tool that does not exist",
};

export const RESOLUTION_LABELS: Record<string, string> = {
  clean_single_event: "one clean webhook",
  ordered_event_chain: "an ordered chain of webhooks",
  late_authorization_flip: "a failure followed by a late authorization",
  silence_threshold_exceeded: "silence past the five-minute threshold",
  within_silence_threshold: "no webhooks yet, still within the waiting window",
  order_paid_unconfirmed: "order.paid without a confirming capture",
};

export function scenarioTitle(id: string): string {
  return SCENARIO_TITLES[id]?.title ?? id.replace(/_/g, " ");
}
