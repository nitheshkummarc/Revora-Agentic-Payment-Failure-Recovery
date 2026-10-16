"""Puts payments into the state a dataset row describes.

Each row of `data/synthetic_events_500.json` carries declarative gateway
operations (`gateway_seed.setup` and `gateway_seed.at_observation`). These
helpers execute them. Used by the batch runner for all 500 rows and by the
agent demo API for one row at a time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.gateway.mock_gateway import MockPaymentGateway
from app.gateway.schemas import (
    ChaosConfig,
    CreatePaymentRequest,
    ErrorObject,
    FailPaymentRequest,
    SimulateWebhookRequest,
)


class BatchClock:
    """A clock the caller moves explicitly. Nothing here sleeps."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def set(self, moment: datetime) -> datetime:
        self.t = moment
        return self.t


def _chaos_config(raw: Optional[Dict[str, Any]]) -> Optional[ChaosConfig]:
    return ChaosConfig(**raw) if raw else None


def apply_step(gateway: MockPaymentGateway, step: Dict[str, Any]) -> None:
    """Execute one declared gateway operation.

    An unrecognised op raises rather than being skipped: a dataset the runner
    only partly understands would seed payments into states the rows do not
    describe, and every downstream figure would be quietly wrong.
    """
    op = step["op"]
    if op == "create_payment":
        gateway.create_payment(
            CreatePaymentRequest(
                payment_id=step["payment_id"],
                amount=step["amount"],
                currency=step["currency"],
                notes=step["notes"],
            )
        )
    elif op == "fail_payment":
        gateway.fail_payment(
            FailPaymentRequest(
                payment_id=step["payment_id"],
                error=ErrorObject(**step["error"]),
                chaos=_chaos_config(step["chaos"]),
            )
        )
    elif op == "simulate_webhook":
        gateway.simulate_webhook(
            SimulateWebhookRequest(
                entity_id=step["payment_id"],
                event=step["event"],
                chaos=_chaos_config(step["chaos"]),
            )
        )
    else:
        raise ValueError(f"unknown gateway seed op: {op!r}")
