"""Model client and recommendation-layer entry point.

Two things live here:

* `AnthropicLLMClient`, a thin wrapper over the Anthropic Messages API using the
  provider's structured-output feature (`messages.parse` with a Pydantic model),
  so valid JSON is guaranteed by the API rather than coaxed out with a "please
  output JSON" instruction and a regex.

* `IntelligenceLayer`, the public entry point. It sanitises, decides whether the
  model should be consulted at all, and applies the deterministic safety guard
  on the way out.

Every call is fresh. No conversation state is kept between events: per-item
accuracy degrades as context accumulates across a long batch run, so
`recommend()` builds a new single-message request each time and keeps no history
attribute to accumulate into.

This module never touches the gateway. Its output is a recommendation only; the
policy engine and orchestrator are the only components allowed to act on it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, List, Optional, Protocol

from app.core.config import INTELLIGENCE_SETTINGS
from app.core.logging import get_logger, log_event
from app.intelligence.prompts import SYSTEM_PROMPT, build_user_content
from app.intelligence.sanitizer import sanitize_customer_note
from app.intelligence.schemas import (
    MONEY_MOVING_ACTIONS,
    IntelligenceDecision,
    IntelligenceInput,
    LLMRecommendation,
    RecommendedAction,
)

logger = get_logger("intelligence")

#: Default model for recommendation calls.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 2048


class LLMClient(Protocol):
    """Minimal contract the Intelligence Layer needs from a provider."""

    model: str

    def recommend(self, system_prompt: str, user_content: str) -> LLMRecommendation:
        """One stateless call. Must return a validated LLMRecommendation."""
        ...


class LLMUnavailableError(RuntimeError):
    """The provider could not be reached or returned an unusable response."""


class AnthropicLLMClient:
    """Anthropic Messages API client using native structured outputs."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: Optional[str] = None,
        client: Optional[object] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        if client is not None:
            self._client = client
            return
        try:
            import anthropic  # imported lazily: the rest of Revora runs without it
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise LLMUnavailableError(
                "the `anthropic` package is not installed; install it or pass a "
                "different LLMClient implementation"
            ) from exc
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        client_kwargs = {
            "timeout": timeout if timeout is not None else INTELLIGENCE_SETTINGS.request_timeout_seconds,
            "max_retries": max_retries if max_retries is not None else INTELLIGENCE_SETTINGS.max_retries,
        }
        self._client = (
            anthropic.Anthropic(api_key=key, **client_kwargs)
            if key
            else anthropic.Anthropic(**client_kwargs)
        )

    def recommend(self, system_prompt: str, user_content: str) -> LLMRecommendation:
        # A brand-new single-message request. Nothing from any previous event is
        # carried in -- there is deliberately no self._history to append to.
        #
        # The system prompt is a frozen constant sent byte-identical on every
        # call, and the per-event content sits after it, so a cache breakpoint
        # at the end of the system block is read by every subsequent request in
        # a batch. Placement matters more than the marker: caching is a prefix
        # match, so the breakpoint has to fall at the end of the shared portion.
        #
        # Whether it engages depends on the prompt clearing the model's minimum
        # cacheable prefix, which is not a property of this code and is not
        # asserted here. Read `usage.cache_read_input_tokens` on a live call to
        # find out -- a prefix below the minimum silently does not cache.
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
            output_format=LLMRecommendation,
        )
        parsed = getattr(response, "parsed_output", None)
        if not isinstance(parsed, LLMRecommendation):
            raise LLMUnavailableError(
                "structured output did not validate against LLMRecommendation"
            )
        return parsed


class StubLLMClient:
    """Deterministic offline client for tests and for demoing without a key.

    Records every call so tests can assert that each event got a fresh,
    single-message request with no accumulated history.
    """

    def __init__(
        self,
        recommendation: Optional[LLMRecommendation] = None,
        responder: Optional[Callable[[str, str], LLMRecommendation]] = None,
        model: str = "stub-model",
    ) -> None:
        self.model = model
        self._recommendation = recommendation or LLMRecommendation(
            recommended_action=RecommendedAction.RETRY_SOFT,
            confidence=0.8,
            reasoning="stub: transient failure, a single soft retry is reasonable",
        )
        self._responder = responder
        self.calls: List[dict] = []

    def recommend(self, system_prompt: str, user_content: str) -> LLMRecommendation:
        self.calls.append({"system": system_prompt, "user": user_content})
        if self._responder is not None:
            return self._responder(system_prompt, user_content)
        return self._recommendation.model_copy(deep=True)


class ExplodingLLMClient:
    """Raises if called at all. Used to prove the ambiguity short-circuit never
    reaches the LLM, rather than merely asserting a boolean flag."""

    model = "must-not-be-called"

    def recommend(self, system_prompt: str, user_content: str) -> LLMRecommendation:
        raise AssertionError(
            "the LLM must not be consulted for an ambiguous trace -- "
            "never let the LLM guess in place of missing data"
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntelligenceLayer:
    """Turns a tracer result into a recommended recovery action."""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._client = llm_client
        self._clock = clock or _utcnow

    def recommend(self, request: IntelligenceInput) -> IntelligenceDecision:
        now = request.decided_at or self._clock()

        # ------------------------------------------------------------------
        # 1. Sanitise first, unconditionally. This is context construction
        #    rather than a branch of the decision: the note is sanitised and
        #    injection-flagged even when the model is never called, so the
        #    audit trail is complete either way.
        # ------------------------------------------------------------------
        note, report = sanitize_customer_note(request.customer_note)

        # ------------------------------------------------------------------
        # 2. Ambiguity short-circuit. If the tracer could not build a confident
        #    chain, the model is not asked to guess in place of the missing
        #    data. No call is made at all.
        # ------------------------------------------------------------------
        if request.trace.ambiguous:
            decision = IntelligenceDecision(
                payment_id=request.payment_id,
                recommended_action=RecommendedAction.REQUEST_VERIFICATION,
                confidence=1.0,
                reasoning=(
                    "Tracer reported ambiguous=true, so no LLM call was made. "
                    "Deterministic short-circuit to REQUEST_VERIFICATION: the "
                    "payment's status must be confirmed before any recovery "
                    "action. Tracer ambiguity reasons: "
                    f"{request.trace.ambiguity_reasons}"
                ),
                llm_called=False,
                short_circuit_reason="tracer_ambiguous",
                untrusted_customer_note=note,
                sanitization=report,
                model=None,
                decided_at=now,
            )
            self._log(decision)
            return decision

        # ------------------------------------------------------------------
        # 3. Consult the model. Fresh call, tracer output only, note delimited.
        #
        #    REVORA_DISABLE_LLM is an operational kill switch: set it to fall
        #    back to the same fail-safe escalation as a missing client,
        #    without having to change how the layer is wired. Checked here
        #    rather than at construction so it can be flipped for a live demo
        #    without a restart.
        # ------------------------------------------------------------------
        if os.environ.get("REVORA_DISABLE_LLM"):
            return self._fail_safe(
                request, note, report, now, reason="llm_disabled_by_kill_switch"
            )
        if self._client is None:
            return self._fail_safe(
                request,
                note,
                report,
                now,
                reason="no_llm_client_configured",
            )

        user_content = build_user_content(request.trace, note)
        try:
            recommendation = self._client.recommend(SYSTEM_PROMPT, user_content)
        except AssertionError:
            raise
        except Exception as exc:  # provider error, validation error, timeout
            # The exception text (may carry request/response internals from the
            # SDK or network layer) is logged server-side only. reasoning and
            # short_circuit_reason reach the dashboard and the audit trail, so
            # they get a fixed, generic code instead.
            log_event(
                logger,
                "llm_call_failed",
                payment_id=request.payment_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return self._fail_safe(
                request, note, report, now, reason="llm_call_failed"
            )

        # ------------------------------------------------------------------
        # 4. Deterministic safety guard. The delimited-block prompt design is
        #    the primary defence against injection; this is a second layer that
        #    does not depend on the model having obeyed it. A note that looked
        #    like an instruction cannot produce a money-moving recommendation.
        # ------------------------------------------------------------------
        action = recommendation.recommended_action
        override_reason: Optional[str] = None
        original_action: Optional[RecommendedAction] = None

        if report.looks_like_instruction:
            original_action = action
            override_reason = (
                "injection_guard: customer note matched instruction-like "
                f"pattern(s) {report.injection_patterns_flagged}; recommendation "
                f"overridden from {action.value} to ESCALATE_HUMAN"
            )
            action = RecommendedAction.ESCALATE_HUMAN

        # ------------------------------------------------------------------
        # 5. Grounding guard. A money-moving action must be backed by a real
        #    error object, not just a model's say-so.
        #
        #    UNREACHABLE ON THE NORMAL PIPELINE -- a backstop, not an
        #    independently exercised rule. The tracer sets ambiguous=True
        #    whenever a FAILED payment has no grounded error object
        #    (tracer.py: "no_error_object_on_failure"), and an ambiguous trace
        #    never reaches this method at all -- it short-circuits to
        #    REQUEST_VERIFICATION in step 2, above. So `grounded_error` is
        #    already guaranteed non-None by the time a debiting action gets
        #    this far. Retained because that guarantee lives in a different
        #    module: if the ambiguity rule ever narrows, this catches what it
        #    no longer does.
        # ------------------------------------------------------------------
        if action in MONEY_MOVING_ACTIONS and request.trace.grounded_error is None:
            original_action = original_action or action
            override_reason = (
                "grounding_guard: recommendation "
                f"{action.value} is money-moving but the trace carries no "
                "grounded error object; overridden to ESCALATE_HUMAN"
            )
            action = RecommendedAction.ESCALATE_HUMAN

        decision = IntelligenceDecision(
            payment_id=request.payment_id,
            recommended_action=action,
            confidence=recommendation.confidence,
            reasoning=recommendation.reasoning,
            llm_called=True,
            short_circuit_reason=None,
            guard_override_reason=override_reason,
            original_llm_action=original_action,
            untrusted_customer_note=note,
            sanitization=report,
            model=getattr(self._client, "model", None),
            decided_at=now,
        )
        self._log(decision)
        return decision

    # -- helpers -----------------------------------------------------------
    def _fail_safe(
        self,
        request: IntelligenceInput,
        note: str,
        report,
        now: datetime,
        *,
        reason: str,
    ) -> IntelligenceDecision:
        """When the model cannot be consulted, escalate rather than guess.

        An adversarial or broken input must produce a rejected action or an
        escalation, never a silently executed unsafe one. A failed call is
        exactly that situation.
        """
        decision = IntelligenceDecision(
            payment_id=request.payment_id,
            recommended_action=RecommendedAction.ESCALATE_HUMAN,
            confidence=0.0,
            reasoning=(
                "No usable LLM recommendation was obtained, so the event is "
                f"escalated rather than guessed at. Reason: {reason}"
            ),
            llm_called=False,
            short_circuit_reason=reason,
            untrusted_customer_note=note,
            sanitization=report,
            model=getattr(self._client, "model", None) if self._client else None,
            decided_at=now,
        )
        self._log(decision)
        return decision

    @staticmethod
    def _log(decision: IntelligenceDecision) -> None:
        log_event(
            logger,
            "recommendation",
            payment_id=decision.payment_id,
            action=decision.recommended_action.value,
            confidence=decision.confidence,
            llm_called=decision.llm_called,
            short_circuit_reason=decision.short_circuit_reason,
            guard_override_reason=decision.guard_override_reason,
            injection_patterns=decision.sanitization.injection_patterns_flagged,
        )


__all__ = [
    "AnthropicLLMClient",
    "ExplodingLLMClient",
    "IntelligenceLayer",
    "LLMClient",
    "LLMUnavailableError",
    "MONEY_MOVING_ACTIONS",
    "StubLLMClient",
]
