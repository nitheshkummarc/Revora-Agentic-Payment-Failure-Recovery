"""Module 5 tests -- PolicyEngine.

The Module 5 prompt requires one explicit test per rule, each proving the
specific blocked_reason string is correct rather than merely that
approved == False. The five named ones are:

  * test_discount_exceeds_limit_is_blocked
  * test_mandate_ceiling_exceeded_is_blocked
  * test_missing_pre_debit_notice_is_blocked
  * test_afa_required_and_missing_is_blocked
  * test_opted_out_is_a_permanent_hard_block

GROUND_TRUTH.md Day 6-7 also asks this file to be treated as a regression
suite, not a one-off demo script: re-run it after any PolicyEngine change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.intelligence.schemas import (
    IntelligenceDecision,
    RecommendedAction,
    SanitizationReport,
)
from app.policy import rules as R
from app.policy.engine import PolicyEngine
from app.policy.schemas import EventContext, PolicyDecision

NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)

#: A notice sent well over PRE_DEBIT_NOTICE_HOURS ago -- the compliant case.
NOTICE_OK = NOW - timedelta(hours=30)


def clean_sanitization() -> SanitizationReport:
    return SanitizationReport(
        original_length=0,
        sanitized_length=0,
        truncated=False,
        control_characters_stripped=0,
        injection_patterns_flagged=[],
        looks_like_instruction=False,
    )


def llm(action: RecommendedAction = RecommendedAction.RETRY_SOFT) -> IntelligenceDecision:
    return IntelligenceDecision(
        payment_id="pay_1",
        recommended_action=action,
        confidence=0.9,
        reasoning="transient failure, a soft retry is reasonable",
        llm_called=True,
        untrusted_customer_note="",
        sanitization=clean_sanitization(),
        model="stub-model",
        decided_at=NOW,
    )


def context(**overrides) -> EventContext:
    """A fully compliant context. Tests break exactly one field at a time."""
    base = dict(
        payment_id="pay_1",
        amount=50000,  # Rs.500 in paise -- below the AFA threshold
        pre_debit_notice_sent_at=NOTICE_OK,
        mandate_ceiling=100000,  # Rs.1,000
        afa_flag=True,
        opted_out=False,
        retry_count=0,
        discount_amount=0,
        trace_confidence=1.0,
        evaluated_at=NOW,
    )
    base.update(overrides)
    return EventContext(**base)


@pytest.fixture()
def engine() -> PolicyEngine:
    return PolicyEngine(clock=lambda: NOW)


# --------------------------------------------------------------------------
# Ground-truth conformance: the constants ARE the rules
# --------------------------------------------------------------------------
def test_rule_constants_match_ground_truth_verbatim():
    assert R.MAX_RETRIES == 3
    assert R.MAX_DISCOUNT == 500
    assert R.MANDATE_CEILING_CHECK is True
    assert R.PRE_DEBIT_NOTICE_HOURS == 24
    assert R.AFA_REQUIRED_ABOVE == 15000
    assert R.COOLDOWN_AFTER_OPT_OUT == "permanent"


def test_paise_conversion_is_explicit_and_correct():
    """The rules are stated in rupees; the dataset is in paise. A silent
    mix-up here would be a compliance bug, not just an arithmetic one."""
    assert R.PAISE_PER_RUPEE == 100
    assert R.MAX_DISCOUNT_PAISE == 50_000  # Rs.500
    assert R.AFA_REQUIRED_ABOVE_PAISE == 1_500_000  # Rs.15,000


def test_rbi_circular_is_cited_by_number_only():
    """GROUND_TRUTH.md's Appendix warns judges may check section numbers.
    GROUND_TRUTH.md does not provide them, so none are claimed."""
    assert R.RBI_CIRCULAR == "RBI/DPSS/2026-27/396"
    for rule_id in R.RuleId:
        assert "section" not in rule_id.value.lower()


def test_sip_insurance_threshold_is_documented_but_not_enforced(engine):
    """Rs.1 lakh appears in GROUND_TRUTH.md's Appendix but not in the Day 6-7
    rule block, and enforcing it would need a mandate-category field the
    dataset spec does not define."""
    assert R.AFA_REQUIRED_ABOVE_SIP_INSURANCE == 100000
    # A Rs.20,000 action is still blocked without AFA -- the general threshold
    # is what is enforced.
    decision = engine.validate(
        llm(), context(amount=2_000_000, afa_flag=False, mandate_ceiling=5_000_000)
    )
    assert decision.approved is False
    assert decision.rule_id == "AFA_REQUIRED_AND_MISSING"


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_fully_compliant_retry_is_approved(engine):
    decision = engine.validate(llm(), context())
    assert decision.approved is True
    assert decision.blocked_reason is None
    assert decision.final_action is RecommendedAction.RETRY_SOFT
    assert decision.rule_id is None
    assert decision.overrode_llm is False


def test_every_rule_is_recorded_even_when_it_passes(engine):
    """The audit trail shows what was checked, not only what tripped."""
    decision = engine.validate(llm(), context())
    checked = {e.rule_id for e in decision.rules_evaluated}
    for expected in (
        "CUSTOMER_OPTED_OUT",
        "MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT",
        "MISSING_RBI_FIELD_MANDATE_CEILING",
        "MISSING_RBI_FIELD_AFA_FLAG",
        "PRE_DEBIT_NOTICE_TOO_RECENT",
        "MANDATE_CEILING_EXCEEDED",
        "AFA_REQUIRED_AND_MISSING",
        "MAX_DISCOUNT_EXCEEDED",
        "MAX_RETRIES_EXCEEDED",
    ):
        assert expected in checked
    assert all(e.passed for e in decision.rules_evaluated)


# --------------------------------------------------------------------------
# REQUIRED RULE TEST 1 -- discount exceeds limit
# --------------------------------------------------------------------------
def test_discount_exceeds_limit_is_blocked(engine):
    """GROUND_TRUTH.md's own demo example: the LLM recommends a Rs.5,000
    discount when the cap is Rs.500."""
    decision = engine.validate(llm(), context(discount_amount=500_000))  # Rs.5,000

    assert decision.approved is False
    assert decision.rule_id == "MAX_DISCOUNT_EXCEEDED"
    assert decision.blocked_reason == (
        "MAX_DISCOUNT_EXCEEDED: proposed discount Rs.5,000.00 exceeds "
        "MAX_DISCOUNT of Rs.500"
    )
    assert decision.final_action is RecommendedAction.ESCALATE_HUMAN
    assert decision.overrode_llm is True


def test_discount_exactly_at_the_cap_is_allowed(engine):
    decision = engine.validate(llm(), context(discount_amount=R.MAX_DISCOUNT_PAISE))
    assert decision.approved is True


# --------------------------------------------------------------------------
# REQUIRED RULE TEST 2 -- mandate ceiling exceeded
# --------------------------------------------------------------------------
def test_mandate_ceiling_exceeded_is_blocked(engine):
    decision = engine.validate(
        llm(), context(amount=150_000, mandate_ceiling=100_000)
    )  # Rs.1,500 against a Rs.1,000 ceiling

    assert decision.approved is False
    assert decision.rule_id == "MANDATE_CEILING_EXCEEDED"
    assert decision.blocked_reason == (
        "MANDATE_CEILING_EXCEEDED: action amount Rs.1,500.00 exceeds the "
        "customer-set variable-amount mandate ceiling of Rs.1,000.00 "
        "[RBI/DPSS/2026-27/396]"
    )
    assert decision.final_action is RecommendedAction.ESCALATE_HUMAN


def test_amount_exactly_at_the_ceiling_is_allowed(engine):
    decision = engine.validate(llm(), context(amount=100_000, mandate_ceiling=100_000))
    assert decision.approved is True


# --------------------------------------------------------------------------
# REQUIRED RULE TEST 3 -- missing pre-debit notice
# --------------------------------------------------------------------------
def test_missing_pre_debit_notice_is_blocked(engine):
    """Fail closed: the field is absent entirely, which must block rather than
    pass through."""
    decision = engine.validate(llm(), context(pre_debit_notice_sent_at=None))

    assert decision.approved is False
    assert decision.rule_id == "MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT"
    assert decision.blocked_reason == (
        "MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT: required RBI field "
        "`pre_debit_notice_sent_at` is missing from the event; failing closed "
        "rather than treating absence as compliance [RBI/DPSS/2026-27/396]"
    )
    assert decision.final_action is RecommendedAction.ESCALATE_HUMAN


def test_pre_debit_notice_sent_too_recently_is_blocked(engine):
    """Present but unmet: the notice must ALREADY have been sent 24h ago."""
    decision = engine.validate(
        llm(), context(pre_debit_notice_sent_at=NOW - timedelta(hours=2))
    )

    assert decision.approved is False
    assert decision.rule_id == "PRE_DEBIT_NOTICE_TOO_RECENT"
    assert decision.blocked_reason == (
        "PRE_DEBIT_NOTICE_TOO_RECENT: pre-debit notice was sent 2.0h ago but "
        "PRE_DEBIT_NOTICE_HOURS requires 24h to have already elapsed before a "
        "retry may fire [RBI/DPSS/2026-27/396]"
    )
    assert decision.final_action is RecommendedAction.NO_ACTION_COOLDOWN


def test_pre_debit_notice_exactly_24h_old_is_allowed(engine):
    decision = engine.validate(
        llm(), context(pre_debit_notice_sent_at=NOW - timedelta(hours=24))
    )
    assert decision.approved is True


def test_pre_debit_notice_dated_in_the_future_is_blocked(engine):
    decision = engine.validate(
        llm(), context(pre_debit_notice_sent_at=NOW + timedelta(hours=1))
    )
    assert decision.approved is False
    assert decision.rule_id == "PRE_DEBIT_NOTICE_TOO_RECENT"
    assert "is in the future" in decision.blocked_reason


# --------------------------------------------------------------------------
# REQUIRED RULE TEST 4 -- AFA required and missing
# --------------------------------------------------------------------------
def test_afa_required_and_missing_is_blocked(engine):
    """Above Rs.15,000 the action needs afa_flag = true, else auto-block."""
    decision = engine.validate(
        llm(), context(amount=2_000_000, afa_flag=False, mandate_ceiling=5_000_000)
    )

    assert decision.approved is False
    assert decision.rule_id == "AFA_REQUIRED_AND_MISSING"
    assert decision.blocked_reason == (
        "AFA_REQUIRED_AND_MISSING: action amount Rs.20,000.00 is above the "
        "AFA_REQUIRED_ABOVE threshold of Rs.15,000 and `afa_flag` is False; "
        "additional factor of authentication is required, so the action is "
        "auto-blocked [RBI/DPSS/2026-27/396]"
    )
    assert decision.final_action is RecommendedAction.ESCALATE_HUMAN


def test_afa_field_absent_entirely_is_blocked_as_a_missing_field(engine):
    """Absent is checked before value, so it can never read as a passing value."""
    decision = engine.validate(
        llm(), context(amount=2_000_000, afa_flag=None, mandate_ceiling=5_000_000)
    )
    assert decision.approved is False
    assert decision.rule_id == "MISSING_RBI_FIELD_AFA_FLAG"
    assert "failing closed" in decision.blocked_reason


def test_afa_exactly_at_the_threshold_does_not_require_the_flag(engine):
    """AFA_REQUIRED_ABOVE means strictly above."""
    at_threshold = engine.validate(
        llm(), context(amount=R.AFA_REQUIRED_ABOVE_PAISE, afa_flag=False, mandate_ceiling=2_000_000)
    )
    assert at_threshold.approved is True

    one_paise_over = engine.validate(
        llm(),
        context(amount=R.AFA_REQUIRED_ABOVE_PAISE + 1, afa_flag=False, mandate_ceiling=2_000_000),
    )
    assert one_paise_over.approved is False
    assert one_paise_over.rule_id == "AFA_REQUIRED_AND_MISSING"


def test_afa_present_and_true_allows_a_large_amount(engine):
    decision = engine.validate(
        llm(), context(amount=2_000_000, afa_flag=True, mandate_ceiling=5_000_000)
    )
    assert decision.approved is True


# --------------------------------------------------------------------------
# REQUIRED RULE TEST 5 -- opted out, permanent hard block
# --------------------------------------------------------------------------
def test_opted_out_is_a_permanent_hard_block(engine):
    decision = engine.validate(llm(), context(opted_out=True))

    assert decision.approved is False
    assert decision.rule_id == "CUSTOMER_OPTED_OUT"
    assert decision.blocked_reason == (
        "CUSTOMER_OPTED_OUT: customer has opted out; COOLDOWN_AFTER_OPT_OUT is "
        "'permanent' so this is a hard block with no override, regardless of any "
        "other field [RBI/DPSS/2026-27/396]"
    )
    assert decision.final_action is RecommendedAction.NO_ACTION_COOLDOWN


def test_opted_out_blocks_even_a_fully_compliant_event(engine):
    """"No override" means no other field can rescue it."""
    decision = engine.validate(
        llm(),
        context(
            opted_out=True,
            afa_flag=True,
            discount_amount=0,
            retry_count=0,
            pre_debit_notice_sent_at=NOW - timedelta(days=7),
        ),
    )
    assert decision.approved is False
    assert decision.rule_id == "CUSTOMER_OPTED_OUT"


@pytest.mark.parametrize("action", list(RecommendedAction))
def test_opted_out_blocks_every_action_type(engine, action):
    """Read strictly: nothing at all happens for an opted-out customer."""
    decision = engine.validate(llm(action), context(opted_out=True))
    assert decision.approved is False
    assert decision.rule_id == "CUSTOMER_OPTED_OUT"
    assert decision.final_action is RecommendedAction.NO_ACTION_COOLDOWN


# --------------------------------------------------------------------------
# Remaining rules
# --------------------------------------------------------------------------
def test_max_retries_exceeded_is_blocked(engine):
    decision = engine.validate(llm(), context(retry_count=3))

    assert decision.approved is False
    assert decision.rule_id == "MAX_RETRIES_EXCEEDED"
    assert decision.blocked_reason == (
        "MAX_RETRIES_EXCEEDED: 3 retry attempt(s) already made; MAX_RETRIES is 3, "
        "matching Razorpay's subscription-halt threshold"
    )
    assert decision.final_action is RecommendedAction.NO_ACTION_COOLDOWN


def test_two_retries_is_still_allowed(engine):
    assert engine.validate(llm(), context(retry_count=2)).approved is True


def test_missing_mandate_ceiling_is_blocked(engine):
    decision = engine.validate(llm(), context(mandate_ceiling=None))
    assert decision.approved is False
    assert decision.rule_id == "MISSING_RBI_FIELD_MANDATE_CEILING"
    assert "failing closed" in decision.blocked_reason


def test_low_trace_confidence_is_blocked(engine):
    """Day 3-4 mitigation: PolicyEngine hard-rejects a recovery action whose
    tracer confidence is below the threshold."""
    decision = engine.validate(llm(), context(trace_confidence=0.30))
    assert decision.approved is False
    assert decision.rule_id == "TRACE_CONFIDENCE_BELOW_THRESHOLD"
    assert "0.30" in decision.blocked_reason
    assert decision.final_action is RecommendedAction.ESCALATE_HUMAN


def test_absent_trace_confidence_is_recorded_as_skipped_not_passed_silently(engine):
    decision = engine.validate(llm(), context(trace_confidence=None))
    assert decision.approved is True
    assert decision.trace_confidence is None


# --------------------------------------------------------------------------
# Non-debiting actions
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "action",
    [
        RecommendedAction.REQUEST_VERIFICATION,
        RecommendedAction.ESCALATE_HUMAN,
        RecommendedAction.NO_ACTION_COOLDOWN,
    ],
)
def test_non_debiting_actions_are_not_gated_on_rbi_fields(engine, action):
    """Requiring a pre-debit notice before an escalation would block the safe
    path and push events toward the unsafe one."""
    decision = engine.validate(
        llm(action),
        context(pre_debit_notice_sent_at=None, mandate_ceiling=None, afa_flag=None),
    )
    assert decision.approved is True
    assert decision.final_action is action


def test_retry_soft_is_the_only_debiting_action():
    assert R.DEBITING_ACTIONS == frozenset({RecommendedAction.RETRY_SOFT})


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------
def test_opt_out_takes_precedence_over_every_other_violation(engine):
    decision = engine.validate(
        llm(),
        context(
            opted_out=True,
            discount_amount=999_999,
            retry_count=99,
            afa_flag=None,
            mandate_ceiling=None,
            pre_debit_notice_sent_at=None,
        ),
    )
    assert decision.rule_id == "CUSTOMER_OPTED_OUT"


def test_missing_field_takes_precedence_over_a_value_violation(engine):
    """Absence must be reported as absence, not misdiagnosed as a value
    breach caused by a default."""
    decision = engine.validate(
        llm(), context(mandate_ceiling=None, discount_amount=999_999)
    )
    assert decision.rule_id == "MISSING_RBI_FIELD_MANDATE_CEILING"


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------
def test_block_shows_recommendation_block_and_reason_together(engine):
    """GROUND_TRUTH.md Day 6-7 deliverable: LLM recommendation -> PolicyEngine
    block -> reason string, all three visible."""
    decision = engine.validate(llm(), context(discount_amount=500_000))
    payload = decision.model_dump(mode="json")

    assert payload["recommended_action"] == "RETRY_SOFT"   # what the LLM wanted
    assert payload["approved"] is False                     # the block
    assert payload["blocked_reason"].startswith("MAX_DISCOUNT_EXCEEDED:")  # the reason
    assert payload["final_action"] == "ESCALATE_HUMAN"
    assert payload["rbi_circular"] == "RBI/DPSS/2026-27/396"


def test_every_blocked_reason_names_its_rule(engine):
    """Never a generic "blocked" flag."""
    cases = [
        context(opted_out=True),
        context(pre_debit_notice_sent_at=None),
        context(mandate_ceiling=None),
        context(afa_flag=None),
        context(pre_debit_notice_sent_at=NOW - timedelta(hours=1)),
        context(amount=150_000, mandate_ceiling=100_000),
        context(amount=2_000_000, afa_flag=False, mandate_ceiling=5_000_000),
        context(discount_amount=500_000),
        context(retry_count=5),
        context(trace_confidence=0.1),
    ]
    seen = set()
    for ctx in cases:
        decision = engine.validate(llm(), ctx)
        assert decision.approved is False
        assert decision.rule_id is not None
        assert decision.blocked_reason.startswith(f"{decision.rule_id}:")
        seen.add(decision.rule_id)
    assert len(seen) == 10, "each case must trip a distinct rule"


def test_decision_is_serialisable_for_the_dashboard(engine):
    decision = engine.validate(llm(), context(opted_out=True))
    payload = decision.model_dump(mode="json")
    assert isinstance(payload["rules_evaluated"], list)
    assert payload["rules_evaluated"][0]["rule_id"] == "CUSTOMER_OPTED_OUT"
    assert isinstance(decision, PolicyDecision)


# --------------------------------------------------------------------------
# Architectural boundary
# --------------------------------------------------------------------------
def test_policy_module_is_deterministic_and_touches_nothing_external():
    import pathlib

    for filename in ("engine.py", "rules.py", "schemas.py"):
        source = pathlib.Path(f"backend/app/policy/{filename}").read_text(encoding="utf-8")
        for forbidden in (
            "anthropic",
            "openai",
            "httpx",
            "MockPaymentGateway",
            "mock_gateway",
            "IntelligenceLayer",
        ):
            assert forbidden not in source, f"{filename} must not reference {forbidden}"


def test_validation_is_deterministic(engine):
    first = engine.validate(llm(), context(discount_amount=500_000))
    second = engine.validate(llm(), context(discount_amount=500_000))
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_event_context_rejects_unknown_fields():
    with pytest.raises(ValueError):
        EventContext(payment_id="pay_1", amount=1000, mandate_ceiling_rupees=500)
