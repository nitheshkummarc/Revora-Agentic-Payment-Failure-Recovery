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


# --------------------------------------------------------------------------
# Subscriptions: PENDING -> HALTED after exactly 3 failed charge attempts
# --------------------------------------------------------------------------
def test_subscription_halts_after_three_failed_charges(gateway: MockPaymentGateway):
    """Subscriptions move to halted after exactly three charge-retry attempts."""
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
        "subscription.pending",
        "subscription.pending",
        "subscription.pending",
        "subscription.halted",
    ]


def test_duplicate_subscription_pending_does_not_double_count(
    gateway: MockPaymentGateway, clock: FakeClock
):
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


def test_concurrent_payment_creation_does_not_corrupt_or_drop_records(
    clock: FakeClock,
):
    """FastAPI runs sync route handlers in a threadpool, so concurrent HTTP
    requests are real concurrent threads sharing one gateway instance. Without
    the per-call lock, interleaved read-modify-write on self.payments could
    lose or corrupt a record; with it, every one of N concurrent creations
    must land intact."""
    import threading

    gateway = MockPaymentGateway(
        settings=GatewaySettings(webhook_delivery_failure_rate=0.0), clock=clock
    )
    thread_count = 25
    errors: List[Exception] = []

    def _create_one(index: int) -> None:
        try:
            gateway.create_payment(
                CreatePaymentRequest(
                    payment_id=f"pay_concurrent_{index}",
                    amount=1000 + index,
                    currency="INR",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(exc)

    threads = [threading.Thread(target=_create_one, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []
    assert len(gateway.payments) == thread_count
    for index in range(thread_count):
        record = gateway.payments[f"pay_concurrent_{index}"]
        assert record.amount == 1000 + index
        assert len(gateway.event_history[f"pay_concurrent_{index}"]) >= 0


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
