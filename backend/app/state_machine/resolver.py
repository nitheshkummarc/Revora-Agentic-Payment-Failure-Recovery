"""StateResolver (Module 2).

Given the merchant-visible evidence for one payment -- the delivered webhook
events plus the payment's creation time -- decide which of the canonical states
it is actually in, and record which rule fired.

Two documented Razorpay behaviours drive the design (GROUND_TRUTH.md Day 0):

1. A payment can show as Failed because of a bank/Razorpay communication gap
   (or a closed browser tab) and later flip to Authorized when the delayed
   response arrives. The resolver orders by the event's OWN timestamp, so the
   later AUTHORIZED wins, and it logs that a flip occurred.

2. `payment.failed` does not fire at all when the failure happens during the
   authentication step of a first attempt. Silence is therefore a signal, not
   an absence of one -- but only after enough time has passed. Before
   SILENCE_THRESHOLD_SECONDS the payment is legitimately still in progress and
   is reported as plain CREATED. Flagging earlier would manufacture false
   ambiguity that Module 3's tracer would then have to explain away.

Scope boundary: this module resolves *what state a payment is in*. It does not
explain *why* a failure happened -- that is Module 3's FailurePropagationTracer.
No LLM calls, no policy or recovery decisions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, List, Optional, Sequence, Tuple, Union

from app.core.logging import get_logger, log_event
from app.gateway.schemas import WebhookEvent, WebhookEventName
from app.state_machine.schemas import (
    PaymentObservation,
    ResolutionLogEntry,
    ResolutionRule,
    StateResolution,
)
from app.state_machine.states import (
    SILENCE_THRESHOLD_SECONDS,
    STATE_BEARING_EVENTS,
    CanonicalState,
    from_event_name,
    is_legal_transition,
)

logger = get_logger("state_machine")


# --------------------------------------------------------------------------
# Confidence formula
#
# Confidence answers one question: how sure are we that `state` is the
# payment's true current state? It starts at BASE_CONFIDENCE and takes a
# documented deduction for each thing that made the evidence harder to read.
# Every deduction below is a named constant -- no per-event magic numbers.
#
# These weights are a RecoverX design choice, not a documented Razorpay or RBI
# figure. Do not present them to judges as either.
# --------------------------------------------------------------------------
BASE_CONFIDENCE = 1.0

# Conflicting evidence existed (a FAILED and a later AUTHORIZED for the same
# payment). Timestamp ordering resolves it, so this is a moderate deduction,
# not a large one -- we do know which event came last.
PENALTY_FLIP = 0.20

# Events arrived in a different order than they occurred. Ordering by the
# event's own timestamp corrects this, so the deduction is small.
PENALTY_OUT_OF_ORDER = 0.05

# A duplicate delivery was collapsed. Almost no uncertainty -- the duplicate is
# byte-identical to an event we already have.
PENALTY_DUPLICATE = 0.02

# An event implied a transition that is illegal from the state the evidence had
# reached. That means the chain we can see is inconsistent.
PENALTY_ILLEGAL_TRANSITION = 0.25

# No state-bearing webhook at all, past the threshold. We genuinely do not know
# what happened, only that we should have heard something by now. This is the
# largest deduction by design: it is what routes the payment to a status check
# instead of an autonomous action.
PENALTY_SILENCE_EXCEEDED = 0.70

# No state-bearing webhook yet, but still inside the threshold. The payment is
# most likely just in flight, so confidence stays high.
PENALTY_WITHIN_SILENCE = 0.10

CONFIDENCE_FLOOR = 0.05


ResolveInput = Union[WebhookEvent, Sequence[WebhookEvent], PaymentObservation]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StateResolver:
    """Resolves chaotic webhook evidence to one canonical state, with a log of
    which rule fired."""

    def __init__(
        self,
        silence_threshold_seconds: float = SILENCE_THRESHOLD_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.silence_threshold_seconds = silence_threshold_seconds
        self._clock = clock or _utcnow

    # -- input normalisation ---------------------------------------------
    def _normalise(
        self,
        event_or_events: ResolveInput,
        payment_id: Optional[str],
        created_at: Optional[datetime],
        observed_at: Optional[datetime],
    ) -> PaymentObservation:
        if isinstance(event_or_events, PaymentObservation):
            observation = event_or_events.model_copy(deep=True)
            if observed_at is not None:
                observation.observed_at = observed_at
            return observation

        if isinstance(event_or_events, WebhookEvent):
            events: List[WebhookEvent] = [event_or_events]
        else:
            events = list(event_or_events)

        for event in events:
            if event.entity_type != "payment":
                raise ValueError(
                    f"StateResolver resolves payments only; got entity_type="
                    f"{event.entity_type!r} for {event.event_id}"
                )

        entity_ids = {event.entity_id for event in events}
        if len(entity_ids) > 1:
            raise ValueError(
                "StateResolver resolves one payment at a time; got events for "
                f"{sorted(entity_ids)}"
            )

        resolved_id = payment_id or (entity_ids.pop() if entity_ids else None)
        if resolved_id is None:
            raise ValueError("cannot resolve without a payment_id or at least one event")

        if created_at is None:
            if not events:
                # The silence rule needs a reference point. Guessing one would
                # silently fabricate the very number the rule turns on.
                raise ValueError(
                    "created_at is required to resolve a payment with no events "
                    "-- the silence threshold cannot be evaluated without it"
                )
            created_at = min(event.occurred_at for event in events)

        return PaymentObservation(
            payment_id=resolved_id,
            created_at=created_at,
            events=events,
            observed_at=observed_at,
        )

    # -- main entry point -------------------------------------------------
    def resolve(
        self,
        event_or_events: ResolveInput,
        *,
        payment_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
        observed_at: Optional[datetime] = None,
    ) -> StateResolution:
        observation = self._normalise(event_or_events, payment_id, created_at, observed_at)
        now = observation.observed_at or self._clock()
        log: List[ResolutionLogEntry] = []

        considered = [event.event_id for event in observation.events]
        unique, duplicates = self._deduplicate(observation.events)
        if duplicates:
            log.append(
                ResolutionLogEntry(
                    rule="duplicate_delivery_collapsed",
                    message=(
                        f"{len(duplicates)} duplicate delivery/deliveries collapsed; "
                        "deduplicated on (payment_id, sequence, occurred_at), not arrival order"
                    ),
                    event_ids=[event.event_id for event in duplicates],
                    at=now,
                )
            )

        ordered = self._order(unique)
        out_of_order = self._detect_out_of_order(unique, ordered)
        if out_of_order:
            log.append(
                ResolutionLogEntry(
                    rule="out_of_order_arrival_corrected",
                    message=(
                        "events arrived in a different order than they occurred; "
                        "ordered by the event's own occurred_at + sequence"
                    ),
                    event_ids=[event.event_id for event in ordered],
                    at=now,
                )
            )

        state, flip_detected, illegal, applied_log = self._fold(ordered, now)
        log.extend(applied_log)

        # ------------------------------------------------------------------
        # Silence rule. Applies only when the evidence never moved the payment
        # off CREATED -- i.e. no authorized, captured or failed webhook was
        # delivered at all.
        # ------------------------------------------------------------------
        silence_seconds: Optional[float] = None
        needs_status_check = False
        rule: ResolutionRule

        has_state_bearing = any(event.event in STATE_BEARING_EVENTS for event in ordered)
        if not has_state_bearing:
            silence_seconds = (now - observation.created_at).total_seconds()
            if silence_seconds >= self.silence_threshold_seconds:
                state = CanonicalState.PENDING_WEBHOOK
                needs_status_check = True
                rule = ResolutionRule.SILENCE_THRESHOLD_EXCEEDED
                detail = (
                    f"no authorized/captured/failed webhook after "
                    f"{silence_seconds:.0f}s (threshold "
                    f"{self.silence_threshold_seconds:.0f}s); silence is the signal -- "
                    "a status check is required before any recovery action"
                )
            else:
                state = CanonicalState.CREATED
                rule = ResolutionRule.WITHIN_SILENCE_THRESHOLD
                detail = (
                    f"no state-bearing webhook yet, but only {silence_seconds:.0f}s "
                    f"elapsed of a {self.silence_threshold_seconds:.0f}s threshold -- "
                    "legitimately still in progress, not ambiguous"
                )
            log.append(
                ResolutionLogEntry(rule=rule.value, message=detail, event_ids=[], at=now)
            )
        elif flip_detected:
            rule = ResolutionRule.LATE_AUTHORIZATION_FLIP
            detail = (
                "payment was marked FAILED and a later payment.authorized arrived for "
                "the same payment_id; resolved to AUTHORIZED by timestamp ordering "
                "(documented Razorpay bank/Razorpay communication-gap case)"
            )
        elif illegal:
            # Never label an inconsistent chain "clean". The ignored events are
            # listed in `ignored_event_ids` so an auditor can see exactly which
            # ones did not fit.
            rule = ResolutionRule.INCONSISTENT_EVENT_CHAIN
            detail = (
                f"{len(illegal)} event(s) implied a transition not reachable from the "
                f"state the evidence had reached; ignored and flagged, resolved to "
                f"{state.value} from the remaining events"
            )
        elif len(ordered) == 1:
            rule = ResolutionRule.CLEAN_SINGLE_EVENT
            detail = f"single clean {ordered[0].event.value} event, no conflicting evidence"
        else:
            rule = ResolutionRule.ORDERED_EVENT_CHAIN
            detail = (
                f"{len(ordered)} events folded in occurred_at order, "
                "no conflicting evidence"
            )

        confidence = self._score(
            flip_detected=flip_detected,
            out_of_order=out_of_order,
            duplicate_count=len(duplicates),
            illegal_count=len(illegal),
            rule=rule,
        )

        resolution = StateResolution(
            payment_id=observation.payment_id,
            state=state,
            needs_status_check=needs_status_check,
            resolution_confidence=confidence,
            resolution_reason=rule,
            resolution_detail=detail,
            resolved_at=now,
            considered_event_ids=considered,
            ordered_event_ids=[event.event_id for event in ordered],
            duplicate_event_ids=[event.event_id for event in duplicates],
            ignored_event_ids=[event.event_id for event in illegal],
            flip_detected=flip_detected,
            out_of_order_detected=out_of_order,
            silence_seconds=silence_seconds,
            resolution_log=log,
        )

        log_event(
            logger,
            "state_resolved",
            payment_id=resolution.payment_id,
            state=resolution.state.value,
            rule=resolution.resolution_reason.value,
            confidence=resolution.resolution_confidence,
            needs_status_check=resolution.needs_status_check,
            flip_detected=resolution.flip_detected,
        )
        return resolution

    # -- steps -------------------------------------------------------------
    @staticmethod
    def _dedupe_key(event: WebhookEvent):
        """Same key Module 1 uses: the event's own identity, never arrival."""
        return (event.entity_id, event.sequence, event.occurred_at.isoformat())

    def _deduplicate(
        self, events: Iterable[WebhookEvent]
    ) -> Tuple[List[WebhookEvent], List[WebhookEvent]]:
        seen = set()
        unique: List[WebhookEvent] = []
        duplicates: List[WebhookEvent] = []
        for event in events:
            key = self._dedupe_key(event)
            if key in seen:
                duplicates.append(event)
                continue
            seen.add(key)
            unique.append(event)
        return unique, duplicates

    @staticmethod
    def _order(events: Sequence[WebhookEvent]) -> List[WebhookEvent]:
        """Order by the event's OWN timestamp, then sequence. Never by arrival.

        This is what makes the Failed->Authorized flip resolve correctly even
        when the two events are delivered in reverse.
        """
        return sorted(events, key=lambda event: (event.occurred_at, event.sequence))

    @staticmethod
    def _detect_out_of_order(
        as_received: Sequence[WebhookEvent], ordered: Sequence[WebhookEvent]
    ) -> bool:
        arrival = sorted(
            as_received,
            key=lambda event: (event.webhook_received_at, event.sequence),
        )
        return [event.event_id for event in arrival] != [event.event_id for event in ordered]

    def _fold(
        self, ordered: Sequence[WebhookEvent], now: datetime
    ) -> Tuple[CanonicalState, bool, List[WebhookEvent], List[ResolutionLogEntry]]:
        """Walk the ordered chain, applying each event's implied state."""
        state = CanonicalState.CREATED
        flip_detected = False
        illegal: List[WebhookEvent] = []
        log: List[ResolutionLogEntry] = []

        for event in ordered:
            target = from_event_name(event.event)
            if target is None:
                continue
            if not is_legal_transition(state, event.event):
                illegal.append(event)
                log.append(
                    ResolutionLogEntry(
                        rule="illegal_transition_ignored",
                        message=(
                            f"{event.event.value} implies {target.value} but that is not "
                            f"reachable from {state.value}; event ignored and flagged"
                        ),
                        event_ids=[event.event_id],
                        at=now,
                    )
                )
                continue
            if state is CanonicalState.FAILED and target is CanonicalState.AUTHORIZED:
                flip_detected = True
                log.append(
                    ResolutionLogEntry(
                        rule=ResolutionRule.LATE_AUTHORIZATION_FLIP.value,
                        message=(
                            f"flip detected: {event.event_id} ({event.event.value} at "
                            f"{event.occurred_at.isoformat()}) supersedes an earlier FAILED "
                            "for the same payment_id"
                        ),
                        event_ids=[event.event_id],
                        at=now,
                    )
                )
            state = target
        return state, flip_detected, illegal, log

    def _score(
        self,
        *,
        flip_detected: bool,
        out_of_order: bool,
        duplicate_count: int,
        illegal_count: int,
        rule: ResolutionRule,
    ) -> float:
        """Documented additive-penalty formula -- see the constants above."""
        confidence = BASE_CONFIDENCE
        if flip_detected:
            confidence -= PENALTY_FLIP
        if out_of_order:
            confidence -= PENALTY_OUT_OF_ORDER
        confidence -= PENALTY_DUPLICATE * duplicate_count
        confidence -= PENALTY_ILLEGAL_TRANSITION * illegal_count
        if rule is ResolutionRule.SILENCE_THRESHOLD_EXCEEDED:
            confidence -= PENALTY_SILENCE_EXCEEDED
        elif rule is ResolutionRule.WITHIN_SILENCE_THRESHOLD:
            confidence -= PENALTY_WITHIN_SILENCE
        return round(max(CONFIDENCE_FLOOR, min(BASE_CONFIDENCE, confidence)), 4)
