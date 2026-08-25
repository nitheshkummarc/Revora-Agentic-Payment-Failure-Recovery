"""Global configuration for the Revora backend.

Settings fall into two categories, and every constant below says which it is.
Some mirror behaviour Razorpay documents publicly; the rest are local tuning
choices with no external basis. Only the former may be described as matching
gateway behaviour.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

#: Values that mean "off" for a boolean operational env var. Everything else
#: non-empty means "on" -- including typos, which is the safer direction for
#: a kill switch to fail towards, but "false"/"0"/"no" have to mean off or
#: setting one of them produces the opposite of the intended effect.
_FALSY_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})


def env_flag(name: str) -> bool:
    """Read a boolean operational env var (e.g. REVORA_DISABLE_LLM)."""
    return os.environ.get(name, "").strip().lower() not in _FALSY_ENV_VALUES


class GatewaySettings(BaseModel):
    """Settings for the mock payment gateway and its webhook delivery."""

    model_config = ConfigDict(extra="forbid")

    # Upper bound of the documented webhook delay window.
    max_webhook_delay_seconds: float = Field(default=45.0, ge=0.0)

    # Simulated rate at which outbound webhook delivery fails and has to be
    # retried. Chosen from the 3-15% chronic tool-call failure rate typical of
    # production integrations.
    webhook_delivery_failure_rate: float = Field(default=0.05, ge=0.0, le=1.0)

    # Transport-level retry only. Deciding whether to re-attempt a customer's
    # payment is a business decision owned by the policy and orchestration
    # layers, not this one.
    #
    # Both values below are local tuning choices with no external basis.
    # delivery_max_attempts is numerically identical to the recovery retry
    # limit and the subscription halt threshold, but is unrelated to either:
    # it counts HTTP delivery attempts for a single outbound webhook.
    delivery_max_attempts: int = Field(default=3, ge=1)
    delivery_backoff_base_seconds: float = Field(default=0.5, ge=0.0)

    # Consecutive delivery failures before the breaker opens. Local tuning
    # choice, not a documented figure.
    circuit_breaker_threshold: int = Field(default=5, ge=1)

    # How long the breaker stays open before the next delivery attempt is let
    # through again. Local tuning choice: long enough that a transient burst
    # of failures doesn't immediately reopen it, short enough that a demo
    # session recovers within itself rather than needing a manual reset.
    circuit_breaker_cooldown_seconds: float = Field(default=30.0, ge=0.0)

    # Subscriptions halt after exactly this many charge-retry attempts,
    # matching Razorpay's documented subscription behaviour.
    subscription_halt_after_failed_charges: int = Field(default=3, ge=1)

    # Deterministic seed so demo/test runs are reproducible.
    random_seed: int = 1337


GATEWAY_SETTINGS = GatewaySettings()


class IntelligenceSettings(BaseModel):
    """Settings for the Anthropic model client."""

    model_config = ConfigDict(extra="forbid")

    # Both are local tuning choices with no external basis. A 500-row batch
    # calls the model sequentially, so a hung or slow response has to fail
    # closed within a bounded time rather than stall the whole run.
    request_timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_retries: int = Field(default=2, ge=0)


INTELLIGENCE_SETTINGS = IntelligenceSettings()
