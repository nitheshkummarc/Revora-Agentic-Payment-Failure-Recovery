"""Canonical states for RecoverX (Module 2).

GROUND_TRUTH.md Day 1-2 defines the canonical state list verbatim:

    CREATED -> AUTHORIZED -> CAPTURED / FAILED / PENDING_WEBHOOK / REVERSED

Note on the deliberate duplication with `gateway/schemas.py::PaymentState`:
these two enums have the same members but model different things and must not
be merged. `PaymentState` is what a *gateway* holds; `CanonicalState` is what
the resolver *concludes from evidence*. Module 2 is forbidden from modifying
gateway files, and collapsing them would also let the resolver import a type
whose provenance is gateway-internal truth. They are converted explicitly at
the boundary instead (see `from_event_name`).
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Optional

from app.gateway.schemas import WebhookEventName

# GROUND_TRUTH.md Day 1-2, and Module 2 prompt requirement 2. Configurable:
# StateResolver accepts an override, this is only the default.
SILENCE_THRESHOLD_SECONDS: float = 300.0


class CanonicalState(str, Enum):
    """The five/six canonical states, matching GROUND_TRUTH.md exactly."""

    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    PENDING_WEBHOOK = "PENDING_WEBHOOK"
    REVERSED = "REVERSED"


# Which state each webhook event implies, and which prior states that
# transition is legal from. `None` means the event carries no state signal and
# is recorded without changing the resolution.
#
# FAILED is deliberately a legal predecessor of AUTHORIZED: that is the
# documented Razorpay Failed->Authorized flip (GROUND_TRUTH.md Day 0,
# documented behaviour 1), not corrupted evidence.
_TRANSITIONS: Dict[WebhookEventName, tuple] = {
    WebhookEventName.PAYMENT_AUTHORIZED: (
        frozenset({CanonicalState.CREATED, CanonicalState.PENDING_WEBHOOK, CanonicalState.FAILED}),
        CanonicalState.AUTHORIZED,
    ),
    WebhookEventName.PAYMENT_CAPTURED: (
        frozenset({CanonicalState.AUTHORIZED, CanonicalState.PENDING_WEBHOOK}),
        CanonicalState.CAPTURED,
    ),
    WebhookEventName.PAYMENT_FAILED: (
        frozenset({CanonicalState.CREATED, CanonicalState.PENDING_WEBHOOK, CanonicalState.AUTHORIZED}),
        CanonicalState.FAILED,
    ),
    WebhookEventName.REFUND_CREATED: (
        frozenset({CanonicalState.CAPTURED}),
        CanonicalState.REVERSED,
    ),
    WebhookEventName.ORDER_PAID: (None, None),
    WebhookEventName.REFUND_FAILED: (None, None),
    WebhookEventName.PAYMENT_DISPUTE_CREATED: (None, None),
}

# The three events whose absence defines "silence" for the threshold rule
# (Module 2 prompt requirement 2: "no `authorized`, `captured`, or `failed`
# webhook").
STATE_BEARING_EVENTS: FrozenSet[WebhookEventName] = frozenset(
    {
        WebhookEventName.PAYMENT_AUTHORIZED,
        WebhookEventName.PAYMENT_CAPTURED,
        WebhookEventName.PAYMENT_FAILED,
    }
)


def from_event_name(event: WebhookEventName) -> Optional[CanonicalState]:
    """The canonical state this event implies, or None if it carries no state
    signal."""
    return _TRANSITIONS.get(event, (None, None))[1]


def is_legal_transition(current: CanonicalState, event: WebhookEventName) -> bool:
    """Whether `event` may be applied while in `current`."""
    allowed, target = _TRANSITIONS.get(event, (None, None))
    if target is None:
        return True  # carries no state signal; never illegal
    if allowed is None:
        return True
    return current in allowed
