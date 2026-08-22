"""Pydantic schemas for the mock payment gateway.

Field names mirror Razorpay's published error and webhook payloads exactly.
Where Razorpay does not publish a closed set of values -- `step` and `code`
being the notable cases -- the field is typed as a free string rather than a
guessed enum, so callers must supply real values rather than pick from an
invented list.

Every model sets extra="forbid". Silent schema drift, where a renamed field is
accepted as a malformed-but-plausible payload, is the failure this guards
against: a shape mismatch raises instead of propagating.
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
# Canonical payment states:
# CREATED -> AUTHORIZED -> CAPTURED / FAILED / PENDING_WEBHOOK / REVERSED
# --------------------------------------------------------------------------
class PaymentState(str, Enum):
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    PENDING_WEBHOOK = "PENDING_WEBHOOK"
    REVERSED = "REVERSED"


class SubscriptionState(str, Enum):
    """States reachable from the subscription webhook events this gateway
    models, plus the initial CREATED. Razorpay's full subscription status list
    is deliberately not reproduced -- only states the modelled events can
    actually produce are represented."""

    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    HALTED = "HALTED"


# --------------------------------------------------------------------------
# Razorpay webhook event names, spelled exactly as the platform emits them.
# All eleven are listed; the gateway produces state transitions only for the
# subset it models.
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
# Error object, using Razorpay's field names verbatim.
# --------------------------------------------------------------------------
class ErrorSource(str, Enum):
    """Who caused the failure, as attributed by the gateway."""

    CUSTOMER = "customer"
    BANK = "bank"
    GATEWAY = "gateway"
    BUSINESS = "business"
    NETWORK = "network"


class ErrorObject(StrictModel):
    code: str
    description: str
    # Populated when the failure is attributable to one input field (for
    # example "otp"); omitted otherwise.
    field: Optional[str] = None
    source: ErrorSource
    step: str
    reason: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(StrictModel):
    """The wire shape Razorpay returns: {"error": {...}}."""

    error: ErrorObject


# The only error object hardcoded in this module, reproducing a documented
# Razorpay example so that no error code here is invented. Callers needing any
# other failure must pass their own error object; the gateway never guesses.
DOCUMENTED_EXAMPLE_ERROR = ErrorObject(
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
    """The five delivery faults this gateway can inject: a delayed webhook, a
    duplicate delivery, out-of-order delivery, a silent drop where the webhook
    never fires, and the documented Failed-to-Authorized flip."""

    DELAYED_WEBHOOK = "delayed_webhook"
    DUPLICATE_WEBHOOK = "duplicate_webhook"
    OUT_OF_ORDER_WEBHOOK = "out_of_order_webhook"
    SILENT_DROP = "silent_drop"
    FAILED_AUTHORIZED_FLIP = "failed_authorized_flip"


class ChaosConfig(StrictModel):
    """Per-request selection of which delivery faults to inject."""

    modes: List[ChaosMode] = Field(default_factory=list)
    # Bounded to the documented 0-45s window; an out-of-range value is
    # rejected rather than silently clamped.
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

    Consumers must order by `sequence` and `occurred_at`. Arrival order is
    unreliable under delayed and out-of-order delivery and must not be used.
    """

    event_id: str
    entity_type: Literal["payment", "subscription"]
    entity_id: str
    event: WebhookEventName
    sequence: int
    # When the gateway actually made the state change.
    occurred_at: datetime
    # The gap between these two is what a delayed delivery looks like.
    webhook_sent_at: datetime
    webhook_received_at: datetime
    delivery_attempt: int = 1
    is_duplicate_delivery: bool = False
    resulting_state: str
    error: Optional[ErrorObject] = None
    chaos_modes: List[ChaosMode] = Field(default_factory=list)


class TransitionLogEntry(StrictModel):
    """One entry explaining why a transition was or was not applied."""

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
    """Transport-level delivery attempt. Distinct from payment-recovery
    retry, which is a business decision made further up the pipeline."""

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
    # What the gateway itself knows to be true. The status endpoint returns
    # this: a status query reaches the gateway directly and is unaffected by
    # webhook faults, which is what makes it a genuine way to resolve an
    # ambiguous case.
    state: PaymentState
    # What a merchant would believe from delivered webhooks alone. Diverges
    # from `state` under silent-drop and delay faults. State resolution works
    # from the event history rather than this field, which is a lossy fold of
    # the same evidence.
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
    # Free-text and customer-supplied. Treated as untrusted data everywhere
    # downstream, where it is sanitised before use; the gateway only stores it.
    notes: Optional[str] = None


class CapturePaymentRequest(StrictModel):
    payment_id: str
    chaos: Optional[ChaosConfig] = None


class FailPaymentRequest(StrictModel):
    payment_id: str
    # When omitted the gateway falls back to a documented example error
    # object rather than inventing a code.
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
