"""Schemas for the recommendation layer.

The action enum is closed. Modelling it as an enum means an invented action
string fails validation rather than flowing downstream as a plausible-looking
value.

`LLMRecommendation` carries exactly three fields and nothing more. It is the
schema handed to the provider's structured-output feature, so every field added
here becomes a field the model is asked to fill. Audit metadata lives on
`IntelligenceDecision`, which the code builds itself.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.tracer.schemas import TraceResult


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecommendedAction(str, Enum):
    """The closed set of recovery actions.

    Only RETRY_SOFT moves money or re-attempts a customer's payment. The other
    three read state or stop, which is why the injection guard in llm_client.py
    treats RETRY_SOFT as the one action an untrusted note must never reach.
    """

    RETRY_SOFT = "RETRY_SOFT"
    REQUEST_VERIFICATION = "REQUEST_VERIFICATION"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    NO_ACTION_COOLDOWN = "NO_ACTION_COOLDOWN"


#: The only action that causes a payment re-attempt. Everything else is safe to
#: emit from a poisoned input because it either reads state or stops.
MONEY_MOVING_ACTIONS = frozenset({RecommendedAction.RETRY_SOFT})


class LLMRecommendation(StrictModel):
    """The model's output contract.

    `{"recommended_action": enum, "confidence": float, "reasoning": str}`
    """

    recommended_action: RecommendedAction
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class SanitizationReport(StrictModel):
    """What the sanitizer did to one customer-supplied string.

    Kept on the decision so the audit trail shows the note was processed even
    when the LLM was never called.
    """

    original_length: int
    sanitized_length: int
    truncated: bool
    control_characters_stripped: int
    injection_patterns_flagged: List[str] = Field(default_factory=list)
    looks_like_instruction: bool = False


class IntelligenceInput(StrictModel):
    """Everything the recommendation layer is allowed to see.

    `trace` is the tracer's complete result. `customer_note` is raw untrusted
    text: the layer sanitises it internally, so a caller cannot bypass the
    sanitiser by pre-formatting the note. There is deliberately no field for
    raw gateway events. The model sees only the tracer's output, and a schema
    that cannot carry raw events cannot leak them.
    """

    payment_id: str
    trace: TraceResult
    customer_note: Optional[str] = None
    decided_at: Optional[datetime] = None


class IntelligenceDecision(StrictModel):
    """The recommendation plus how it was reached.

    A recommendation only, never a command. The policy engine and the
    orchestrator decide what actually happens.
    """

    payment_id: str

    recommended_action: RecommendedAction
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str

    # --- how the decision was reached ---
    llm_called: bool
    short_circuit_reason: Optional[str] = None
    guard_override_reason: Optional[str] = None
    original_llm_action: Optional[RecommendedAction] = None

    # --- the untrusted note, as it was actually used ---
    untrusted_customer_note: str = ""
    sanitization: SanitizationReport

    model: Optional[str] = None
    decided_at: datetime
