"""Global configuration for the RecoverX backend.

Values that come from GROUND_TRUTH.md are marked with the section they came
from. Anything not documented there is a local implementation detail and is
labelled as such -- do not present those to judges as Razorpay/RBI figures.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GatewaySettings(BaseModel):
    """Settings for the MockPaymentGateway (Module 1)."""

    model_config = ConfigDict(extra="forbid")

    # GROUND_TRUTH.md Day 1-2: "delayed webhook (0-45s)".
    max_webhook_delay_seconds: float = Field(default=45.0, ge=0.0)

    # GROUND_TRUTH.md Day 1-2, failure-mode table: "Chronic tool-call failure
    # rate (3-15% is normal in production)". This is the simulated rate at
    # which our own outbound webhook delivery fails and has to be retried.
    webhook_delivery_failure_rate: float = Field(default=0.05, ge=0.0, le=1.0)

    # Infra-level retry only (NOT payment-recovery retry -- that is the
    # Orchestrator/Policy layer's decision, per the Module 1 prompt).
    #
    # Both values below are local implementation details, NOT documented
    # Razorpay figures. Note especially that delivery_max_attempts=3 is
    # numerically identical to GROUND_TRUTH.md's MAX_RETRIES=3 and to the
    # 3-failed-charge subscription halt threshold, but is completely
    # unrelated to either: this counts HTTP delivery attempts for one
    # outbound webhook, not payment-recovery attempts for a customer.
    # Do not cite this number to judges as a Razorpay behaviour.
    delivery_max_attempts: int = Field(default=3, ge=1)
    delivery_backoff_base_seconds: float = Field(default=0.5, ge=0.0)

    # Circuit breaker: consecutive delivery failures before the breaker opens.
    # Implementation detail, not a documented Razorpay figure.
    circuit_breaker_threshold: int = Field(default=5, ge=1)

    # GROUND_TRUTH.md Day 1-2: subscriptions move to HALTED after exactly 3
    # charge-retry attempts.
    subscription_halt_after_failed_charges: int = Field(default=3, ge=1)

    # Deterministic seed so demo/test runs are reproducible.
    random_seed: int = 1337


GATEWAY_SETTINGS = GatewaySettings()
