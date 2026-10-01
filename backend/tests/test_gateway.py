"""Mock payment gateway tests.

Covers: the normal happy path, each chaos mode individually, and duplicate
webhook idempotency, per the

Time is driven by an injectable FakeClock rather than real sleeps, so a
documented 45-second webhook delay is exercised exactly as written without the
test suite taking 45 seconds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest
from fastapi.testclient import TestClient

from app.core.config import GatewaySettings
from app.gateway.chaos import ChaosInjector
from app.gateway.mock_gateway import (
    MockPaymentGateway,
    WebhookDeliveryClient,
    get_gateway,
)
from app.gateway.schemas import (
    CapturePaymentRequest,
    ChaosConfig,
    ChaosMode,
    CreatePaymentRequest,
    ErrorObject,
    ErrorSource,
    FailPaymentRequest,
    PaymentState,
    SimulateWebhookRequest,
    SubscriptionState,
    WebhookEvent,
    WebhookEventName,
)
from app.main import app

START = datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic clock. `advance` moves logical time forward."""

    def __init__(self, start: datetime = START) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> datetime:
        self.t = self.t + timedelta(seconds=seconds)
        return self.t


class ScriptedRandom:
    """random()-compatible stub returning a fixed script of values."""

    def __init__(self, values: List[float]) -> None:
        self._values = list(values)

    def random(self) -> float:
        return self._values.pop(0) if self._values else 1.0


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def gateway(clock: FakeClock) -> MockPaymentGateway:
    """Gateway with simulated network flakiness switched off, so chaos tests
    measure the chaos mode under test and nothing else."""
    settings = GatewaySettings(webhook_delivery_failure_rate=0.0)
    return MockPaymentGateway(settings=settings, clock=clock)


@pytest.fixture()
def client() -> TestClient:
    get_gateway().reset()
    return TestClient(app)


def _create(gateway: MockPaymentGateway, payment_id: str = "pay_test_1", amount: int = 50000):
    return gateway.create_payment(
        CreatePaymentRequest(payment_id=payment_id, amount=amount, currency="INR")
    )


# --------------------------------------------------------------------------
# Documented-value conformance
# --------------------------------------------------------------------------
def test_canonical_states_are_the_documented_set():
    """The canonical state list is fixed; drift here breaks every consumer."""
    assert {s.value for s in PaymentState} == {
        "CREATED",
        "AUTHORIZED",
        "CAPTURED",
        "FAILED",
        "PENDING_WEBHOOK",
        "REVERSED",
    }


def test_webhook_event_names_are_the_documented_set():
    """Event names must match what Razorpay actually emits, exactly."""
    assert {e.value for e in WebhookEventName} == {
        "payment.authorized",
        "payment.captured",
        "payment.failed",
        "order.paid",
        "subscription.charged",
        "subscription.pending",
        "subscription.activated",
        "subscription.halted",
        "refund.created",
        "refund.failed",
        "payment.dispute.created",
    }


def test_error_object_uses_real_field_names():
    err = ErrorObject(
        code="BAD_REQUEST_ERROR",
        description="Payment failed due to incorrect OTP",
        field="otp",
        source=ErrorSource.CUSTOMER,
        step="payment_authentication",
        reason="incorrect_otp",
    )
    assert set(err.model_dump().keys()) == {
        "code",
        "description",
        "field",
        "source",
        "step",
        "reason",
        "metadata",
    }


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_happy_path_create_authorize_capture(gateway: MockPaymentGateway):
    record = _create(gateway)
    assert record.state is PaymentState.CREATED

    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED)
    )
    assert gateway.payments["pay_test_1"].state is PaymentState.AUTHORIZED

    status = gateway.capture_payment(CapturePaymentRequest(payment_id="pay_test_1"))
    assert status.payment.state is PaymentState.CAPTURED
    assert status.payment.webhook_derived_state is PaymentState.CAPTURED
    assert [e.event.value for e in status.event_history] == [
        "payment.authorized",
        "payment.captured",
    ]
    assert status.pending_webhook_count == 0


def test_repeated_creates_without_a_payment_id_mint_distinct_records(
    gateway: MockPaymentGateway,
):
    """Documented, not a gap: omitting payment_id has no dedup key to check
    against, so two calls create two payments -- real Razorpay behaviour,
    where the merchant never chooses the id. A caller that does supply
    payment_id gets the 409 tested elsewhere on replay."""
    first = gateway.create_payment(CreatePaymentRequest(amount=50000, currency="INR"))
    second = gateway.create_payment(CreatePaymentRequest(amount=50000, currency="INR"))

    assert first.payment_id != second.payment_id
    assert len(gateway.payments) == 2


def test_happy_path_over_http(client: TestClient):
    created = client.post(
        "/payments/create", json={"payment_id": "pay_http_1", "amount": 50000}
    )
    assert created.status_code == 200, created.text
    assert created.json()["state"] == "CREATED"

    authorized = client.post(
        "/webhooks/simulate",
        json={"entity_id": "pay_http_1", "event": "payment.authorized"},
    )
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["current_state"] == "AUTHORIZED"

    captured = client.post("/payments/capture", json={"payment_id": "pay_http_1"})
    assert captured.status_code == 200, captured.text
    assert captured.json()["payment"]["state"] == "CAPTURED"

    status = client.get("/payments/pay_http_1/status")
    assert status.status_code == 200
    assert status.json()["payment"]["state"] == "CAPTURED"


def test_reversed_state_via_refund(gateway: MockPaymentGateway):
    _create(gateway)
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED)
    )
    gateway.capture_payment(CapturePaymentRequest(payment_id="pay_test_1"))
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.REFUND_CREATED)
    )
    assert gateway.payments["pay_test_1"].state is PaymentState.REVERSED


def test_fail_without_explicit_error_uses_documented_example(gateway: MockPaymentGateway):
    """The gateway never invents an error code -- it falls back to the example
    documented verbatim in """
    _create(gateway)
    status = gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    assert status.payment.state is PaymentState.FAILED
    err = status.payment.error
    assert err is not None
    assert err.code == "BAD_REQUEST_ERROR"
    assert err.source is ErrorSource.CUSTOMER
    assert err.step == "payment_authentication"
    assert err.reason == "incorrect_otp"


# --------------------------------------------------------------------------
# Schema drift -- extra="forbid"
# --------------------------------------------------------------------------
def test_subscription_status_over_http(client: TestClient):
    """A subscription that can be driven over HTTP can be queried over HTTP."""
    assert client.get("/subscriptions/sub_http_1/status").status_code == 404
    client.post(
        "/webhooks/simulate",
        json={"entity_id": "sub_http_1", "event": "subscription.activated"},
    )
    response = client.get("/subscriptions/sub_http_1/status")
    assert response.status_code == 200
    assert response.json()["subscription"]["state"] == "ACTIVE"


def test_unknown_field_is_rejected_not_silently_accepted(client: TestClient):
    """A malformed-but-plausible payload must raise rather than pass through."""
    response = client.post(
        "/payments/create",
        json={"amount": 50000, "amount_in_rupees": 500},
    )
    assert response.status_code == 422


def test_unknown_field_rejected_on_error_object(client: TestClient):
    client.post("/payments/create", json={"payment_id": "pay_http_2", "amount": 50000})
    response = client.post(
        "/payments/fail",
        json={
            "payment_id": "pay_http_2",
            "error": {
                "code": "BAD_REQUEST_ERROR",
                "description": "Payment failed due to incorrect OTP",
                "field": "otp",
                "source": "customer",
                "step": "payment_authentication",
                "reason": "incorrect_otp",
                "error_code": "LEGACY_FLAT_FIELD",
            },
        },
    )
    assert response.status_code == 422


def test_delay_outside_documented_window_is_rejected():
    """The documented delay window is 0-45s; anything outside it is rejected."""
    with pytest.raises(ValueError):
        ChaosConfig(modes=[ChaosMode.DELAYED_WEBHOOK], delay_seconds=60)


# --------------------------------------------------------------------------
# Chaos mode 1: delayed webhook (0-45s)
# --------------------------------------------------------------------------
def test_chaos_delayed_webhook(gateway: MockPaymentGateway, clock: FakeClock):
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(modes=[ChaosMode.DELAYED_WEBHOOK], delay_seconds=45),
        )
    )

    # Before the delay elapses: the gateway knows it failed, but nothing has
    # been delivered, so the merchant-observable view is still in flight.
    before = gateway.get_payment_status("pay_test_1")
    assert before.payment.state is PaymentState.FAILED
    assert before.payment.webhook_derived_state is PaymentState.PENDING_WEBHOOK
    assert before.event_history == []
    assert before.pending_webhook_count == 1

    clock.advance(45)
    after = gateway.get_payment_status("pay_test_1")
    assert after.pending_webhook_count == 0
    assert len(after.event_history) == 1
    event = after.event_history[0]
    assert event.event is WebhookEventName.PAYMENT_FAILED
    gap = (event.webhook_received_at - event.webhook_sent_at).total_seconds()
    assert gap == pytest.approx(45.0)
    assert after.payment.webhook_derived_state is PaymentState.FAILED


# --------------------------------------------------------------------------
# Chaos mode 2: duplicate webhook delivery
# --------------------------------------------------------------------------
def test_chaos_duplicate_webhook_delivery(gateway: MockPaymentGateway, clock: FakeClock):
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(modes=[ChaosMode.DUPLICATE_WEBHOOK], duplicate_after_seconds=1),
        )
    )
    clock.advance(2)
    status = gateway.get_payment_status("pay_test_1")

    # Both deliveries are visible in the append-only history...
    assert len(status.event_history) == 2
    assert status.event_history[0].event_id == status.event_history[1].event_id
    assert status.event_history[1].is_duplicate_delivery is True
    assert status.event_history[1].delivery_attempt == 2


def test_duplicate_webhook_does_not_double_transition(
    gateway: MockPaymentGateway, clock: FakeClock
):
    """Idempotency: replaying the same event must not change state twice.

    Dedupe keys off (entity, sequence, occurred_at) -- the event's own
    timestamp and sequence, never arrival order.
    """
    _create(gateway)
    gateway.simulate_webhook(
        SimulateWebhookRequest(
            entity_id="pay_test_1",
            event=WebhookEventName.PAYMENT_AUTHORIZED,
            chaos=ChaosConfig(
                modes=[ChaosMode.DUPLICATE_WEBHOOK], duplicate_after_seconds=1
            ),
        )
    )
    clock.advance(2)
    status = gateway.get_payment_status("pay_test_1")

    assert status.payment.state is PaymentState.AUTHORIZED
    assert status.payment.webhook_derived_state is PaymentState.AUTHORIZED

    applied = [
        t
        for t in status.transition_log
        if t.applied and t.reason == "webhook_derived_transition"
    ]
    assert len(applied) == 1, "state transitioned more than once for one logical event"

    ignored = [
        t for t in status.transition_log if t.reason == "duplicate_delivery_ignored_idempotent"
    ]
    assert len(ignored) == 1
    assert ignored[0].event_id == applied[0].event_id


def test_replaying_an_identical_event_is_a_no_op(gateway: MockPaymentGateway):
    """Direct replay of an already-delivered event, bypassing chaos."""
    _create(gateway)
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED)
    )
    delivered = gateway.event_history["pay_test_1"][0]
    replay = delivered.model_copy(deep=True)

    gateway._deliver(replay)  # same event_id, sequence and occurred_at

    assert gateway.payments["pay_test_1"].state is PaymentState.AUTHORIZED
    assert gateway.payments["pay_test_1"].webhook_derived_state is PaymentState.AUTHORIZED
    assert (
        sum(
            1
            for t in gateway.transition_log["pay_test_1"]
            if t.applied and t.reason == "webhook_derived_transition"
        )
        == 1
    )


# --------------------------------------------------------------------------
# Chaos mode 3: out-of-order webhook delivery
# --------------------------------------------------------------------------
def test_chaos_out_of_order_reverses_arrival_only():
    """Unit-level: the injector reverses arrival order while leaving each
    event's own sequence and occurred_at untouched."""
    events = [
        WebhookEvent(
            event_id=f"evt_pay_x_{seq}",
            entity_type="payment",
            entity_id="pay_x",
            event=WebhookEventName.PAYMENT_FAILED
            if seq == 1
            else WebhookEventName.PAYMENT_AUTHORIZED,
            sequence=seq,
            occurred_at=START + timedelta(seconds=seq),
            webhook_sent_at=START + timedelta(seconds=seq),
            webhook_received_at=START + timedelta(seconds=seq),
            resulting_state="FAILED" if seq == 1 else "AUTHORIZED",
        )
        for seq in (1, 2)
    ]
    injector = ChaosInjector(ChaosConfig(modes=[ChaosMode.OUT_OF_ORDER_WEBHOOK]))
    plan = injector.apply(
        events,
        now=START + timedelta(seconds=10),
        next_sequence=lambda: 99,
        new_event_id=lambda entity_id, seq: f"evt_{entity_id}_{seq}",
    )

    arrival_order = [sd.event.sequence for sd in plan.scheduled]
    assert arrival_order == [2, 1], "higher-sequence event must arrive first"
    assert [sd.event.occurred_at for sd in plan.scheduled] == [
        START + timedelta(seconds=2),
        START + timedelta(seconds=1),
    ]


def test_out_of_order_delivery_does_not_walk_state_backwards(
    gateway: MockPaymentGateway, clock: FakeClock
):
    """Integration: with two events reversed on the wire, ordering by sequence
    (not arrival) keeps the derived state correct."""
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(
                modes=[ChaosMode.FAILED_AUTHORIZED_FLIP, ChaosMode.OUT_OF_ORDER_WEBHOOK],
                flip_after_seconds=30,
            ),
        )
    )
    clock.advance(60)
    status = gateway.get_payment_status("pay_test_1")

    arrival_order = [e.sequence for e in status.event_history]
    assert arrival_order == [2, 1], "expected reversed arrival"
    assert status.payment.webhook_derived_state is PaymentState.AUTHORIZED
    superseded = [t for t in status.transition_log if t.reason.startswith("out_of_order_superseded")]
    assert len(superseded) == 1


# --------------------------------------------------------------------------
# Chaos mode 4: silent drop (webhook never fires)
# --------------------------------------------------------------------------
def test_chaos_silent_drop(gateway: MockPaymentGateway, clock: FakeClock):
    """payment.failed does not
    fire when the failure happens during authentication of a first attempt.
    Silence must be a signal, not an absence of one."""
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(modes=[ChaosMode.SILENT_DROP]),
        )
    )
    clock.advance(600)
    status = gateway.get_payment_status("pay_test_1")

    # Nothing was ever delivered: no history, nothing in flight.
    assert status.event_history == []
    assert status.pending_webhook_count == 0
    # The merchant-observable view is stuck at CREATED...
    assert status.payment.webhook_derived_state is PaymentState.CREATED
    # ...while a direct status query reveals the truth. This is what makes a
    # status check a real disambiguation tool for later modules.
    assert status.payment.state is PaymentState.FAILED

    dropped = [
        t
        for t in status.transition_log
        if t.reason == "webhook_silently_dropped_never_delivered"
    ]
    assert len(dropped) == 1
    assert dropped[0].event is WebhookEventName.PAYMENT_FAILED


# --------------------------------------------------------------------------
# Chaos mode 5: documented Failed -> Authorized flip
# --------------------------------------------------------------------------
def test_chaos_failed_authorized_flip_appends_to_history(
    gateway: MockPaymentGateway, clock: FakeClock
):
    """

    the flip must APPEND to the per-payment_id
    event history rather than overwriting state in place. State resolution needs
    both the FAILED and the later AUTHORIZED entry, each timestamped.
    """
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(
                modes=[ChaosMode.FAILED_AUTHORIZED_FLIP], flip_after_seconds=30
            ),
        )
    )

    mid = gateway.get_payment_status("pay_test_1")
    assert mid.payment.state is PaymentState.FAILED
    assert [e.event.value for e in mid.event_history] == ["payment.failed"]

    clock.advance(30)
    after = gateway.get_payment_status("pay_test_1")

    # Both entries present, in chronological order, neither overwritten.
    assert [e.event.value for e in after.event_history] == [
        "payment.failed",
        "payment.authorized",
    ]
    failed_event, authorized_event = after.event_history
    assert authorized_event.occurred_at > failed_event.occurred_at
    assert authorized_event.sequence > failed_event.sequence

    # The gateway's own state followed the later event.
    assert after.payment.state is PaymentState.AUTHORIZED

    flip_log = [t for t in after.transition_log if t.reason == "late_authorization_flip"]
    assert len(flip_log) == 1
    assert flip_log[0].from_state == "FAILED"
    assert flip_log[0].to_state == "AUTHORIZED"


def test_flip_reaches_gateway_truth_even_when_its_webhook_is_dropped(
    gateway: MockPaymentGateway, clock: FakeClock
):
    """Truth is what the gateway knows, not what the merchant was told. A
    silently dropped flip must still move truth once its moment passes, or the
    status query would confirm a failure that did not happen."""
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(
                modes=[ChaosMode.FAILED_AUTHORIZED_FLIP, ChaosMode.SILENT_DROP],
                flip_after_seconds=30,
            ),
        )
    )
    assert gateway.get_payment_status("pay_test_1").payment.state is PaymentState.FAILED

    clock.advance(30)
    after = gateway.get_payment_status("pay_test_1")
    assert after.payment.state is PaymentState.AUTHORIZED
    # The merchant heard nothing: the failure and the flip were both dropped.
    assert after.event_history == []
    assert after.payment.webhook_derived_state is PaymentState.CREATED


def test_flip_reaches_gateway_truth_when_delivery_always_fails(clock: FakeClock):
    """Same guarantee under transport failure rather than a chaos drop."""
    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=1.0), clock=clock
    )
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(
                modes=[ChaosMode.FAILED_AUTHORIZED_FLIP], flip_after_seconds=30
            ),
        )
    )
    clock.advance(30)
    after = gateway.get_payment_status("pay_test_1")
    assert after.payment.state is PaymentState.AUTHORIZED
    assert after.event_history == []


def test_reset_keeps_the_same_lock_and_rewinds_state(gateway: MockPaymentGateway):
    """Replacing the lock inside `reset` would let a thread already queued on
    the old lock run concurrently with one taking the new lock."""
    lock_before = gateway._lock
    _create(gateway)
    gateway.reset()
    assert gateway._lock is lock_before
    assert gateway.payments == {}
    # The id counter and delivery RNG start over, so a reset gateway behaves
    # exactly like a fresh one.
    fresh = MockPaymentGateway(settings=gateway.settings, clock=gateway._clock)
    assert gateway._new_payment_id() == fresh._new_payment_id()
    assert gateway.delivery_client._rng.random() == fresh.delivery_client._rng.random()


def test_status_snapshot_is_a_copy_not_the_live_record(
    gateway: MockPaymentGateway,
):
    """A snapshot taken before an action must still read as it did before the
    action -- a live reference would make any before/after comparison compare
    an object with itself -- and a caller must not be able to edit the store."""
    created = _create(gateway)
    before = gateway.get_payment_status("pay_test_1")
    assert before.payment is not gateway.payments["pay_test_1"]
    assert created is not gateway.payments["pay_test_1"]

    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    assert before.payment.state is PaymentState.CREATED

    before.payment.state = PaymentState.CAPTURED
    assert gateway.payments["pay_test_1"].state is PaymentState.FAILED

    # The event history is the audit trail: editing a snapshot's copy must not
    # rewrite the stored one.
    after = gateway.get_payment_status("pay_test_1")
    after.event_history[0].resulting_state = "CAPTURED"
    assert gateway.event_history["pay_test_1"][0].resulting_state == "FAILED"
    after.transition_log[0].reason = "edited"
    assert gateway.transition_log["pay_test_1"][0].reason != "edited"


def test_simulate_webhook_does_not_report_an_undeliverable_event_as_delivered(
    clock: FakeClock,
):
    """A due webhook that exhausts its delivery attempts never reached the
    merchant, so the response must not claim it did."""
    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=1.0), clock=clock
    )
    _create(gateway)
    response = gateway.simulate_webhook(
        SimulateWebhookRequest(
            entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED
        )
    )
    assert response.delivered == []
    assert [e.event_id for e in response.dropped] == ["evt_pay_test_1_1"]
    assert gateway.get_payment_status("pay_test_1").event_history == []


def test_flip_truth_waits_for_its_own_moment_under_a_delayed_webhook(
    gateway: MockPaymentGateway, clock: FakeClock
):
    """A delayed webhook delays the merchant's view, not the fact itself."""
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(
                modes=[ChaosMode.FAILED_AUTHORIZED_FLIP, ChaosMode.DELAYED_WEBHOOK],
                flip_after_seconds=10,
                delay_seconds=40,
            ),
        )
    )
    clock.advance(10)
    at_flip = gateway.get_payment_status("pay_test_1")
    assert at_flip.payment.state is PaymentState.AUTHORIZED
    assert at_flip.event_history == []  # both webhooks still in flight

    clock.advance(40)
    landed = gateway.get_payment_status("pay_test_1")
    assert [e.event.value for e in landed.event_history] == [
        "payment.failed",
        "payment.authorized",
    ]
    assert landed.payment.webhook_derived_state is PaymentState.AUTHORIZED


# --------------------------------------------------------------------------
# Subscriptions: PENDING -> HALTED after exactly 3 failed charge attempts
# --------------------------------------------------------------------------
def _activate(gateway: MockPaymentGateway, subscription_id: str) -> None:
    gateway.simulate_webhook(
        SimulateWebhookRequest(
            entity_id=subscription_id, event=WebhookEventName.SUBSCRIPTION_ACTIVATED
        )
    )


def test_subscription_halts_after_three_failed_charges(gateway: MockPaymentGateway):
    """Subscriptions move to halted after exactly three charge-retry attempts."""
    _activate(gateway, "sub_test_1")
    for attempt in (1, 2, 3):
        gateway.simulate_webhook(
            SimulateWebhookRequest(
                entity_id="sub_test_1", event=WebhookEventName.SUBSCRIPTION_PENDING
            )
        )
        sub = gateway.subscriptions["sub_test_1"]
        assert sub.failed_charge_attempts == attempt
        if attempt < 3:
            assert sub.state is SubscriptionState.PENDING

    status = gateway.get_subscription_status("sub_test_1")
    assert status.subscription.state is SubscriptionState.HALTED
    assert status.subscription.failed_charge_attempts == 3
    assert [e.event.value for e in status.event_history] == [
        "subscription.activated",
        "subscription.pending",
        "subscription.pending",
        "subscription.pending",
        "subscription.halted",
    ]


def test_duplicate_subscription_pending_does_not_double_count(
    gateway: MockPaymentGateway, clock: FakeClock
):
    _activate(gateway, "sub_test_2")
    gateway.simulate_webhook(
        SimulateWebhookRequest(
            entity_id="sub_test_2",
            event=WebhookEventName.SUBSCRIPTION_PENDING,
            chaos=ChaosConfig(modes=[ChaosMode.DUPLICATE_WEBHOOK], duplicate_after_seconds=1),
        )
    )
    clock.advance(2)
    assert gateway.subscriptions["sub_test_2"].failed_charge_attempts == 1
    assert gateway.subscriptions["sub_test_2"].state is SubscriptionState.PENDING


def test_subscription_charged_resets_failure_counter(gateway: MockPaymentGateway):
    _activate(gateway, "sub_test_3")
    for _ in range(2):
        gateway.simulate_webhook(
            SimulateWebhookRequest(
                entity_id="sub_test_3", event=WebhookEventName.SUBSCRIPTION_PENDING
            )
        )
    gateway.simulate_webhook(
        SimulateWebhookRequest(
            entity_id="sub_test_3", event=WebhookEventName.SUBSCRIPTION_CHARGED
        )
    )
    sub = gateway.subscriptions["sub_test_3"]
    assert sub.failed_charge_attempts == 0
    assert sub.state is SubscriptionState.ACTIVE


# --------------------------------------------------------------------------
# Infra-level retry / circuit breaker (NOT payment-recovery retry)
# --------------------------------------------------------------------------
def _dummy_event() -> WebhookEvent:
    return WebhookEvent(
        event_id="evt_pay_retry_1",
        entity_type="payment",
        entity_id="pay_retry",
        event=WebhookEventName.PAYMENT_FAILED,
        sequence=1,
        occurred_at=START,
        webhook_sent_at=START,
        webhook_received_at=START,
        resulting_state="FAILED",
    )


def test_delivery_retries_with_backoff_then_succeeds(clock: FakeClock):
    settings = GatewaySettings(
        webhook_delivery_failure_rate=0.5, delivery_max_attempts=3, delivery_backoff_base_seconds=0.5
    )
    client = WebhookDeliveryClient(settings, clock, ScriptedRandom([0.0, 0.9]))
    assert client.deliver(_dummy_event()) is True
    assert [entry.attempt for entry in client.delivery_log] == [1, 2]
    assert client.delivery_log[0].succeeded is False
    assert client.delivery_log[0].backoff_seconds == 0.5
    assert client.delivery_log[1].succeeded is True
    assert client.consecutive_failures == 0


def test_circuit_breaker_opens_after_consecutive_failures(clock: FakeClock):
    from app.gateway.mock_gateway import CircuitOpenError

    settings = GatewaySettings(
        webhook_delivery_failure_rate=1.0,
        delivery_max_attempts=3,
        circuit_breaker_threshold=2,
    )
    client = WebhookDeliveryClient(settings, clock, ScriptedRandom([0.0, 0.0, 0.0]))
    with pytest.raises(CircuitOpenError):
        client.deliver(_dummy_event())
    assert client.circuit_open is True
    assert len(client.delivery_log) == 2


def test_gateway_records_undelivered_event_when_circuit_open(clock: FakeClock):
    settings = GatewaySettings(
        webhook_delivery_failure_rate=1.0,
        delivery_max_attempts=2,
        circuit_breaker_threshold=1,
    )
    gateway = MockPaymentGateway(settings=settings, clock=clock)
    _create(gateway)
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    status = gateway.get_payment_status("pay_test_1")

    assert status.event_history == []
    assert status.payment.state is PaymentState.FAILED  # gateway truth is unaffected
    assert any(t.reason.startswith("delivery_circuit_open") for t in status.transition_log)




def test_delivery_while_the_circuit_is_already_open_is_refused_not_attempted(
    clock: FakeClock,
):
    """The entry guard, as distinct from the guard that trips mid-retry.

    The existing breaker tests all stop at the moment it opens, which raises
    from inside the retry loop. This covers the call *after* that: the breaker
    is already open, and the question is whether a further delivery is quietly
    attempted anyway.
    """
    from app.gateway.mock_gateway import CircuitOpenError

    settings = GatewaySettings(
        webhook_delivery_failure_rate=1.0,
        delivery_max_attempts=2,
        circuit_breaker_threshold=1,
    )
    client = WebhookDeliveryClient(settings, clock, ScriptedRandom([0.0, 0.0, 0.0]))

    # First call trips the breaker; it raises from the retry guard.
    with pytest.raises(CircuitOpenError):
        client.deliver(_dummy_event())
    assert client.circuit_open is True
    attempts_after_trip = len(client.delivery_log)

    # Second call: refused at entry. The message distinguishes this guard from
    # the mid-retry one, which says "tripped during retry".
    with pytest.raises(CircuitOpenError) as caught:
        client.deliver(_dummy_event())
    assert "circuit breaker is open after" in str(caught.value)

    # The refusal is total: no further attempt is made, so the delivery log
    # does not grow. This is what "delivery stops entirely" has to mean.
    assert len(client.delivery_log) == attempts_after_trip


def test_circuit_stays_open_before_the_cooldown_elapses(clock: FakeClock):
    from app.gateway.mock_gateway import CircuitOpenError

    settings = GatewaySettings(
        webhook_delivery_failure_rate=1.0,
        delivery_max_attempts=1,
        circuit_breaker_threshold=1,
        circuit_breaker_cooldown_seconds=30.0,
    )
    client = WebhookDeliveryClient(settings, clock, ScriptedRandom([0.0, 0.0]))

    with pytest.raises(CircuitOpenError):
        client.deliver(_dummy_event())
    assert client.circuit_open is True

    clock.advance(29.0)
    with pytest.raises(CircuitOpenError) as caught:
        client.deliver(_dummy_event())
    assert "circuit breaker is open after" in str(caught.value)
    assert client.circuit_open is True


def test_circuit_closes_itself_once_the_cooldown_elapses(clock: FakeClock):
    """Without this, a failure burst disables delivery for the rest of the
    process's life -- nothing else in the system calls reset_circuit()."""
    from app.gateway.mock_gateway import CircuitOpenError

    settings = GatewaySettings(
        webhook_delivery_failure_rate=0.5,
        delivery_max_attempts=1,
        circuit_breaker_threshold=1,
        circuit_breaker_cooldown_seconds=30.0,
    )
    client = WebhookDeliveryClient(settings, clock, ScriptedRandom([0.0, 0.9]))

    with pytest.raises(CircuitOpenError):
        client.deliver(_dummy_event())
    assert client.circuit_open is True

    clock.advance(30.0)
    assert client.deliver(_dummy_event()) is True
    assert client.circuit_open is False
    assert client.consecutive_failures == 0


def test_gateway_reset_circuit_closes_the_breaker_under_its_own_lock(clock: FakeClock):
    """`delivery_client.reset_circuit()` has no lock of its own -- it is only
    safe today when reached through a `@_locked` gateway method. This is the
    safe entry point for a demo operator resetting a tripped breaker between
    runs, rather than reaching into `gateway.delivery_client` directly."""
    settings = GatewaySettings(
        webhook_delivery_failure_rate=1.0,
        delivery_max_attempts=1,
        circuit_breaker_threshold=1,
    )
    gateway = MockPaymentGateway(settings=settings, clock=clock)
    _create(gateway)
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    assert gateway.delivery_client.circuit_open is True

    gateway.reset_circuit()

    assert gateway.delivery_client.circuit_open is False
    assert gateway.delivery_client.consecutive_failures == 0


def test_concurrent_captures_of_the_same_payment_only_one_succeeds(
    clock: FakeClock, monkeypatch
):
    """Real contention, not just concurrent calls: N threads race to capture
    the SAME payment. capture_payment reads record.state, decides the
    transition is legal, and only then commits it several calls later inside
    _transition_payment_truth -- real work happens in that window
    (WebhookEvent construction, chaos injection, dict lookups). Without the
    lock serialising the whole call, more than one thread can pass its own
    outer check before any of them commits the AUTHORIZED->CAPTURED
    transition, and _deliver() appends a payment.captured history entry
    unconditionally once a thread reaches it -- regardless of whether the
    transition it triggers actually applies -- so more than one such entry
    for a payment that can only be captured once is the real, observable
    symptom of the race, not an exception.

    Relying on natural GIL-timing luck to hit that window turned out not to
    reproduce it (verified: even with the lock removed entirely, the
    unpatched version of this test passed 5/5 runs, because the window is a
    handful of microseconds and 20 threads mostly just ran it sequentially).
    A short sleep is injected into the exact window under test to force the
    interleaving deterministically, rather than hoping for it.

    An earlier version of this test spawned N threads each creating a
    payment with a *distinct* id -- no shared key at all, so it passed
    identically with the lock removed and proved nothing. This replaces it."""
    from app.gateway.mock_gateway import MockPaymentGateway as _Gateway
    import threading
    import time

    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=0.0), clock=clock
    )
    _create(gateway)
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED)
    )

    original_transition = _Gateway._transition_payment_truth

    def _slow_transition(self, event):
        if event.event is WebhookEventName.PAYMENT_CAPTURED:
            time.sleep(0.01)
        return original_transition(self, event)

    monkeypatch.setattr(_Gateway, "_transition_payment_truth", _slow_transition)

    thread_count = 8

    def _try_capture() -> None:
        try:
            gateway.capture_payment(CapturePaymentRequest(payment_id="pay_test_1"))
        except Exception:  # pragma: no cover - either outcome is fine here
            pass

    threads = [threading.Thread(target=_try_capture) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    captured_events = [
        e for e in gateway.event_history["pay_test_1"] if e.event is WebhookEventName.PAYMENT_CAPTURED
    ]
    assert len(captured_events) == 1
    assert gateway.payments["pay_test_1"].state is PaymentState.CAPTURED


def test_an_open_circuit_drops_later_webhooks_without_touching_gateway_truth(
    clock: FakeClock,
):
    """The consequence of the guard above, one level up.

    Truth and evidence part company here: the payment really did transition,
    but no webhook carrying that fact ever reaches the merchant. That is the
    whole reason a stalled breaker is dangerous rather than merely noisy.
    """
    settings = GatewaySettings(
        webhook_delivery_failure_rate=1.0,
        delivery_max_attempts=2,
        circuit_breaker_threshold=1,
    )
    gateway = MockPaymentGateway(settings=settings, clock=clock)
    _create(gateway)
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    assert gateway.delivery_client.circuit_open is True

    # A second, later event, with the breaker already open.
    gateway.simulate_webhook(
        SimulateWebhookRequest(
            entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED
        )
    )
    status = gateway.get_payment_status("pay_test_1")

    # Gateway truth advanced: the flip really happened.
    assert status.payment.state is PaymentState.AUTHORIZED
    # The merchant saw none of it -- not the failure, not the authorization.
    assert status.event_history == []
    assert status.payment.webhook_derived_state is PaymentState.CREATED
    # Every drop is recorded with the reason, so the silence is explicable
    # afterwards rather than indistinguishable from a payment that never moved.
    dropped = [t for t in status.transition_log if t.reason.startswith("delivery_circuit_open")]
    assert len(dropped) == 2
    assert "circuit breaker is open after" in dropped[1].reason


# --------------------------------------------------------------------------
# API-level error handling
# --------------------------------------------------------------------------
def test_status_of_unknown_payment_is_404(client: TestClient):
    assert client.get("/payments/pay_does_not_exist/status").status_code == 404


def test_illegal_capture_is_rejected(client: TestClient):
    client.post("/payments/create", json={"payment_id": "pay_http_3", "amount": 50000})
    response = client.post("/payments/capture", json={"payment_id": "pay_http_3"})
    assert response.status_code == 409


# --------------------------------------------------------------------------
# Four safety paths named as untested in README.md's "Known untested paths
# in the gateway" -- implemented and reachable, but nothing exercised them.
# --------------------------------------------------------------------------
def test_delivery_returns_false_when_the_retry_budget_is_exhausted_without_tripping_the_circuit(
    clock: FakeClock,
):
    """The circuit breaker and the exhausted-retry-budget branch are two
    different failure modes with two different observable outcomes:
    CircuitOpenError vs a plain `return False`. At the 5% default the
    breaker almost always trips first (threshold 5 < typical max_attempts
    run), so this branch never ran under any existing test. Reached here by
    setting the threshold above the number of attempts a single delivery can
    make, so every attempt fails and the loop simply runs out."""
    settings = GatewaySettings(
        webhook_delivery_failure_rate=1.0,
        delivery_max_attempts=2,
        circuit_breaker_threshold=5,
    )
    client_ = WebhookDeliveryClient(settings, clock, ScriptedRandom([0.0, 0.0]))

    result = client_.deliver(_dummy_event())

    assert result is False
    assert client_.circuit_open is False
    assert len(client_.delivery_log) == 2
    assert all(not entry.succeeded for entry in client_.delivery_log)


def test_illegal_truth_transition_is_refused_and_logged(clock: FakeClock, gateway: MockPaymentGateway):
    """capture_payment/fail_payment pre-check legality and raise before ever
    reaching the gateway-truth transition -- test_illegal_capture_is_rejected
    covers that outer guard. simulate_webhook has no equivalent pre-check, so
    an event implying an illegal transition reaches _transition_payment_truth
    directly, where the inner guard has to refuse it on its own rather than
    relying on a caller having already checked."""
    _create(gateway)  # state: CREATED

    # PAYMENT_CAPTURED is only legal from AUTHORIZED or PENDING_WEBHOOK.
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_CAPTURED)
    )
    status = gateway.get_payment_status("pay_test_1")

    assert status.payment.state is PaymentState.CREATED  # unchanged, not corrupted
    refused = [
        t for t in status.transition_log if t.reason == "illegal_transition_CREATED_to_CAPTURED"
    ]
    assert len(refused) == 1
    assert refused[0].applied is False


def test_illegal_derived_transition_is_refused_and_logged(clock: FakeClock, gateway: MockPaymentGateway):
    """The derived (merchant-visible) state has its own legality guard,
    separate from truth's, because the two can diverge under chaos: truth
    updates the instant an event happens, but the derived state only updates
    on delivery. A silently dropped AUTHORIZED webhook leaves derived stuck
    at CREATED while truth moves on to AUTHORIZED then CAPTURED -- so the
    later, cleanly-delivered CAPTURED webhook is legal for truth but illegal
    for a derived state that never left CREATED."""
    _create(gateway)
    gateway.simulate_webhook(
        SimulateWebhookRequest(
            entity_id="pay_test_1",
            event=WebhookEventName.PAYMENT_AUTHORIZED,
            chaos=ChaosConfig(modes=[ChaosMode.SILENT_DROP]),
        )
    )
    pre = gateway.get_payment_status("pay_test_1")
    assert pre.payment.state is PaymentState.AUTHORIZED  # truth moved on
    assert pre.payment.webhook_derived_state is PaymentState.CREATED  # derived did not

    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_CAPTURED)
    )
    status = gateway.get_payment_status("pay_test_1")

    assert status.payment.state is PaymentState.CAPTURED  # legal for truth, applied
    assert status.payment.webhook_derived_state is PaymentState.CREATED  # illegal for derived, refused
    refused = [
        t
        for t in status.transition_log
        if t.reason == "derived_illegal_transition_CREATED_to_CAPTURED"
    ]
    assert len(refused) == 1


def test_failing_a_payment_from_a_terminal_state_is_rejected(gateway: MockPaymentGateway):
    """Mirrors test_illegal_capture_is_rejected for the other mutating
    endpoint: once a payment is CAPTURED, PAYMENT_FAILED is not in its
    allowed source states either -- there is no path back from "already
    succeeded" to "failed"."""
    from app.gateway.mock_gateway import IllegalTransitionError

    _create(gateway)
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED)
    )
    gateway.capture_payment(CapturePaymentRequest(payment_id="pay_test_1"))

    with pytest.raises(IllegalTransitionError):
        gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))

    assert gateway.payments["pay_test_1"].state is PaymentState.CAPTURED  # unchanged


def test_capturing_an_already_captured_payment_is_rejected(gateway: MockPaymentGateway):
    """The specific guard the naive-baseline comparison (README's "duplicate-
    payment risk" section) depends on: a soft retry that reaches Execute
    against a payment gateway truth already shows as CAPTURED calls
    capture_payment again, and this is the refusal that stops it from
    silently succeeding a second time. Previously proven only end-to-end
    through the verify_mismatch_stale_success dataset rows, never directly."""
    from app.gateway.mock_gateway import IllegalTransitionError

    _create(gateway)
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED)
    )
    gateway.capture_payment(CapturePaymentRequest(payment_id="pay_test_1"))

    with pytest.raises(IllegalTransitionError, match="from state CAPTURED"):
        gateway.capture_payment(CapturePaymentRequest(payment_id="pay_test_1"))

    status = gateway.get_payment_status("pay_test_1")
    captured_events = [
        e for e in status.event_history if e.event is WebhookEventName.PAYMENT_CAPTURED
    ]
    assert len(captured_events) == 1  # the rejected second call left no trace of a second capture
    assert status.payment.state is PaymentState.CAPTURED


def test_a_silently_dropped_event_records_the_chaos_applied_to_it(
    gateway: MockPaymentGateway,
):
    """The dropped event is the audit record of the silence, so it must carry
    the mode that caused it."""
    _create(gateway)
    response = gateway.simulate_webhook(
        SimulateWebhookRequest(
            entity_id="pay_test_1",
            event=WebhookEventName.PAYMENT_AUTHORIZED,
            chaos=ChaosConfig(modes=[ChaosMode.SILENT_DROP]),
        )
    )
    assert [e.chaos_modes for e in response.dropped] == [[ChaosMode.SILENT_DROP]]


def test_an_empty_id_is_rejected_not_replaced(client: TestClient):
    """An empty payment_id used to be read as "omitted" and a fresh id minted,
    so the caller got back a payment under an id it never sent."""
    assert client.post("/payments/create", json={"payment_id": "", "amount": 50000}).status_code == 422
    assert (
        client.post(
            "/webhooks/simulate", json={"entity_id": "", "event": "subscription.activated"}
        ).status_code
        == 422
    )



# --------------------------------------------------------------------------
# Recovery retry: idempotent on its key, counted on the payment
# --------------------------------------------------------------------------
def _retry(gateway, key, payment_id="pay_test_1", discount=0):
    from app.gateway.schemas import RetryPaymentRequest

    return gateway.retry_payment(
        RetryPaymentRequest(payment_id=payment_id, idempotency_key=key, discount_amount=discount)
    )


def test_retrying_a_failed_payment_charges_a_new_attempt(gateway: MockPaymentGateway):
    _create(gateway)
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    charged = _retry(gateway, "k1")

    assert charged.payment.payment_id == "pay_test_1_attempt1"
    assert charged.payment.retry_of == "pay_test_1"
    assert charged.payment.state is PaymentState.CAPTURED
    assert charged.payment.captured_amount == 50000
    assert [e.event.value for e in charged.event_history] == [
        "payment.authorized",
        "payment.captured",
    ]

    original = gateway.get_payment_status("pay_test_1")
    assert original.payment.state is PaymentState.FAILED
    assert original.payment.recovery_attempts == 1
    assert original.payment.retry_attempt_ids == ["pay_test_1_attempt1"]
    assert [a.payment_id for a in original.retry_attempts] == ["pay_test_1_attempt1"]
    assert [e.event.value for e in original.event_history] == ["payment.failed"]


def test_an_approved_discount_comes_off_the_amount_charged(gateway: MockPaymentGateway):
    _create(gateway)
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    charged = _retry(gateway, "k1", discount=5000)
    assert charged.payment.amount == 45000
    assert charged.payment.captured_amount == 45000


def test_a_discount_not_below_the_amount_is_refused(gateway: MockPaymentGateway):
    from app.gateway.mock_gateway import InvalidRetryError

    _create(gateway)
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    with pytest.raises(InvalidRetryError):
        _retry(gateway, "k1", discount=50000)
    assert gateway.payments["pay_test_1"].recovery_attempts == 0
    assert gateway.payments["pay_test_1"].retry_attempt_ids == []


def test_an_authorized_payment_is_captured_rather_than_charged_again(
    gateway: MockPaymentGateway,
):
    _create(gateway)
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="pay_test_1", event=WebhookEventName.PAYMENT_AUTHORIZED)
    )
    charged = _retry(gateway, "k1")
    assert charged.payment.payment_id == "pay_test_1"
    assert charged.payment.state is PaymentState.CAPTURED
    assert gateway.payments["pay_test_1"].retry_attempt_ids == []


def test_a_payment_that_has_not_failed_cannot_be_retried(gateway: MockPaymentGateway):
    from app.gateway.mock_gateway import IllegalTransitionError

    _create(gateway)
    with pytest.raises(IllegalTransitionError):
        _retry(gateway, "k1")


def test_replaying_a_retry_key_does_not_charge_again(gateway: MockPaymentGateway):
    """A duplicated or retried request with the same key returns the first
    attempt's result: no new events, no new attempt counted."""
    _create(gateway)
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    first = _retry(gateway, "k1")
    replay = _retry(gateway, "k1")
    assert replay.payment.payment_id == first.payment.payment_id
    assert replay.payment.state is PaymentState.CAPTURED
    assert len(replay.event_history) == len(first.event_history)
    original = gateway.payments["pay_test_1"]
    assert original.recovery_attempts == 1
    assert original.retry_attempt_ids == ["pay_test_1_attempt1"]


def test_a_retry_key_cannot_be_reused_for_another_payment(gateway: MockPaymentGateway):
    from app.gateway.mock_gateway import IdempotencyConflictError

    _create(gateway, payment_id="pay_a")
    _create(gateway, payment_id="pay_b")
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_a"))
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_b"))
    _retry(gateway, "shared", payment_id="pay_a")
    with pytest.raises(IdempotencyConflictError):
        _retry(gateway, "shared", payment_id="pay_b")
    assert gateway.payments["pay_b"].state is PaymentState.FAILED


def test_a_captured_payment_cannot_be_retried(gateway: MockPaymentGateway):
    from app.gateway.mock_gateway import IllegalTransitionError

    _create(gateway)
    gateway.fail_payment(FailPaymentRequest(payment_id="pay_test_1"))
    _retry(gateway, "k1")
    with pytest.raises(IllegalTransitionError):
        _retry(gateway, "k2")
    assert gateway.payments["pay_test_1"].recovery_attempts == 1


def test_retry_over_http_maps_errors_to_status_codes(client: TestClient):
    client.post("/payments/create", json={"payment_id": "pay_http_r", "amount": 50000})
    client.post("/payments/fail", json={"payment_id": "pay_http_r"})
    ok = client.post("/payments/retry", json={"payment_id": "pay_http_r", "idempotency_key": "k"})
    assert ok.status_code == 200
    assert ok.json()["payment"]["state"] == "CAPTURED"
    again = client.post("/payments/retry", json={"payment_id": "pay_http_r", "idempotency_key": "k2"})
    assert again.status_code == 409
    missing = client.post("/payments/retry", json={"payment_id": "nope", "idempotency_key": "k3"})
    assert missing.status_code == 404



def test_out_of_order_never_delivers_an_event_before_it_happens(
    gateway: MockPaymentGateway, clock: FakeClock
):
    """Reversing arrival order must hold the earlier event back, not pull the
    later one forward: a webhook cannot arrive before the fact it reports."""
    _create(gateway)
    gateway.fail_payment(
        FailPaymentRequest(
            payment_id="pay_test_1",
            chaos=ChaosConfig(
                modes=[ChaosMode.FAILED_AUTHORIZED_FLIP, ChaosMode.OUT_OF_ORDER_WEBHOOK],
                flip_after_seconds=30,
            ),
        )
    )
    early = gateway.get_payment_status("pay_test_1")
    assert early.event_history == []
    assert early.payment.state is PaymentState.FAILED
    assert early.payment.webhook_derived_state is not PaymentState.AUTHORIZED

    clock.advance(31)
    later = gateway.get_payment_status("pay_test_1")
    for event in later.event_history:
        assert event.webhook_received_at >= event.occurred_at
    assert [e.sequence for e in later.event_history] == [2, 1]



@pytest.mark.parametrize(
    "setup, event",
    [
        ([], WebhookEventName.SUBSCRIPTION_PENDING),
        ([], WebhookEventName.SUBSCRIPTION_CHARGED),
        ([], WebhookEventName.SUBSCRIPTION_HALTED),
        ([WebhookEventName.SUBSCRIPTION_ACTIVATED], WebhookEventName.SUBSCRIPTION_ACTIVATED),
        ([WebhookEventName.SUBSCRIPTION_ACTIVATED], WebhookEventName.SUBSCRIPTION_HALTED),
    ],
)
def test_illegal_subscription_events_are_recorded_but_change_nothing(
    gateway: MockPaymentGateway, setup, event
):
    for earlier in setup:
        gateway.simulate_webhook(SimulateWebhookRequest(entity_id="sub_x", event=earlier))
    before = gateway.subscriptions["sub_x"].state if setup else SubscriptionState.CREATED
    gateway.simulate_webhook(SimulateWebhookRequest(entity_id="sub_x", event=event))
    status = gateway.get_subscription_status("sub_x")
    assert status.subscription.state is before
    assert status.subscription.failed_charge_attempts == 0
    assert status.event_history[-1].event is event
    assert status.transition_log[-1].applied is False
    assert status.transition_log[-1].reason.startswith("illegal_transition_")


def test_a_halted_subscription_stays_halted(gateway: MockPaymentGateway):
    _activate(gateway, "sub_h")
    for _ in range(3):
        gateway.simulate_webhook(
            SimulateWebhookRequest(entity_id="sub_h", event=WebhookEventName.SUBSCRIPTION_PENDING)
        )
    assert gateway.subscriptions["sub_h"].state is SubscriptionState.HALTED
    gateway.simulate_webhook(
        SimulateWebhookRequest(entity_id="sub_h", event=WebhookEventName.SUBSCRIPTION_CHARGED)
    )
    assert gateway.subscriptions["sub_h"].state is SubscriptionState.HALTED
