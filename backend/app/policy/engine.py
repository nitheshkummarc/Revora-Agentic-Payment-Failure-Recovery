"""PolicyEngine (Module 5) -- deterministic guardrails, including RBI fields.

Consumes the Intelligence Layer's output and either approves it or blocks it
with a reason that names the exact rule that fired. Fully deterministic: no LLM
calls. It never touches the gateway either -- the Orchestrator executes, the
PolicyEngine only approves or blocks.

Two design points worth knowing before reading the code:

1. **Fail closed, and check for absence before checking values.** A missing RBI
   field is evaluated before any comparison that would use it, so an absent
   field can never be read as a passing value. GROUND_TRUTH.md Day 6-7 calls
   this "the single most defensible line in your pitch versus a generic dunning
   bot", and it only holds if absence is checked first.

2. **Only debiting actions are gated on the RBI fields.** RETRY_SOFT re-attempts
   a customer's payment; REQUEST_VERIFICATION reads status, and ESCALATE_HUMAN /
   NO_ACTION_COOLDOWN stop. Requiring a pre-debit notice before an escalation
   would block the safe path and push events toward the unsafe one. The one
   exception is the opt-out rule, which blocks everything -- see
   `rules.check_opted_out`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, List, Optional

from app.core.logging import get_logger, log_event
from app.intelligence.schemas import IntelligenceDecision, RecommendedAction
from app.policy import rules as R
from app.policy.schemas import EventContext, PolicyDecision, RuleEvaluation

logger = get_logger("policy")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PolicyEngine:
    """Deterministic validator for LLM recovery recommendations."""

    def __init__(self, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._clock = clock or _utcnow

    def validate(
        self,
        llm_output: IntelligenceDecision,
        event_context: EventContext,
    ) -> PolicyDecision:
        now = event_context.evaluated_at or self._clock()
        recommended = llm_output.recommended_action
        evaluations: List[RuleEvaluation] = []

        def record(rule_id: R.RuleId, violation: Optional[R.Violation]) -> Optional[R.Violation]:
            evaluations.append(
                RuleEvaluation(
                    rule_id=rule_id.value,
                    passed=violation is None,
                    detail=violation.detail if violation else None,
                )
            )
            return violation

        # ------------------------------------------------------------------
        # 1. Opt-out. Checked first and applies to EVERY action, because
        #    COOLDOWN_AFTER_OPT_OUT is "permanent" with no override.
        # ------------------------------------------------------------------
        violation = record(
            R.RuleId.CUSTOMER_OPTED_OUT, R.check_opted_out(event_context.opted_out)
        )
        if violation:
            return self._blocked(llm_output, event_context, violation, evaluations, now)

        # ------------------------------------------------------------------
        # 2. Non-debiting actions need no RBI gating: they read state or stop.
        # ------------------------------------------------------------------
        if recommended not in R.DEBITING_ACTIONS:
            return self._approved(
                llm_output,
                event_context,
                evaluations,
                now,
                note=(
                    f"{recommended.value} does not debit the customer, so the RBI "
                    "pre-debit/ceiling/AFA gates do not apply"
                ),
            )

        # ------------------------------------------------------------------
        # 3. Required RBI fields present? Absence is checked before any value
        #    comparison that would consume it.
        # ------------------------------------------------------------------
        missing = R.check_required_rbi_fields(
            event_context.pre_debit_notice_sent_at,
            event_context.mandate_ceiling,
            event_context.afa_flag,
        )
        for rule_id in (
            R.RuleId.MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT,
            R.RuleId.MISSING_RBI_FIELD_MANDATE_CEILING,
            R.RuleId.MISSING_RBI_FIELD_AFA_FLAG,
        ):
            evaluations.append(
                RuleEvaluation(
                    rule_id=rule_id.value,
                    passed=not (missing and missing.rule_id is rule_id),
                    detail=missing.detail if missing and missing.rule_id is rule_id else None,
                )
            )
        if missing:
            return self._blocked(llm_output, event_context, missing, evaluations, now)

        # Past this point the three RBI fields are known to be present.
        assert event_context.pre_debit_notice_sent_at is not None
        assert event_context.mandate_ceiling is not None
        assert event_context.afa_flag is not None

        # ------------------------------------------------------------------
        # 4. Value rules.
        # ------------------------------------------------------------------
        checks = [
            (
                R.RuleId.PRE_DEBIT_NOTICE_TOO_RECENT,
                lambda: R.check_pre_debit_notice(event_context.pre_debit_notice_sent_at, now),
            ),
            (
                R.RuleId.MANDATE_CEILING_EXCEEDED,
                lambda: R.check_mandate_ceiling(
                    event_context.amount, event_context.mandate_ceiling
                ),
            ),
            (
                R.RuleId.AFA_REQUIRED_AND_MISSING,
                lambda: R.check_afa(event_context.amount, event_context.afa_flag),
            ),
            (
                R.RuleId.MAX_DISCOUNT_EXCEEDED,
                lambda: R.check_max_discount(event_context.discount_amount),
            ),
            (
                R.RuleId.MAX_RETRIES_EXCEEDED,
                lambda: R.check_max_retries(event_context.retry_count),
            ),
            (
                R.RuleId.TRACE_CONFIDENCE_BELOW_THRESHOLD,
                # Skipped, not silently passed, when the Orchestrator did not
                # supply a tracer confidence -- see EventContext.trace_confidence.
                lambda: (
                    None
                    if event_context.trace_confidence is None
                    else R.check_trace_confidence(event_context.trace_confidence)
                ),
            ),
        ]

        first_violation: Optional[R.Violation] = None
        for rule_id, check in checks:
            result = check()
            evaluations.append(
                RuleEvaluation(
                    rule_id=rule_id.value,
                    passed=result is None,
                    detail=result.detail if result else None,
                )
            )
            if result is not None and first_violation is None:
                first_violation = result

        if first_violation is not None:
            return self._blocked(
                llm_output, event_context, first_violation, evaluations, now
            )

        return self._approved(llm_output, event_context, evaluations, now)

    # -- outcome builders --------------------------------------------------
    def _approved(
        self,
        llm_output: IntelligenceDecision,
        event_context: EventContext,
        evaluations: List[RuleEvaluation],
        now: datetime,
        note: Optional[str] = None,
    ) -> PolicyDecision:
        decision = PolicyDecision(
            payment_id=event_context.payment_id,
            approved=True,
            blocked_reason=None,
            final_action=llm_output.recommended_action,
            rule_id=None,
            recommended_action=llm_output.recommended_action,
            overrode_llm=False,
            rules_evaluated=evaluations,
            rbi_circular=R.RBI_CIRCULAR,
            llm_confidence=llm_output.confidence,
            trace_confidence=event_context.trace_confidence,
            injection_patterns_flagged=list(
                llm_output.sanitization.injection_patterns_flagged
            ),
            decided_at=now,
        )
        self._log(decision, note)
        return decision

    def _blocked(
        self,
        llm_output: IntelligenceDecision,
        event_context: EventContext,
        violation: R.Violation,
        evaluations: List[RuleEvaluation],
        now: datetime,
    ) -> PolicyDecision:
        decision = PolicyDecision(
            payment_id=event_context.payment_id,
            approved=False,
            blocked_reason=violation.blocked_reason,
            final_action=violation.final_action,
            rule_id=violation.rule_id.value,
            recommended_action=llm_output.recommended_action,
            overrode_llm=violation.final_action is not llm_output.recommended_action,
            rules_evaluated=evaluations,
            rbi_circular=R.RBI_CIRCULAR,
            llm_confidence=llm_output.confidence,
            trace_confidence=event_context.trace_confidence,
            injection_patterns_flagged=list(
                llm_output.sanitization.injection_patterns_flagged
            ),
            decided_at=now,
        )
        self._log(decision)
        return decision

    @staticmethod
    def _log(decision: PolicyDecision, note: Optional[str] = None) -> None:
        """Every validate() call is logged -- approved or blocked -- with
        enough detail to reconstruct the decision for the audit trail.

        The three things GROUND_TRUTH.md Day 6-7's deliverable requires to be
        visible together are all here: the LLM recommendation, the block, and
        the reason string.
        """
        log_event(
            logger,
            "policy_decision",
            payment_id=decision.payment_id,
            llm_recommended_action=decision.recommended_action.value,
            approved=decision.approved,
            final_action=decision.final_action.value,
            rule_id=decision.rule_id,
            blocked_reason=decision.blocked_reason,
            overrode_llm=decision.overrode_llm,
            rules_checked=[e.rule_id for e in decision.rules_evaluated],
            rules_failed=[e.rule_id for e in decision.rules_evaluated if not e.passed],
            note=note,
        )
