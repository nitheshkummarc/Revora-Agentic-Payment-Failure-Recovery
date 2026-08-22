"""Pydantic schemas for the MockPaymentGateway (Module 1).

Field names are taken verbatim from GROUND_TRUTH.md (Day 0, "Real error object
schema" and "Real webhook events to model"). Nothing here is invented: where
GROUND_TRUTH.md does not document a closed set of values (e.g. `step`, `code`),
the field is typed as a free string rather than a guessed enum, so that the
dataset generator in Module 7 is forced to supply real values instead of
picking from a fabricated list.

Every model sets extra="forbid" -- GROUND_TRUTH.md Day 1-2 lists "schema drift
in tool calls" as a known failure mode, with the mitigation being that a shape
mismatch must throw rather than silently pass through.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base for every inbound/outbound gateway schema."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Canonical states -- GROUND_TRUTH.md Day 1-2:
# "CREATED -> AUTHORIZED -> CAPTURED / FAILED / PENDING_WEBHOOK / REVERSED"
# --------------------------------------------------------------------------
class PaymentState(str, Enum):
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    PENDING_WEBHOOK = "PENDING_WEBHOOK"
    REVERSED = "REVERSED"


class SubscriptionState(str, Enum):
    """Only states traceable to GROUND_TRUTH.md's documented subscription
    webhook event names (subscription.activated, subscription.pending,
    subscription.halted) plus the initial CREATED. Razorpay's full
    subscription status list is deliberately NOT reproduced here, because
    GROUND_TRUTH.md does not enumerate it."""

    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    HALTED = "HALTED"


# --------------------------------------------------------------------------
# Webhook event names -- GROUND_TRUTH.md Day 0, "Real webhook events to model
# (use these exact names)". All eleven are listed; the gateway only produces
# transitions for the ones it models.
# --------------------------------------------------------------------------
class WebhookEventName(str, Enum):
    PAYMENT_AUTHORIZED = "payment.authorized"
    PAYMENT_CAPTURED = "payment.captured"
    PAYMENT_FAILED = "payment.failed"
    ORDER_PAID = "order.paid"
    SUBSCRIPTION_CHARGED = "subscription.charged"
    SUBSCRIPTION_PENDING = "subscription.pending"
    SUBSCRIPTION_ACTIVATED = "subscription.activated"
    SUBSCRIPTION_HALTED = "subscription.halted"
    REFUND_CREATED = "refund.created"
    REFUND_FAILED = "refund.failed"
    PAYMENT_DISPUTE_CREATED = "payment.dispute.created"


PAYMENT_EVENTS = frozenset(
    {
        WebhookEventName.PAYMENT_AUTHORIZED,
        WebhookEventName.PAYMENT_CAPTURED,
        WebhookEventName.PAYMENT_FAILED,
        WebhookEventName.ORDER_PAID,
        WebhookEventName.REFUND_CREATED,
        WebhookEventName.REFUND_FAILED,
        WebhookEventName.PAYMENT_DISPUTE_CREATED,
    }
)

SUBSCRIPTION_EVENTS = frozenset(
    {
        WebhookEventName.SUBSCRIPTION_CHARGED,
        WebhookEventName.SUBSCRIPTION_PENDING,
        WebhookEventName.SUBSCRIPTION_ACTIVATED,
        WebhookEventName.SUBSCRIPTION_HALTED,
    }
)


# --------------------------------------------------------------------------
# Error object -- GROUND_TRUTH.md Day 0, verbatim field names.
# --------------------------------------------------------------------------
class ErrorSource(str, Enum):
    """GROUND_TRUTH.md Day 0: source = who caused it
    (customer / bank / gateway / business / network)."""

    CUSTOMER = "customer"
    BANK = "bank"
    GATEWAY = "gateway"
    BUSINESS = "business"
    NETWORK = "network"


class ErrorObject(StrictModel):
    code: str
    description: str
    # GROUND_TRUTH.md's example carries field="otp"; Razorpay omits/nulls it
    # for errors not attributable to a single field.
    field: Optional[str] = None
    source: ErrorSource
    step: str
    reason: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(StrictModel):
    """The wire shape from GROUND_TRUTH.md: {"error": {...}}."""

    error: ErrorObject


# The ONLY error object hardcoded in this module. It is copied verbatim from
# GROUND_TRUTH.md Day 0's example so that nothing here is a fabricated
# Razorpay error code. Callers that need any other failure must pass their own
# error object explicitly -- the gateway will not guess one.
GROUND_TRUTH_EXAMPLE_ERROR = ErrorObject(
    code="BAD_REQUEST_ERROR",
    description="Payment failed due to incorrect OTP",
    field="otp",
    source=ErrorSource.CUSTOMER,
    step="payment_authentication",
    reason="incorrect_otp",
    metadata={},
)


# --------------------------------------------------------------------------
# Chaos configuration
# --------------------------------------------------------------------------
class ChaosMode(str, Enum):
    """GROUND_TRUTH.md Day 1-2: delayed webhook (0-45s), duplicate webhook,
    out-of-order webhook, silent drop (never fires), and the documented
    Failed->Authorized flip."""

    DELAYED_WEBHOOK = "delayed_webhook"
    DUPLICATE_WEBHOOK = "duplicate_webhook"
    OUT_OF_ORDER_WEBHOOK = "out_of_order_webhook"
    SILENT_DROP = "silent_drop"
    FAILED_AUTHORIZED_FLIP = "failed_authorized_flip"


class ChaosConfig(StrictModel):
    """Per-request chaos flag (GROUND_TRUTH.md Day 1-2: "Inject chaos via a
    config flag per request")."""

    modes: List[ChaosMode] = Field(default_factory=list)
    # The 0-45s window is documented; validated here so an out-of-range value
    # is rejected rather than silently clamped.
    delay_seconds: float = Field(default=0.0, ge=0.0, le=45.0)
    # How long after the FAILED event the documented late AUTHORIZED arrives.
    flip_after_seconds: float = Field(default=30.0, ge=0.0, le=45.0)
    # How long after the first delivery the duplicate copy arrives.
    duplicate_after_seconds: float = Field(default=1.0, ge=0.0, le=45.0)


# --------------------------------------------------------------------------
# Webhook event record (append-only history entry)
# --------------------------------------------------------------------------
class WebhookEvent(StrictModel):
    """One webhook delivery as the merchant would observe it.

    `sequence` + `occurred_at` are what downstream modules must order by --
    GROUND_TRUTH.md Day 1-2 is explicit that arrival order must NOT be used.
    """

    event_id: str
    entity_type: Literal["payment", "subscription"]
    entity_id: str
    event: WebhookEventName
    sequence: int
    # When the gateway actually made the state change.
    occurred_at: datetime
    # GROUND_TRUTH.md Days 11-13 names these two fields for the delay bucket.
    webhook_sent_at: datetime
    webhook_received_at: datetime
    delivery_attempt: int = 1
    is_duplicate_delivery: bool = False
    resulting_state: str
    error: Optional[ErrorObject] = None
    chaos_modes: List[ChaosMode] = Field(default_factory=list)


class TransitionLogEntry(StrictModel):
    """The "log of *why*" required by the Day 1-2 deliverable."""

    entity_id: str
    event_id: Optional[str] = None
    event: Optional[WebhookEventName] = None
    sequence: Optional[int] = None
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    applied: bool
    reason: str
    at: datetime


class DeliveryAttemptLogEntry(StrictModel):
    """Infra-level (HTTP) retry log -- NOT payment-recovery retry."""

    event_id: str
    attempt: int
    succeeded: bool
    backoff_seconds: float
    circuit_open: bool
    at: datetime


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
class PaymentRecord(StrictModel):
    payment_id: str
    order_id: Optional[str] = None
    amount: int  # paise -- Razorpay's integer-paise convention (50000 = Rs.500)
    currency: str = "INR"
    # What the gateway itself knows to be true. GET /payments/{id}/status
    # returns this -- a status query hits the gateway directly and is therefore
    # unaffected by webhook chaos. This is what makes a status check a real
    # disambiguation tool for later modules.
    state: PaymentState
    # What a merchant would believe from delivered webhooks alone. Diverges
    # from `state` under silent-drop / delay chaos. Module 2's resolver works
    # from the event history, not from this field.
    webhook_derived_state: PaymentState
    created_at: datetime
    updated_at: datetime
    error: Optional[ErrorObject] = None
    notes: Optional[str] = None
    subscription_id: Optional[str] = None


class SubscriptionRecord(StrictModel):
    subscription_id: str
    state: SubscriptionState
    failed_charge_attempts: int = 0
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Request / response schemas
# --------------------------------------------------------------------------
class CreatePaymentRequest(StrictModel):
    amount: int = Field(gt=0, description="Amount in paise (50000 = Rs.500)")
    currency: str = "INR"
    payment_id: Optional[str] = None
    order_id: Optional[str] = None
    subscription_id: Optional[str] = None
    # Free-text, customer-supplied. Treated as untrusted data everywhere
    # downstream (Module 4 sanitises it); the gateway only stores it.
    notes: Optional[str] = None


class CapturePaymentRequest(StrictModel):
    payment_id: str
    chaos: Optional[ChaosConfig] = None


class FailPaymentRequest(StrictModel):
    payment_id: str
    # Optional: when omitted the gateway uses GROUND_TRUTH.md's documented
    # example error object rather than inventing a code.
    error: Optional[ErrorObject] = None
    chaos: Optional[ChaosConfig] = None


class SimulateWebhookRequest(StrictModel):
    entity_id: str
    event: WebhookEventName
    error: Optional[ErrorObject] = None
    chaos: Optional[ChaosConfig] = None


class PaymentStatusResponse(StrictModel):
    payment: PaymentRecord
    event_history: List[WebhookEvent]
    transition_log: List[TransitionLogEntry]
    pending_webhook_count: int


class SubscriptionStatusResponse(StrictModel):
    subscription: SubscriptionRecord
    event_history: List[WebhookEvent]
    transition_log: List[TransitionLogEntry]


class SimulateWebhookResponse(StrictModel):
    entity_type: Literal["payment", "subscription"]
    entity_id: str
    delivered: List[WebhookEvent]
    dropped: List[WebhookEvent]
    scheduled: List[WebhookEvent]
    current_state: str
    webhook_derived_state: Optional[str] = None
