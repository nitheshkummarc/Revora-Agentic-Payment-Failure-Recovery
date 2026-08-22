"""Deterministic root-cause tracing for failed payments.

Given a payment resolved to FAILED or PENDING_WEBHOOK, walk backward through
its linked event chain to build a causal path, and state the root cause in
terms of the Razorpay error fields present on the events themselves. No model
calls, no recovery decisions, no gateway calls.

Three properties distinguish this from reading the last error code:

1. Reverse BFS over a real causal graph. Nodes are individual webhook
   delivery attempts, not just events. Edges run backward along two kinds of
   causal link: retry edges (delivery attempt k came after attempt k-1 of the
   same event) and progression edges (this event followed the previous one in
   the payment's life). The walk starts at the terminal node and discovers
   predecessors, so the output is a causal path rather than a flat list.

2. Root cause is quoted, never paraphrased. The `root_cause` string embeds the
   literal `source`, `step` and `reason` values from the event's own error
   object, so it can be grepped back to the originating event. No parallel
   taxonomy is invented on top of Razorpay's.

3. A gap in the chain is reported, never smoothed over. Returning a shorter
   chain that looks complete is worse than admitting the hole, so any missing
   telemetry sets `ambiguous: true`, forcing a status query before any
   recovery action can be taken.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from app.core.logging import get_logger, log_event
from app.gateway.schemas import ErrorObject, WebhookEvent, WebhookEventName
from app.state_machine.schemas import ResolutionRule, StateResolution
from app.state_machine.states import CanonicalState, from_event_name
from app.tracer.schemas import CausalHop, TraceInput, TraceResult

logger = get_logger("tracer")


# --------------------------------------------------------------------------
# Confidence formula
#
# Deliberately MULTIPLICATIVE, not a weighted sum. A confident diagnosis needs
# BOTH a complete chain AND a fully grounded error object -- being strong on
# one cannot compensate for being empty on the other. A weighted sum would let
# a payment with no error object at all still score ~0.5 on chain completeness
# alone, which is precisely the failure mode of filling a gap with a
# plausible-sounding story.
#
#     confidence = chain_completeness
#                * error_grounding
#                * inherited_resolution_confidence
#                - PENALTY_PER_IGNORED_EVENT * len(ignored_event_ids)
#
#   chain_completeness  = observed events / expected events, where expected is
#                         derived from the event sequence numbers themselves
#                         (1..max_sequence). A hole in the middle and a chain
#                         that never reaches the origin both show up here.
#   error_grounding     = fraction of the (source, step, reason) triplet that
#                         is actually populated on the terminal failure event.
#   inherited_...       = the resolver's confidence. The tracer cannot be more
#                         certain about why a payment failed than the resolver
#                         was about what state it is in.
#   ignored events      = events the resolver could not place in the chain.
#                         Each is a piece of evidence nobody has explained.
#
# These weights are local design choices with no external basis. They are not
# documented Razorpay or RBI figures and must not be described as such.
# --------------------------------------------------------------------------
# Heuristic. Not derived from any external figure, and not reverse-engineered
# to make the threshold below cross at a particular ignored-event count: it was
# chosen only as a deduction large enough to matter but small enough not to
# dominate.
#
# What it actually produces, noting that `inherited_resolution_confidence` is
# never 1.0 when events were ignored because resolution has already deducted
# its own PENALTY_ILLEGAL_TRANSITION (0.25) per ignored event, so the two
# penalties compound:
#
#   n ignored | inherited | 1 * 1 * inherited - 0.15n | confidence | < 0.60?
#   ----------+-----------+---------------------------+------------+--------
#       1     |   0.75    |  0.75 - 0.15 =  0.60      |    0.60    |  no
#       2     |   0.50    |  0.50 - 0.30 =  0.20      |    0.20    |  yes
#       3     |   0.25    |  0.25 - 0.45 = -0.20      |    0.00    |  yes
#
# So the confidence gate first fires at n=2. At n=1 the score lands exactly on
# the threshold, and `0.60 < 0.60` is False, so that case is caught solely by
# the structural `events_ignored_during_resolution` rule below -- which is why
# that rule exists independently of the score. The exact-boundary landing is
# coincidence rather than design, and it is knife-edge: changing this constant
# or PENALTY_ILLEGAL_TRANSITION by 0.01 flips whether a second ambiguity reason
# is recorded at n=1. The ambiguity verdict itself is unaffected either way.
PENALTY_PER_IGNORED_EVENT = 0.15

# Below this, the diagnosis is treated as ambiguous regardless of which
# individual check passed. A backstop rather than the primary gate; the
# structural rules below are. Also heuristic -- see the note above.
CONFIDENCE_AMBIGUITY_THRESHOLD = 0.60

# The only two states the tracer is defined for.
TRACEABLE_STATES = frozenset({CanonicalState.FAILED, CanonicalState.PENDING_WEBHOOK})

# Resolution rules that are inherently ambiguous no matter how the chain looks.
AMBIGUOUS_RESOLUTION_RULES = frozenset(
    {
        ResolutionRule.SILENCE_THRESHOLD_EXCEEDED,
        ResolutionRule.INCONSISTENT_EVENT_CHAIN,
    }
)


class NotTraceableError(ValueError):
    """Raised when asked to diagnose a payment that did not fail.

    Deliberately loud. Returning a placeholder root cause for a captured
    payment would put a fabricated diagnosis into the audit trail.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FailurePropagationTracer:
    """Deterministic root-cause tracer. No LLM, ever."""

    def __init__(self, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._clock = clock or _utcnow

    # -- entry point -------------------------------------------------------
    @staticmethod
    def is_traceable(resolution: StateResolution) -> bool:
        return resolution.state in TRACEABLE_STATES

    def trace(self, trace_input: TraceInput) -> TraceResult:
        resolution = trace_input.resolution
        now = trace_input.traced_at or self._clock()

        if not self.is_traceable(resolution):
            raise NotTraceableError(
                f"payment {trace_input.payment_id} resolved to "
                f"{resolution.state.value}; the tracer diagnoses FAILED or "
                "PENDING_WEBHOOK only, and will not invent a cause for a "
                "payment that did not fail"
            )

        events = self._deduplicate(trace_input.events)
        ordered = sorted(
            events,
            key=lambda e: (e.occurred_at, e.sequence, e.delivery_attempt),
        )

        # 1. Build the causal graph and walk it backward from the terminal node.
        graph = self._build_graph(ordered)
        causal_path = self._reverse_bfs(graph, ordered)
        hops = [self._to_hop(graph[node_id]) for node_id in causal_path]
        causal_chain = list(dict.fromkeys(hop.event_id for hop in hops))

        # 2. Detect telemetry gaps from the sequence numbers themselves.
        missing_sequences, chain_completeness = self._chain_completeness(ordered)

        # 3. Ground the root cause in the terminal failure event's error object.
        terminal_error = self._terminal_error(ordered)
        error_grounding = self._error_grounding(terminal_error)

        # 4. Score, then decide ambiguity.
        confidence = self._score(
            chain_completeness=chain_completeness,
            error_grounding=error_grounding,
            inherited=resolution.resolution_confidence,
            ignored_count=len(resolution.ignored_event_ids),
        )
        ambiguous, reasons = self._assess_ambiguity(
            resolution=resolution,
            ordered=ordered,
            missing_sequences=missing_sequences,
            chain_completeness=chain_completeness,
            terminal_error=terminal_error,
            error_grounding=error_grounding,
            confidence=confidence,
        )

        root_cause = self._root_cause(
            resolution=resolution,
            terminal_error=terminal_error,
            missing_sequences=missing_sequences,
            ambiguous=ambiguous,
            reasons=reasons,
        )

        result = TraceResult(
            payment_id=trace_input.payment_id,
            root_cause=root_cause,
            causal_chain=causal_chain,
            confidence=confidence,
            ambiguous=ambiguous,
            ambiguity_reasons=reasons,
            chain_completeness=chain_completeness,
            error_grounding=error_grounding,
            inherited_resolution_confidence=resolution.resolution_confidence,
            resolved_state=resolution.state,
            resolution_reason=resolution.resolution_reason.value,
            ignored_event_ids=list(resolution.ignored_event_ids),
            missing_sequences=missing_sequences,
            causal_hops=hops,
            grounded_error=terminal_error,
            traced_at=now,
        )

        log_event(
            logger,
            "failure_traced",
            payment_id=result.payment_id,
            state=result.resolved_state.value,
            root_cause=result.root_cause,
            confidence=result.confidence,
            ambiguous=result.ambiguous,
            chain_length=len(result.causal_chain),
            ambiguity_reasons=result.ambiguity_reasons,
        )
        return result

    # -- graph -------------------------------------------------------------
    @staticmethod
    def _node_id(event: WebhookEvent) -> str:
        return f"{event.event_id}#{event.delivery_attempt}"

    @staticmethod
    def _deduplicate(events: Sequence[WebhookEvent]) -> List[WebhookEvent]:
        """Collapse byte-identical redeliveries, keeping distinct attempts.

        A duplicate delivery is a real causal node (it is a webhook attempt
        that happened), so attempts are kept apart; only exact repeats of the
        same attempt are dropped.
        """
        seen: Set[Tuple] = set()
        unique: List[WebhookEvent] = []
        for event in events:
            key = (
                event.entity_id,
                event.sequence,
                event.occurred_at.isoformat(),
                event.delivery_attempt,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(event)
        return unique

    def _build_graph(self, ordered: Sequence[WebhookEvent]) -> Dict[str, "_Node"]:
        """Build the causal DAG.

        Two edge kinds, both pointing backward in time:
          * retry edge      -- attempt k of event E follows attempt k-1 of E
          * progression edge -- event E follows the last attempt of event E-1
        """
        graph: Dict[str, _Node] = {}
        attempts_by_event: Dict[str, List[WebhookEvent]] = {}
        for event in ordered:
            attempts_by_event.setdefault(event.event_id, []).append(event)

        # Distinct logical events, in causal order.
        logical_order: List[str] = list(dict.fromkeys(e.event_id for e in ordered))

        for index, event_id in enumerate(logical_order):
            attempts = sorted(
                attempts_by_event[event_id], key=lambda e: e.delivery_attempt
            )
            previous_event_last_node: Optional[str] = None
            if index > 0:
                prior_attempts = sorted(
                    attempts_by_event[logical_order[index - 1]],
                    key=lambda e: e.delivery_attempt,
                )
                previous_event_last_node = self._node_id(prior_attempts[-1])

            for attempt_index, event in enumerate(attempts):
                node_id = self._node_id(event)
                predecessors: List[str] = []
                if attempt_index > 0:
                    # retry edge
                    predecessors.append(self._node_id(attempts[attempt_index - 1]))
                elif previous_event_last_node is not None:
                    # progression edge
                    predecessors.append(previous_event_last_node)
                graph[node_id] = _Node(
                    node_id=node_id, event=event, predecessors=predecessors
                )
        return graph

    def _reverse_bfs(
        self, graph: Dict[str, "_Node"], ordered: Sequence[WebhookEvent]
    ) -> List[str]:
        """Breadth-first walk backward from the terminal node.

        Returns node ids in causal order (root first), which is the reverse of
        the discovery order.
        """
        if not ordered:
            return []

        terminal = self._node_id(ordered[-1])
        discovered: List[str] = []
        seen: Set[str] = {terminal}
        queue: deque = deque([terminal])

        while queue:
            node_id = queue.popleft()
            discovered.append(node_id)
            for predecessor in graph[node_id].predecessors:
                if predecessor not in seen:
                    seen.add(predecessor)
                    queue.append(predecessor)

        # Discovery ran terminal -> root; present it root -> terminal.
        return list(reversed(discovered))

    def _to_hop(self, node: "_Node") -> CausalHop:
        event = node.event
        return CausalHop(
            node_id=node.node_id,
            event_id=event.event_id,
            event=event.event.value,
            sequence=event.sequence,
            delivery_attempt=event.delivery_attempt,
            occurred_at=event.occurred_at,
            implied_state=from_event_name(event.event),
            is_duplicate_delivery=event.is_duplicate_delivery,
            has_error_object=event.error is not None,
        )

    # -- completeness ------------------------------------------------------
    @staticmethod
    def _chain_completeness(
        ordered: Sequence[WebhookEvent],
    ) -> Tuple[List[int], float]:
        """Detect telemetry gaps straight from the sequence numbers.

        The gateway assigns per-entity sequence numbers starting at 1, so the
        events that should be present are 1..max(sequence). Anything absent
        from that range is missing telemetry, whether a hole in the middle or a
        chain that never reaches the origin.
        """
        if not ordered:
            return [], 0.0
        present = {event.sequence for event in ordered}
        highest = max(present)
        expected = set(range(1, highest + 1))
        missing = sorted(expected - present)
        completeness = len(present) / len(expected) if expected else 0.0
        return missing, round(completeness, 4)

    # -- grounding ---------------------------------------------------------
    @staticmethod
    def _terminal_error(ordered: Sequence[WebhookEvent]) -> Optional[ErrorObject]:
        """The error object on the most recent payment.failed event.

        Only `payment.failed` carries the failure's own error object; taking it
        from any other event would attribute a cause to the wrong step.
        """
        for event in reversed(list(ordered)):
            if event.event is WebhookEventName.PAYMENT_FAILED and event.error is not None:
                return event.error
        return None

    @staticmethod
    def _error_grounding(error: Optional[ErrorObject]) -> float:
        """Fraction of the (source, step, reason) triplet actually populated.

        Razorpay already provides this triplet as a causal-chain marker. If it
        is not fully populated the diagnosis is not fully grounded, and the
        score has to say so.
        """
        if error is None:
            return 0.0
        values = (
            error.source.value if error.source is not None else "",
            error.step,
            error.reason,
        )
        populated = sum(1 for value in values if value and str(value).strip())
        return round(populated / 3.0, 4)

    # -- scoring -----------------------------------------------------------
    @staticmethod
    def _score(
        *,
        chain_completeness: float,
        error_grounding: float,
        inherited: float,
        ignored_count: int,
    ) -> float:
        confidence = chain_completeness * error_grounding * inherited
        confidence -= PENALTY_PER_IGNORED_EVENT * ignored_count
        return round(max(0.0, min(1.0, confidence)), 4)

    # -- ambiguity ---------------------------------------------------------
    def _assess_ambiguity(
        self,
        *,
        resolution: StateResolution,
        ordered: Sequence[WebhookEvent],
        missing_sequences: List[int],
        chain_completeness: float,
        terminal_error: Optional[ErrorObject],
        error_grounding: float,
        confidence: float,
    ) -> Tuple[bool, List[str]]:
        """Every rule that can force `ambiguous: true`, each recorded by name.

        Structural rules come first and are independent of the score: a gap in
        the chain is ambiguous even if everything else looks excellent. The
        confidence threshold is only a backstop.
        """
        reasons: List[str] = []

        if not ordered:
            reasons.append(
                "no_delivered_events: the payment has no webhook evidence at all, "
                "so no causal chain can be built"
            )

        if missing_sequences:
            reasons.append(
                "chain_gap_missing_telemetry: sequence(s) "
                f"{missing_sequences} are absent from the event chain; reporting "
                "the hole rather than returning a shorter chain that looks complete"
            )

        if resolution.state is CanonicalState.FAILED and terminal_error is None:
            reasons.append(
                "no_error_object_on_failure: resolved FAILED but no payment.failed "
                "event carries an error object, so source/step/reason cannot be quoted"
            )

        if terminal_error is not None and error_grounding < 1.0:
            reasons.append(
                f"incomplete_error_triplet: only {error_grounding:.0%} of "
                "(source, step, reason) is populated on the failing event"
            )

        if resolution.state is CanonicalState.PENDING_WEBHOOK:
            reasons.append(
                "pending_webhook_state: the resolver could not confirm an outcome "
                "from delivered webhooks; a status-endpoint query is required "
                "before any recovery action"
            )

        # --- continuity with the resolver's own findings ------------------
        if resolution.ignored_event_ids:
            reasons.append(
                "events_ignored_during_resolution: "
                f"{resolution.ignored_event_ids} could not be placed in the chain "
                "by the resolver; unexplained evidence is not a clean chain"
            )

        if resolution.resolution_reason in AMBIGUOUS_RESOLUTION_RULES:
            reasons.append(
                f"ambiguous_resolution_rule: resolver reported "
                f"'{resolution.resolution_reason.value}'"
            )

        if confidence < CONFIDENCE_AMBIGUITY_THRESHOLD:
            reasons.append(
                f"confidence_below_threshold: {confidence:.2f} < "
                f"{CONFIDENCE_AMBIGUITY_THRESHOLD:.2f}"
            )

        return bool(reasons), reasons

    # -- root cause --------------------------------------------------------
    @staticmethod
    def _root_cause(
        *,
        resolution: StateResolution,
        terminal_error: Optional[ErrorObject],
        missing_sequences: List[int],
        ambiguous: bool,
        reasons: List[str],
    ) -> str:
        """Quote the real error fields; never paraphrase, never invent.

        The grounded form is fixed by the
            "Failure at step: <step>, source: <source>, reason: <reason>"
        so the literal values stay greppable back to the originating event.
        """
        if terminal_error is not None:
            base = (
                f"Failure at step: {terminal_error.step}, "
                f"source: {terminal_error.source.value}, "
                f"reason: {terminal_error.reason}"
            )
            qualifiers: List[str] = []
            if missing_sequences:
                qualifiers.append(
                    f"chain incomplete, missing sequence(s) {missing_sequences}"
                )
            if resolution.ignored_event_ids:
                qualifiers.append(
                    f"{len(resolution.ignored_event_ids)} event(s) ignored during "
                    f"state resolution ({resolution.resolution_reason.value})"
                )
            if qualifiers:
                return f"{base}; {'; '.join(qualifiers)}"
            return base

        # No grounded error object. Say what is missing -- do not manufacture a
        # source/step/reason triplet that no event actually carried.
        detail = reasons[0].split(":", 1)[0] if reasons else "insufficient_evidence"
        return (
            f"Undetermined root cause for state {resolution.state.value}: no "
            f"error object with source/step/reason was delivered for this payment "
            f"({detail}). A status-endpoint query is required before any recovery "
            "action."
        )


class _Node:
    """Internal causal-graph node. Not part of the public schema."""

    __slots__ = ("node_id", "event", "predecessors")

    def __init__(self, node_id: str, event: WebhookEvent, predecessors: List[str]) -> None:
        self.node_id = node_id
        self.event = event
        self.predecessors = predecessors
