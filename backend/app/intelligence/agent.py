"""The planning agent: a bounded tool-calling loop around the recommendation.

Two phases.

1. **Investigate.** The model may call read-only tools to look things up: the
   payment's current gateway status, its compliance facts, its retry history,
   and a dry run of the compliance rules. It stops when it has what it needs,
   or when the tool budget runs out.
2. **Propose.** The model is asked once more, with the lookup results added to
   its input, and answers through the same strict JSON-schema structured
   output a single recommendation call uses. The proposal is validated as an
   `LLMRecommendation`, exactly like the single-call path.

The proposal is a separate structured call rather than a "submit" tool because
GPT-OSS on Groq sometimes names an invented tool (`json`) when it means to
submit, which the API rejects. Structured output has no such failure mode. A
rejected tool call during investigation ends the investigation, since it means
the model was trying to answer.

Everything after the proposal is unchanged: `IntelligenceLayer` applies its
guards, the policy engine validates, and the orchestrator executes.

What the agent cannot do is structural. There is no tool that writes: the
toolbox protocol below has four read-only methods, and the executor lives in
the orchestrator, which only acts on an approved policy decision. This module
imports no gateway code; the orchestrator builds the toolbox and passes it in.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from app.core.config import GROQ_SETTINGS
from app.intelligence.prompts import with_findings
from app.intelligence.schemas import AgentStep, LLMRecommendation, RecommendedAction

#: Lookups allowed per payment before the agent must propose. A Revora design
#: choice: the four tools once each, with room for one repeat.
MAX_AGENT_TOOL_CALLS = 5

#: Step names that are not tools the model can call.
PROPOSE_ACTION = "propose_action"
REJECTED_TOOL_CALL = "rejected_tool_call"


class AgentUnavailableError(RuntimeError):
    """The agent loop could not produce a usable proposal."""


class AgentToolbox(Protocol):
    """Read-only lookups for one payment. Each returns a JSON-serialisable dict."""

    def check_payment_status(self) -> Dict[str, Any]:
        ...

    def get_mandate_facts(self) -> Dict[str, Any]:
        ...

    def get_retry_history(self) -> Dict[str, Any]:
        ...

    def check_policy(self, action: RecommendedAction) -> Dict[str, Any]:
        ...


def _function(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_ACTIONS = [action.value for action in RecommendedAction]

#: The lookup tools, in the OpenAI-compatible format the Groq API accepts.
TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    _function(
        "check_payment_status",
        "Current state of the payment at the gateway, and the state of any "
        "earlier retry attempts.",
        {},
        [],
    ),
    _function(
        "get_mandate_facts",
        "Compliance facts for the payment: amount, mandate ceiling, pre-debit "
        "notice time, AFA flag, mandate category, opt-out and proposed discount.",
        {},
        [],
    ),
    _function(
        "get_retry_history",
        "How many recovery attempts have already been made, and the limit.",
        {},
        [],
    ),
    _function(
        "check_policy",
        "Predict whether an action would pass the compliance rules, and which "
        "rule would block it. Does not perform the action.",
        {"action": {"type": "string", "enum": _ACTIONS}},
        ["action"],
    ),
]

AGENT_TOOL_NAMES = [definition["function"]["name"] for definition in TOOL_DEFINITIONS]


def run_tool(toolbox: AgentToolbox, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Run one read-only tool. A bad call returns an error the model can read
    rather than raising, so the loop can continue."""
    if name == "check_payment_status":
        return toolbox.check_payment_status()
    if name == "get_mandate_facts":
        return toolbox.get_mandate_facts()
    if name == "get_retry_history":
        return toolbox.get_retry_history()
    if name == "check_policy":
        try:
            action = RecommendedAction(arguments.get("action"))
        except ValueError:
            return {"error": f"action must be one of {_ACTIONS}"}
        return toolbox.check_policy(action)
    return {"error": f"unknown tool {name!r}"}


def findings_text(steps: List[AgentStep]) -> str:
    """The lookup results, as they are shown to the model for its proposal."""
    lookups = [step for step in steps if step.tool in AGENT_TOOL_NAMES]
    if not lookups:
        return "No lookups were made."
    return "\n".join(
        f"{step.tool}({json.dumps(step.arguments)}) -> {json.dumps(step.result)}"
        for step in lookups
    )


def _is_tool_use_failure(exc: Exception) -> bool:
    """Whether the provider rejected a turn because the model named a tool
    that was not offered. Groq reports this as a 400 with code
    `tool_use_failed`."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("code") == "tool_use_failed":
            return True
    return "tool_use_failed" in str(exc)


@dataclass
class AgentResult:
    """The agent's proposal and the steps that led to it."""

    recommendation: LLMRecommendation
    steps: List[AgentStep] = field(default_factory=list)


class GroqAgentClient:
    """Runs the investigate-then-propose loop against Groq.

    The proposal uses `GroqLLMClient`'s strict JSON-schema call, so it carries
    the same validation guarantee as the single-call path; an invalid one
    raises, and the recommendation layer escalates. `recommend` (no tools) is
    that same single call, so this client also satisfies the plain
    `LLMClient` protocol.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        max_output_tokens: int = 2048,
        api_key: Optional[str] = None,
        client: Optional[object] = None,
        max_tool_calls: int = MAX_AGENT_TOOL_CALLS,
    ) -> None:
        from app.intelligence.llm_client import DEFAULT_GROQ_MODEL, GroqLLMClient

        self.model = model or DEFAULT_GROQ_MODEL
        self.max_output_tokens = max_output_tokens
        self.max_tool_calls = max_tool_calls
        if client is None:
            try:
                import groq  # imported lazily: the rest of Revora runs without it
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise AgentUnavailableError("the `groq` package is not installed") from exc
            client = groq.Groq(
                api_key=api_key or os.environ.get("GROQ_API_KEY"),
                timeout=GROQ_SETTINGS.request_timeout_seconds,
                max_retries=GROQ_SETTINGS.max_retries,
            )
        self._client = client
        self._single_call = GroqLLMClient(
            model=self.model, max_output_tokens=max_output_tokens, client=client
        )

    def recommend(self, system_prompt: str, user_content: str) -> LLMRecommendation:
        return self._single_call.recommend(system_prompt, user_content)

    def recommend_with_tools(
        self, system_prompt: str, user_content: str, toolbox: AgentToolbox
    ) -> AgentResult:
        steps = self._investigate(system_prompt, user_content, toolbox)
        try:
            recommendation = self._single_call.recommend(
                system_prompt, with_findings(user_content, findings_text(steps))
            )
        except ValueError as exc:  # schema validation of the proposal
            raise AgentUnavailableError(f"invalid proposal: {exc}") from exc
        steps.append(
            AgentStep(
                step=len(steps) + 1,
                tool=PROPOSE_ACTION,
                arguments=recommendation.model_dump(mode="json"),
            )
        )
        return AgentResult(recommendation=recommendation, steps=steps)

    def _investigate(
        self, system_prompt: str, user_content: str, toolbox: AgentToolbox
    ) -> List[AgentStep]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        steps: List[AgentStep] = []
        while len(steps) < self.max_tool_calls:
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                    max_completion_tokens=self.max_output_tokens,
                )
            except Exception as exc:
                if not _is_tool_use_failure(exc):
                    raise
                steps.append(
                    AgentStep(
                        step=len(steps) + 1,
                        tool=REJECTED_TOOL_CALL,
                        result={"note": "called a tool that does not exist; moving to the proposal"},
                    )
                )
                return steps
            message = response.choices[0].message
            calls = message.tool_calls or []
            if not calls:
                return steps  # the model has what it needs
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in calls
                    ],
                }
            )
            for call in calls:
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be a JSON object")
                except ValueError:
                    arguments, result = {}, {"error": "arguments were not a valid JSON object"}
                else:
                    result = run_tool(toolbox, call.function.name, arguments)
                steps.append(
                    AgentStep(
                        step=len(steps) + 1,
                        tool=call.function.name,
                        arguments=arguments,
                        result=result,
                    )
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )
        return steps


class ScriptedAgentClient:
    """Deterministic offline agent for tests and keyless demos.

    Follows the investigation a careful operator would: check the payment's
    real status, read the compliance facts and retry history, then ask the
    rules about a retry. It proposes a status check if the payment has already
    succeeded, the rules' own substitute action if a retry would be blocked,
    and a retry otherwise. It never reads the customer note.
    """

    model = "scripted-agent"

    def recommend(self, system_prompt: str, user_content: str) -> LLMRecommendation:
        return LLMRecommendation(
            recommended_action=RecommendedAction.RETRY_SOFT,
            confidence=0.8,
            reasoning="scripted agent without tools: a single soft retry is reasonable",
        )

    def recommend_with_tools(
        self, system_prompt: str, user_content: str, toolbox: AgentToolbox
    ) -> AgentResult:
        steps: List[AgentStep] = []

        def call(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            arguments = arguments or {}
            result = run_tool(toolbox, name, arguments)
            steps.append(AgentStep(step=len(steps) + 1, tool=name, arguments=arguments, result=result))
            return result

        status = call("check_payment_status")
        facts = call("get_mandate_facts")
        call("get_retry_history")

        state = status.get("payment_state")
        paid_attempts = [
            attempt["payment_id"]
            for attempt in status.get("retry_attempts", [])
            if attempt.get("state") == "CAPTURED"
        ]
        if state in ("CAPTURED", "AUTHORIZED") or paid_attempts:
            proposal = LLMRecommendation(
                recommended_action=RecommendedAction.REQUEST_VERIFICATION,
                confidence=0.9,
                reasoning=(
                    f"The gateway reports the payment as {state}"
                    + (f" and attempt(s) {paid_attempts} as captured" if paid_attempts else "")
                    + ", although the delivered webhooks say it failed. A retry could "
                    "charge the customer twice, so the status should be confirmed instead."
                ),
            )
        else:
            policy = call("check_policy", {"action": RecommendedAction.RETRY_SOFT.value})
            if policy.get("approved"):
                proposal = LLMRecommendation(
                    recommended_action=RecommendedAction.RETRY_SOFT,
                    confidence=0.8,
                    reasoning=(
                        f"The payment is {state} at the gateway, the failure is quoted from "
                        "the error object, and the compliance rules allow a retry."
                    ),
                )
            else:
                substitute = RecommendedAction(policy.get("final_action", "ESCALATE_HUMAN"))
                proposal = LLMRecommendation(
                    recommended_action=substitute,
                    confidence=0.9,
                    reasoning=(
                        f"A retry would be blocked by {policy.get('rule_id')}: "
                        f"{policy.get('blocked_reason')}"
                        + (" The customer has opted out." if facts.get("opted_out") else "")
                    ),
                )
        steps.append(
            AgentStep(
                step=len(steps) + 1,
                tool=PROPOSE_ACTION,
                arguments=proposal.model_dump(mode="json"),
            )
        )
        return AgentResult(recommendation=proposal, steps=steps)


__all__ = [
    "AGENT_TOOL_NAMES",
    "AgentResult",
    "AgentToolbox",
    "AgentUnavailableError",
    "GroqAgentClient",
    "MAX_AGENT_TOOL_CALLS",
    "ScriptedAgentClient",
    "TOOL_DEFINITIONS",
    "findings_text",
    "run_tool",
]
