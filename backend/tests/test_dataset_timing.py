"""Behavioural proof of the dataset's two-phase seed timing.

`test_seed_offset_outlasts_the_silence_threshold` in test_dataset.py asserts
only that 600 > 300. That is a comparison of two integers: it would still pass
if the resolver ignored elapsed time entirely, and it proves nothing about what
the offset actually buys.

These tests drive the real gateway and the real resolver through the runner's
own `apply_step`, at two seed offsets that differ in nothing but time, and
assert the resolutions actually diverge. If the silence threshold moves, or the
resolver stops distinguishing a dropped webhook from a payment still in flight,
these fail where the arithmetic assertion would not.
"""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from app.core.config import GatewaySettings
from app.gateway.mock_gateway import MockPaymentGateway
from app.state_machine.resolver import StateResolver
from app.state_machine.schemas import PaymentObservation, ResolutionRule
from app.state_machine.states import CanonicalState, SILENCE_THRESHOLD_SECONDS

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "data" / "run_batch.py"

REFERENCE = datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)

#: The dataset's own offset, and one deliberately inside the threshold.
BEYOND_THRESHOLD_SECONDS = 600
WITHIN_THRESHOLD_SECONDS = 200


@pytest.fixture(scope="module")
def runner():
    """The real replay code, not a reimplementation of it.

    Importing the runner is the point: a test that rebuilt the seeding by hand
    could pass while the shipped replay path was broken.
    """
    spec = importlib.util.spec_from_file_location("run_batch", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def set(self, moment: datetime) -> datetime:
        self.t = moment
        return self.t


def silent_drop_steps(payment_id: str) -> List[Dict[str, Any]]:
    """A silent-drop row's setup, identical for both offsets under test."""
    return [
        {
            "op": "create_payment",
            "payment_id": payment_id,
            "amount": 99900,
            "currency": "INR",
            "notes": None,
        },
        {
            "op": "fail_payment",
            "payment_id": payment_id,
            "error": {
                "code": "BAD_REQUEST_ERROR",
                "description": "Payment failed due to incorrect OTP",
                "field": "otp",
                "source": "customer",
                "step": "payment_authentication",
                "reason": "incorrect_otp",
                "metadata": {},
            },
            "chaos": {
                "modes": ["silent_drop"],
                "delay_seconds": 0.0,
                "flip_after_seconds": 30.0,
                "duplicate_after_seconds": 1.0,
            },
        },
    ]


def resolve_at_reference(runner, seed_offset_seconds: int):
    """Seed a silent-drop payment `seed_offset_seconds` before the batch
    instant, then observe it at the batch instant."""
    clock = Clock(REFERENCE - timedelta(seconds=seed_offset_seconds))
    gateway = MockPaymentGateway(
        # No transport flakiness: the only variable under test is elapsed time.
        settings=GatewaySettings(webhook_delivery_failure_rate=0.0),
        clock=clock,
    )
    payment_id = f"pay_offset_{seed_offset_seconds}"
    for step in silent_drop_steps(payment_id):
        runner.apply_step(gateway, step)

    clock.set(REFERENCE)

    snapshot = gateway.get_payment_status(payment_id)
    observation = PaymentObservation(
        payment_id=payment_id,
        created_at=snapshot.payment.created_at,
        events=list(snapshot.event_history),
        observed_at=clock(),
    )
    return StateResolver().resolve(observation), snapshot


def test_the_two_offsets_differ_only_in_elapsed_time(runner):
    """Guards the comparison itself: if the two scenarios diverged in any way
    other than timing, the tests below would prove nothing about timing."""
    beyond = silent_drop_steps("pay_x")
    within = silent_drop_steps("pay_x")
    assert beyond == within
    assert WITHIN_THRESHOLD_SECONDS < SILENCE_THRESHOLD_SECONDS < BEYOND_THRESHOLD_SECONDS


def test_no_webhook_is_delivered_at_either_offset(runner):
    """Both payments are equally silent. What differs is only how long they
    have been silent -- not whether any evidence arrived."""
    for offset in (BEYOND_THRESHOLD_SECONDS, WITHIN_THRESHOLD_SECONDS):
        _, snapshot = resolve_at_reference(runner, offset)
        assert snapshot.event_history == []
        # The gateway itself knows the payment failed; the merchant cannot.
        assert snapshot.payment.state.value == "FAILED"


def test_seeding_beyond_the_threshold_resolves_to_pending_webhook(runner):
    """600s of silence is past the threshold, so the silence is a signal and
    the payment must be flagged for a status check."""
    resolution, _ = resolve_at_reference(runner, BEYOND_THRESHOLD_SECONDS)

    assert resolution.state is CanonicalState.PENDING_WEBHOOK
    assert resolution.needs_status_check is True
    assert resolution.resolution_reason is ResolutionRule.SILENCE_THRESHOLD_EXCEEDED


def test_seeding_within_the_threshold_does_not_flag_the_payment(runner):
    """200s of silence is a payment legitimately still in progress. Flagging it
    would manufacture ambiguity the tracer would then have to explain away."""
    resolution, _ = resolve_at_reference(runner, WITHIN_THRESHOLD_SECONDS)

    assert resolution.state is CanonicalState.CREATED
    assert resolution.needs_status_check is False
    assert resolution.resolution_reason is ResolutionRule.WITHIN_SILENCE_THRESHOLD


def test_the_two_offsets_actually_produce_different_resolutions(runner):
    """The claim the dataset's 600s offset rests on, stated as one assertion:
    identical scenarios seeded either side of the threshold resolve
    differently."""
    beyond, _ = resolve_at_reference(runner, BEYOND_THRESHOLD_SECONDS)
    within, _ = resolve_at_reference(runner, WITHIN_THRESHOLD_SECONDS)

    assert beyond.state is not within.state
    assert beyond.needs_status_check != within.needs_status_check
    assert beyond.resolution_reason is not within.resolution_reason


def test_the_boundary_is_inclusive_of_the_threshold(runner):
    """The comparison is `silence_seconds >= threshold`, so exactly 300s of
    silence already flags; 299s does not.

    Pinned in this direction deliberately. A >= / > slip moves the boundary by
    one second and nothing else in the suite would notice, because every other
    case sits far from it.
    """
    at_threshold, _ = resolve_at_reference(runner, int(SILENCE_THRESHOLD_SECONDS))
    assert at_threshold.state is CanonicalState.PENDING_WEBHOOK
    assert at_threshold.needs_status_check is True

    one_before, _ = resolve_at_reference(runner, int(SILENCE_THRESHOLD_SECONDS) - 1)
    assert one_before.state is CanonicalState.CREATED
    assert one_before.needs_status_check is False


def test_the_shipped_dataset_uses_an_offset_that_produces_the_flag(runner):
    """Ties the behaviour back to the committed file: the offset the dataset
    actually ships must be one that lands on the PENDING_WEBHOOK side."""
    import json

    dataset = json.loads(
        (REPO_ROOT / "data" / "synthetic_events_500.json").read_text(encoding="utf-8")
    )
    resolution, _ = resolve_at_reference(runner, int(dataset["seed_offset_seconds"]))
    assert resolution.state is CanonicalState.PENDING_WEBHOOK
    assert resolution.needs_status_check is True
