"""In-memory Razorpay-shaped payment gateway with delivery-fault injection.

No database, no real API calls, and no webhook signature verification -- there
is no real shared secret to verify against.

Three ideas drive the design:

1. `PaymentRecord.state` is the gateway's own truth. A status query hits the
   gateway directly, so it is unaffected by webhook chaos -- that is what makes
   a status check a genuine disambiguation tool for later modules.
   `PaymentRecord.webhook_derived_state` is what a merchant would believe from
   delivered webhooks alone. Under silent-drop or delay chaos the two diverge,
   and that divergence is the ambiguity the pipeline exists to diagnose.

2. The per-entity `event_history` is append-only. Nothing is ever rewritten in
   place -- the Failed->Authorized flip appends a later `payment.authorized`
   entry next to the earlier `payment.failed` one, both timestamped, because
   state resolution needs the full chronological timeline.

3. Idempotency keys off (entity_id, sequence, occurred_at) -- an event's own
   timestamp and sequence number, never its arrival order. Replaying the same
   event twice appends a history entry (so the duplicate delivery is visible in
   the audit trail) but does not transition state twice.

"Retry" here means infrastructure retry only: our own outbound webhook
delivery retrying a simulated failure with backoff and a circuit breaker.
Payment-recovery retry -- the decision to re-attempt a customer's payment -- is
a business decision owned by the Orchestrator/Policy layer, not this module.
"""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, status

from app.core.config import GATEWAY_SETTINGS, GatewaySettings
from app.core.logging import get_logger, log_event
from app.gateway.chaos import (
    ChaosInjector,
    DeliveryPlan,
    ScheduledDelivery,
    error_or_documented_default,
)
from app.gateway.schemas import (
    PAYMENT_EVENTS,
    SUBSCRIPTION_EVENTS,
    CapturePaymentRequest,
    ChaosConfig,
    CreatePaymentRequest,
    DeliveryAttemptLogEntry,
    ErrorObject,
    FailPaymentRequest,
    PaymentRecord,
    PaymentState,
    PaymentStatusResponse,
    SimulateWebhookRequest,
    SimulateWebhookResponse,
    SubscriptionRecord,
    SubscriptionState,
    SubscriptionStatusResponse,
    TransitionLogEntry,
    WebhookEvent,
    WebhookEventName,
)

logger = get_logger("gateway")


# --------------------------------------------------------------------------
# Transition tables
# --------------------------------------------------------------------------
# `None` in the `to` slot means the event is recorded in history but changes no
# state (order.paid, refund.failed, payment.dispute.created).
_PAYMENT_TRANSITIONS: Dict[WebhookEventName, Tuple[Optional[frozenset], Optional[PaymentState]]] = {
    # FAILED is an allowed source state for AUTHORIZED: that is the documented
    # Razorpay Failed->Authorized flip, not a bug.
    WebhookEventName.PAYMENT_AUTHORIZED: (
        frozenset({PaymentState.CREATED, PaymentState.PENDING_WEBHOOK, PaymentState.FAILED}),
        PaymentState.AUTHORIZED,
    ),
    WebhookEventName.PAYMENT_CAPTURED: (
        frozenset({PaymentState.AUTHORIZED, PaymentState.PENDING_WEBHOOK}),
        PaymentState.CAPTURED,
    ),
    WebhookEventName.PAYMENT_FAILED: (
        frozenset({PaymentState.CREATED, PaymentState.PENDING_WEBHOOK, PaymentState.AUTHORIZED}),
        PaymentState.FAILED,
    ),
    WebhookEventName.REFUND_CREATED: (
        frozenset({PaymentState.CAPTURED}),
        PaymentState.REVERSED,
    ),
    WebhookEventName.ORDER_PAID: (None, None),
    WebhookEventName.REFUND_FAILED: (None, None),
    WebhookEventName.PAYMENT_DISPUTE_CREATED: (None, None),
}

_SUBSCRIPTION_TRANSITIONS: Dict[WebhookEventName, SubscriptionState] = {
    WebhookEventName.SUBSCRIPTION_ACTIVATED: SubscriptionState.ACTIVE,
    WebhookEventName.SUBSCRIPTION_CHARGED: SubscriptionState.ACTIVE,
    WebhookEventName.SUBSCRIPTION_PENDING: SubscriptionState.PENDING,
    WebhookEventName.SUBSCRIPTION_HALTED: SubscriptionState.HALTED,
}


class CircuitOpenError(RuntimeError):
    """Raised when the outbound webhook delivery circuit breaker is open."""


class WebhookDeliveryClient:
    """Infra-level retry-with-backoff + circuit breaker around our own
    outbound webhook delivery.

    A 3-15% chronic failure rate is normal for production integrations, so
    retry with backoff and a max-attempts circuit breaker belong in this layer
    rather than bolted on later.

    Backoff intervals are computed and logged, never slept -- a demo should not
    burn wall-clock time proving that exponential backoff arithmetic works.
    """

    def __init__(
        self,
        settings: GatewaySettings,
        clock: Callable[[], datetime],
        rng: Optional[random.Random] = None,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._rng = rng or random.Random(settings.random_seed)
        self.consecutive_failures = 0
        self.circuit_open = False
        self.delivery_log: List[DeliveryAttemptLogEntry] = []

    def reset_circuit(self) -> None:
        self.consecutive_failures = 0
        self.circuit_open = False

    def deliver(self, event: WebhookEvent) -> bool:
        """Attempt delivery. Returns True on success.

        Raises CircuitOpenError if the breaker has already tripped -- the
        caller records the event as dropped rather than pretending it landed.
        """
        if self.circuit_open:
            raise CircuitOpenError(
                "webhook delivery circuit breaker is open after "
                f"{self.consecutive_failures} consecutive failures"
            )

        for attempt in range(1, self.settings.delivery_max_attempts + 1):
            succeeded = self._rng.random() >= self.settings.webhook_delivery_failure_rate
            backoff = 0.0 if succeeded else self.settings.delivery_backoff_base_seconds * (2 ** (attempt - 1))
            if succeeded:
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.settings.circuit_breaker_threshold:
                    self.circuit_open = True
            self.delivery_log.append(
                DeliveryAttemptLogEntry(
                    event_id=event.event_id,
                    attempt=attempt,
                    succeeded=succeeded,
                    backoff_seconds=backoff,
                    circuit_open=self.circuit_open,
                    at=self._clock(),
                )
            )
            if succeeded:
                return True
            if self.circuit_open:
                raise CircuitOpenError(
                    "webhook delivery circuit breaker tripped during retry of "
                    f"{event.event_id}"
                )
        return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MockPaymentGateway:
    """In-memory payment gateway with chaos injection."""

    def __init__(
        self,
        settings: Optional[GatewaySettings] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.settings = settings or GATEWAY_SETTINGS
        self._clock = clock or _utcnow
        self.payments: Dict[str, PaymentRecord] = {}
        self.subscriptions: Dict[str, SubscriptionRecord] = {}
        self.event_history: Dict[str, List[WebhookEvent]] = {}
        self.transition_log: Dict[str, List[TransitionLogEntry]] = {}
        self.delivery_client = WebhookDeliveryClient(
            self.settings, self._clock, random.Random(self.settings.random_seed)
        )
        self._pending: List[ScheduledDelivery] = []
        self._sequence_counters: Dict[str, itertools.count] = {}
        self._applied_derived: Dict[str, set] = {}
        self._applied_truth: set = set()
        self._last_applied_sequence: Dict[str, int] = {}
        self._id_counter = itertools.count(1)
        # Subscriptions that crossed the halt threshold and still owe a
        # subscription.halted event. Deferred rather than emitted inline so the
        # halt is always logged AFTER the charge attempt that triggered it.
        self._deferred_halts: List[str] = []
        self._draining = False

    # -- clock / ids -----------------------------------------------------
    def now(self) -> datetime:
        return self._clock()

    def _next_sequence(self, entity_id: str) -> int:
        counter = self._sequence_counters.setdefault(entity_id, itertools.count(1))
        return next(counter)

    @staticmethod
    def _event_id(entity_id: str, sequence: int) -> str:
        return f"evt_{entity_id}_{sequence}"

    def _new_payment_id(self) -> str:
        return f"pay_{next(self._id_counter):08d}"

    @staticmethod
    def _dedupe_key(event: WebhookEvent) -> Tuple[str, int, str]:
        """Idempotency key: entity + event timestamp + sequence.

        Deliberately not arrival order: duplicate detection keys off the
        event's own timestamp and sequence, which are stable under reordering.
        """
        return (event.entity_id, event.sequence, event.occurred_at.isoformat())

    # -- logging ---------------------------------------------------------
    def _log_transition(
        self,
        entity_id: str,
        *,
        applied: bool,
        reason: str,
        event: Optional[WebhookEvent] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
    ) -> TransitionLogEntry:
        entry = TransitionLogEntry(
            entity_id=entity_id,
            event_id=event.event_id if event else None,
            event=event.event if event else None,
            sequence=event.sequence if event else None,
            from_state=from_state,
            to_state=to_state,
            applied=applied,
            reason=reason,
            at=self.now(),
        )
        self.transition_log.setdefault(entity_id, []).append(entry)
        log_event(
            logger,
            "transition",
            entity_id=entity_id,
            event=entry.event.value if entry.event else None,
            applied=applied,
            reason=reason,
            from_state=from_state,
            to_state=to_state,
        )
        return entry

    # -- public API ------------------------------------------------------
    def create_payment(self, request: CreatePaymentRequest) -> PaymentRecord:
        self.settle()
        payment_id = request.payment_id or self._new_payment_id()
        if payment_id in self.payments:
            raise ValueError(f"payment {payment_id} already exists")
        now = self.now()
        record = PaymentRecord(
            payment_id=payment_id,
            order_id=request.order_id,
            amount=request.amount,
            currency=request.currency,
            state=PaymentState.CREATED,
            webhook_derived_state=PaymentState.CREATED,
            created_at=now,
            updated_at=now,
            error=None,
            notes=request.notes,
            subscription_id=request.subscription_id,
        )
        self.payments[payment_id] = record
        self.event_history.setdefault(payment_id, [])
        self.transition_log.setdefault(payment_id, [])
        if request.subscription_id:
            self._get_or_create_subscription(request.subscription_id)
        self._log_transition(
            payment_id,
            applied=True,
            reason="payment_created",
            from_state=None,
            to_state=PaymentState.CREATED.value,
        )
        return record

    def capture_payment(self, request: CapturePaymentRequest) -> PaymentStatusResponse:
        self.settle()
        record = self._require_payment(request.payment_id)
        allowed, _ = _PAYMENT_TRANSITIONS[WebhookEventName.PAYMENT_CAPTURED]
        if allowed is not None and record.state not in allowed:
            self._log_transition(
                record.payment_id,
                applied=False,
                reason=f"illegal_capture_from_{record.state.value}",
                from_state=record.state.value,
            )
            raise IllegalTransitionError(
                f"cannot capture payment {record.payment_id} from state {record.state.value}"
            )
        self._emit(
            entity_id=record.payment_id,
            entity_type="payment",
            event_name=WebhookEventName.PAYMENT_CAPTURED,
            error=None,
            chaos=request.chaos,
        )
        return self.get_payment_status(record.payment_id)

    def fail_payment(self, request: FailPaymentRequest) -> PaymentStatusResponse:
        self.settle()
        record = self._require_payment(request.payment_id)
        error = error_or_documented_default(request.error)
        allowed, _ = _PAYMENT_TRANSITIONS[WebhookEventName.PAYMENT_FAILED]
        if allowed is not None and record.state not in allowed:
            self._log_transition(
                record.payment_id,
                applied=False,
                reason=f"illegal_fail_from_{record.state.value}",
                from_state=record.state.value,
            )
            raise IllegalTransitionError(
                f"cannot fail payment {record.payment_id} from state {record.state.value}"
            )
        record.error = error
        self._emit(
            entity_id=record.payment_id,
            entity_type="payment",
            event_name=WebhookEventName.PAYMENT_FAILED,
            error=error,
            chaos=request.chaos,
        )
        return self.get_payment_status(record.payment_id)

    def simulate_webhook(self, request: SimulateWebhookRequest) -> SimulateWebhookResponse:
        self.settle()
        event_name = request.event
        if event_name in SUBSCRIPTION_EVENTS:
            entity_type = "subscription"
            self._get_or_create_subscription(request.entity_id)
        elif event_name in PAYMENT_EVENTS:
            entity_type = "payment"
            self._require_payment(request.entity_id)
        else:  # pragma: no cover - enum is exhaustive
            raise ValueError(f"unroutable webhook event {event_name}")

        plan = self._emit(
            entity_id=request.entity_id,
            entity_type=entity_type,
            event_name=event_name,
            error=request.error,
            chaos=request.chaos,
        )
        delivered = [
            sd.event for sd in plan.scheduled if sd.deliver_at <= self.now()
        ]
        scheduled = [sd.event for sd in plan.scheduled if sd.deliver_at > self.now()]

        if entity_type == "payment":
            record = self.payments[request.entity_id]
            current_state = record.state.value
            derived = record.webhook_derived_state.value
        else:
            sub = self.subscriptions[request.entity_id]
            current_state = sub.state.value
            derived = None
        return SimulateWebhookResponse(
            entity_type=entity_type,
            entity_id=request.entity_id,
            delivered=delivered,
            dropped=list(plan.dropped),
            scheduled=scheduled,
            current_state=current_state,
            webhook_derived_state=derived,
        )

    def get_payment_status(self, payment_id: str) -> PaymentStatusResponse:
        """Status query. Hits the gateway's own truth, so it is unaffected by
        webhook chaos -- this is the disambiguation tool for later modules."""
        self.settle()
        record = self._require_payment(payment_id)
        return PaymentStatusResponse(
            payment=record,
            event_history=list(self.event_history.get(payment_id, [])),
            transition_log=list(self.transition_log.get(payment_id, [])),
            pending_webhook_count=sum(
                1 for sd in self._pending if sd.event.entity_id == payment_id
            ),
        )

    def get_subscription_status(self, subscription_id: str) -> SubscriptionStatusResponse:
        self.settle()
        sub = self.subscriptions.get(subscription_id)
        if sub is None:
            raise EntityNotFoundError(f"subscription {subscription_id} not found")
        return SubscriptionStatusResponse(
            subscription=sub,
            event_history=list(self.event_history.get(subscription_id, [])),
            transition_log=list(self.transition_log.get(subscription_id, [])),
        )

    # -- emission / delivery ---------------------------------------------
    def _emit(
        self,
        *,
        entity_id: str,
        entity_type: str,
        event_name: WebhookEventName,
        error: Optional[ErrorObject],
        chaos: Optional[ChaosConfig],
    ) -> DeliveryPlan:
        now = self.now()
        sequence = self._next_sequence(entity_id)
        primary = WebhookEvent(
            event_id=self._event_id(entity_id, sequence),
            entity_type=entity_type,  # type: ignore[arg-type]
            entity_id=entity_id,
            event=event_name,
            sequence=sequence,
            occurred_at=now,
            webhook_sent_at=now,
            webhook_received_at=now,
            delivery_attempt=1,
            is_duplicate_delivery=False,
            resulting_state=self._preview_state(entity_id, entity_type, event_name),
            error=error,
            chaos_modes=[],
        )

        # The gateway itself knows about the primary event the instant it
        # happens, regardless of whether the webhook ever lands.
        self._apply_truth(primary)

        injector = ChaosInjector(chaos)
        plan = injector.apply(
            [primary],
            now=now,
            next_sequence=lambda: self._next_sequence(entity_id),
            new_event_id=self._event_id,
        )

        for note in plan.notes:
            self._log_transition(entity_id, applied=False, reason=f"chaos:{note}")

        for dropped in plan.dropped:
            self._log_transition(
                entity_id,
                applied=False,
                reason="webhook_silently_dropped_never_delivered",
                event=dropped,
            )

        for sd in plan.scheduled:
            self._pending.append(sd)
        self._pending.sort(key=lambda sd: sd.deliver_at)

        # A webhook is in flight and has not landed: the merchant-observable
        # view is PENDING_WEBHOOK until it does.
        if entity_type == "payment":
            record = self.payments[entity_id]
            future = [sd for sd in plan.scheduled if sd.deliver_at > now]
            if future and record.webhook_derived_state is PaymentState.CREATED:
                record.webhook_derived_state = PaymentState.PENDING_WEBHOOK
                self._log_transition(
                    entity_id,
                    applied=True,
                    reason="webhook_in_flight_derived_state_pending",
                    from_state=PaymentState.CREATED.value,
                    to_state=PaymentState.PENDING_WEBHOOK.value,
                )

        self.settle()
        self._drain_deferred_halts()
        return plan

    def _preview_state(self, entity_id: str, entity_type: str, event_name: WebhookEventName) -> str:
        """The state this event would put the entity into, recorded on the
        event itself so the audit trail is readable without replaying."""
        if entity_type == "payment":
            allowed, target = _PAYMENT_TRANSITIONS.get(event_name, (None, None))
            if target is not None:
                return target.value
            record = self.payments.get(entity_id)
            return record.state.value if record else PaymentState.CREATED.value
        target_sub = _SUBSCRIPTION_TRANSITIONS.get(event_name)
        if target_sub is not None:
            return target_sub.value
        sub = self.subscriptions.get(entity_id)
        return sub.state.value if sub else SubscriptionState.CREATED.value

    def settle(self, now: Optional[datetime] = None) -> List[WebhookEvent]:
        """Deliver every scheduled webhook whose arrival time has passed.

        Called at the top of every public method, so a caller only has to move
        the clock forward -- there is no separate "tick" endpoint to remember.
        """
        current = now or self._clock()
        delivered: List[WebhookEvent] = []
        # Re-read the queue each pass: delivering an event can enqueue another
        # one (a halt triggered by a late-arriving subscription.pending).
        while True:
            due = [sd for sd in self._pending if sd.deliver_at <= current]
            if not due:
                break
            self._pending = [sd for sd in self._pending if sd.deliver_at > current]
            due.sort(key=lambda sd: sd.deliver_at)
            for sd in due:
                if self._deliver(sd.event):
                    delivered.append(sd.event)
            self._drain_deferred_halts()
        self._drain_deferred_halts()
        return delivered

    def _deliver(self, event: WebhookEvent) -> bool:
        """Deliver one webhook through the retrying/circuit-broken client and
        fold it into the merchant-observable (derived) state."""
        try:
            ok = self.delivery_client.deliver(event)
        except CircuitOpenError as exc:
            self._log_transition(
                event.entity_id,
                applied=False,
                reason=f"delivery_circuit_open:{exc}",
                event=event,
            )
            return False
        if not ok:
            self._log_transition(
                event.entity_id,
                applied=False,
                reason="delivery_failed_after_max_attempts",
                event=event,
            )
            return False

        # Append-only history: duplicates land here too, so the audit trail
        # shows that a duplicate arrived and was ignored.
        self.event_history.setdefault(event.entity_id, []).append(event)

        # Chaos-generated events (the flip) have not touched truth yet.
        self._apply_truth(event)
        self._apply_derived(event)
        return True

    # -- state application ------------------------------------------------
    def _apply_truth(self, event: WebhookEvent) -> None:
        key = self._dedupe_key(event)
        if key in self._applied_truth:
            return
        self._applied_truth.add(key)
        if event.entity_type == "payment":
            self._transition_payment_truth(event)
        else:
            self._transition_subscription(event)

    def _transition_payment_truth(self, event: WebhookEvent) -> None:
        record = self.payments.get(event.entity_id)
        if record is None:
            return
        allowed, target = _PAYMENT_TRANSITIONS.get(event.event, (None, None))
        if target is None:
            self._log_transition(
                event.entity_id,
                applied=False,
                reason="event_recorded_no_state_change",
                event=event,
                from_state=record.state.value,
                to_state=record.state.value,
            )
            return
        if allowed is not None and record.state not in allowed:
            self._log_transition(
                event.entity_id,
                applied=False,
                reason=f"illegal_transition_{record.state.value}_to_{target.value}",
                event=event,
                from_state=record.state.value,
                to_state=target.value,
            )
            return
        previous = record.state
        record.state = target
        record.updated_at = self.now()
        flip = previous is PaymentState.FAILED and target is PaymentState.AUTHORIZED
        self._log_transition(
            event.entity_id,
            applied=True,
            reason="late_authorization_flip" if flip else "gateway_truth_transition",
            event=event,
            from_state=previous.value,
            to_state=target.value,
        )

    def _apply_derived(self, event: WebhookEvent) -> None:
        """Fold a delivered webhook into the merchant-observable state.

        Idempotent by (entity, sequence, occurred_at); ordered by `sequence`,
        never by arrival, so an out-of-order delivery cannot walk the state
        backwards.
        """
        if event.entity_type != "payment":
            return
        record = self.payments.get(event.entity_id)
        if record is None:
            return

        key = self._dedupe_key(event)
        seen = self._applied_derived.setdefault(event.entity_id, set())
        if key in seen:
            self._log_transition(
                event.entity_id,
                applied=False,
                reason="duplicate_delivery_ignored_idempotent",
                event=event,
                from_state=record.webhook_derived_state.value,
                to_state=record.webhook_derived_state.value,
            )
            return

        last_seq = self._last_applied_sequence.get(event.entity_id, 0)
        if event.sequence < last_seq:
            seen.add(key)
            self._log_transition(
                event.entity_id,
                applied=False,
                reason=f"out_of_order_superseded_by_sequence_{last_seq}",
                event=event,
                from_state=record.webhook_derived_state.value,
                to_state=record.webhook_derived_state.value,
            )
            return

        allowed, target = _PAYMENT_TRANSITIONS.get(event.event, (None, None))
        seen.add(key)
        if target is None:
            return
        if allowed is not None and record.webhook_derived_state not in allowed:
            self._log_transition(
                event.entity_id,
                applied=False,
                reason=(
                    f"derived_illegal_transition_{record.webhook_derived_state.value}"
                    f"_to_{target.value}"
                ),
                event=event,
                from_state=record.webhook_derived_state.value,
                to_state=target.value,
            )
            return
        previous = record.webhook_derived_state
        record.webhook_derived_state = target
        self._last_applied_sequence[event.entity_id] = event.sequence
        self._log_transition(
            event.entity_id,
            applied=True,
            reason="webhook_derived_transition",
            event=event,
            from_state=previous.value,
            to_state=target.value,
        )

    def _transition_subscription(self, event: WebhookEvent) -> None:
        sub = self.subscriptions.get(event.entity_id)
        if sub is None:
            return
        target = _SUBSCRIPTION_TRANSITIONS.get(event.event)
        if target is None:
            return
        previous = sub.state

        if event.event is WebhookEventName.SUBSCRIPTION_CHARGED:
            sub.failed_charge_attempts = 0
        elif event.event is WebhookEventName.SUBSCRIPTION_PENDING:
            sub.failed_charge_attempts += 1

        sub.state = target
        sub.updated_at = self.now()
        self._log_transition(
            event.entity_id,
            applied=True,
            reason="subscription_transition",
            event=event,
            from_state=previous.value,
            to_state=target.value,
        )

        # Subscriptions halt after exactly three charge-retry attempts.
        # Queued rather than emitted inline -- see _drain_deferred_halts.
        threshold = self.settings.subscription_halt_after_failed_charges
        if (
            event.event is WebhookEventName.SUBSCRIPTION_PENDING
            and sub.failed_charge_attempts >= threshold
            and sub.state is not SubscriptionState.HALTED
            and sub.subscription_id not in self._deferred_halts
        ):
            self._deferred_halts.append(sub.subscription_id)

    def _drain_deferred_halts(self) -> None:
        """Emit `subscription.halted` for any subscription that crossed the
        3-failed-charge threshold. Guarded against re-entrancy so a halt
        emitted mid-delivery cannot recurse into itself."""
        if self._draining:
            return
        self._draining = True
        try:
            while self._deferred_halts:
                subscription_id = self._deferred_halts.pop(0)
                sub = self.subscriptions.get(subscription_id)
                if sub is None or sub.state is SubscriptionState.HALTED:
                    continue
                threshold = self.settings.subscription_halt_after_failed_charges
                self._log_transition(
                    subscription_id,
                    applied=True,
                    reason=f"subscription_halt_threshold_reached_{threshold}_failed_charge_attempts",
                    from_state=sub.state.value,
                    to_state=SubscriptionState.HALTED.value,
                )
                self._emit(
                    entity_id=subscription_id,
                    entity_type="subscription",
                    event_name=WebhookEventName.SUBSCRIPTION_HALTED,
                    error=None,
                    chaos=None,
                )
        finally:
            self._draining = False

    # -- lookups ----------------------------------------------------------
    def _require_payment(self, payment_id: str) -> PaymentRecord:
        record = self.payments.get(payment_id)
        if record is None:
            raise EntityNotFoundError(f"payment {payment_id} not found")
        return record

    def _get_or_create_subscription(self, subscription_id: str) -> SubscriptionRecord:
        sub = self.subscriptions.get(subscription_id)
        if sub is not None:
            return sub
        now = self.now()
        sub = SubscriptionRecord(
            subscription_id=subscription_id,
            state=SubscriptionState.CREATED,
            failed_charge_attempts=0,
            created_at=now,
            updated_at=now,
        )
        self.subscriptions[subscription_id] = sub
        self.event_history.setdefault(subscription_id, [])
        self.transition_log.setdefault(subscription_id, [])
        return sub

    def reset(self) -> None:
        """Wipe all state. Used between tests and demo runs."""
        self.__init__(self.settings, self._clock)  # noqa: PLC2801


class EntityNotFoundError(LookupError):
    """Requested payment/subscription does not exist in the in-memory store."""


class IllegalTransitionError(RuntimeError):
    """Requested transition is not legal from the entity's current state."""


# --------------------------------------------------------------------------
# FastAPI router
#
# API-level errors use FastAPI's plain `detail` shape rather than the Razorpay
# error object. That schema describes payment errors; Razorpay does not publish
# `code`/`step`/`reason` values for API plumbing failures, so emitting one here
# would mean inventing values that look real but are not.
# --------------------------------------------------------------------------
router = APIRouter(tags=["gateway"])

_gateway = MockPaymentGateway()


def get_gateway() -> MockPaymentGateway:
    return _gateway


@router.post("/payments/create", response_model=PaymentRecord)
def create_payment(request: CreatePaymentRequest) -> PaymentRecord:
    try:
        return get_gateway().create_payment(request)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/payments/capture", response_model=PaymentStatusResponse)
def capture_payment(request: CapturePaymentRequest) -> PaymentStatusResponse:
    gateway = get_gateway()
    try:
        return gateway.capture_payment(request)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/payments/fail", response_model=PaymentStatusResponse)
def fail_payment(request: FailPaymentRequest) -> PaymentStatusResponse:
    gateway = get_gateway()
    try:
        return gateway.fail_payment(request)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/payments/{payment_id}/status", response_model=PaymentStatusResponse)
def payment_status(payment_id: str) -> PaymentStatusResponse:
    try:
        return get_gateway().get_payment_status(payment_id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/webhooks/simulate", response_model=SimulateWebhookResponse)
def simulate_webhook(request: SimulateWebhookRequest) -> SimulateWebhookResponse:
    gateway = get_gateway()
    try:
        return gateway.simulate_webhook(request)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
