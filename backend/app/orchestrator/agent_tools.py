"""The read-only toolbox the planning agent uses for one payment.

Built by the orchestrator, which is the only module allowed to read the
gateway, and handed to the recommendation layer. Every method reads; none
writes. What each one returns is chosen with the same care as the prompt:

* `check_payment_status` gives the gateway's current state and the states of
  earlier retry attempts. It does not return webhook payloads or the event
  history: the model's view of the evidence stays the tracer's summary.
* `get_mandate_facts` gives the compliance facts from the batch row. The
  customer note is not among them; it reaches the model only inside the
  delimited untrusted block.
* `check_policy` runs the real policy engine on a hypothetical decision. It is
  a prediction: the actual decision is made again, after the guards, on the
  action the agent proposes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict

from app.gateway.mock_gateway import MockPaymentGateway
from app.intelligence.schemas import IntelligenceDecision, RecommendedAction, SanitizationReport
from app.orchestrator.schemas import BatchEvent
from app.policy.engine import PolicyEngine
from app.policy.rules import MAX_RETRIES
from app.policy.schemas import EventContext


class GatewayAgentToolbox:
    """Answers the agent's lookups for one payment."""

    def __init__(
        self,
        gateway: MockPaymentGateway,
        policy: PolicyEngine,
        event: BatchEvent,
        retry_count: int,
        trace_confidence: float,
        clock: Callable[[], datetime],
    ) -> None:
        self._gateway = gateway
        self._policy = policy
        self._event = event
        self._retry_count = retry_count
        self._trace_confidence = trace_confidence
        self._clock = clock

    def check_payment_status(self) -> Dict[str, Any]:
        snapshot = self._gateway.get_payment_status(self._event.payment_id)
        return {
            "payment_id": self._event.payment_id,
            "payment_state": snapshot.payment.state.value,
            "retry_attempts": [
                {"payment_id": attempt.payment_id, "state": attempt.state.value}
                for attempt in snapshot.retry_attempts
            ],
        }

    def get_mandate_facts(self) -> Dict[str, Any]:
        event = self._event
        notice = event.pre_debit_notice_sent_at
        hours_since_notice = (
            round((self._clock() - notice).total_seconds() / 3600, 1) if notice else None
        )
        return {
            "amount_paise": event.amount,
            "currency": event.currency,
            "mandate_ceiling_paise": event.mandate_ceiling,
            "pre_debit_notice_sent_at": notice.isoformat() if notice else None,
            "hours_since_pre_debit_notice": hours_since_notice,
            "afa_flag": event.afa_flag,
            "mandate_category": event.mandate_category,
            "opted_out": event.opted_out,
            "proposed_discount_paise": event.discount_amount,
        }

    def get_retry_history(self) -> Dict[str, Any]:
        snapshot = self._gateway.get_payment_status(self._event.payment_id)
        return {
            "attempts_recorded_by_gateway": snapshot.payment.recovery_attempts,
            "attempts_reported_by_merchant": self._event.retry_count,
            "attempts_counted_by_policy": self._retry_count,
            "max_retries": MAX_RETRIES,
        }

    def check_policy(self, action: RecommendedAction) -> Dict[str, Any]:
        now = self._clock()
        hypothetical = IntelligenceDecision(
            payment_id=self._event.payment_id,
            recommended_action=action,
            confidence=1.0,
            reasoning="hypothetical decision for a policy dry run",
            llm_called=False,
            sanitization=SanitizationReport(
                original_length=0,
                sanitized_length=0,
                truncated=False,
                control_characters_stripped=0,
            ),
            decided_at=now,
        )
        event = self._event
        decision = self._policy.validate(
            hypothetical,
            EventContext(
                payment_id=event.payment_id,
                amount=event.amount,
                currency=event.currency,
                pre_debit_notice_sent_at=event.pre_debit_notice_sent_at,
                mandate_ceiling=event.mandate_ceiling,
                afa_flag=event.afa_flag,
                mandate_category=event.mandate_category,
                opted_out=event.opted_out,
                retry_count=self._retry_count,
                discount_amount=event.discount_amount,
                trace_confidence=self._trace_confidence,
                evaluated_at=now,
            ),
        )
        return {
            "action": action.value,
            "approved": decision.approved,
            "rule_id": decision.rule_id,
            "blocked_reason": decision.blocked_reason,
            "final_action": decision.final_action.value,
        }
