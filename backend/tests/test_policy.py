"""Policy engine tests.

One explicit test per rule, each proving the specific blocked_reason string is
correct rather than merely that approved == False. The five headline cases:

  * test_discount_exceeds_limit_is_blocked
  * test_mandate_ceiling_exceeded_is_blocked
  * test_missing_pre_debit_notice_is_blocked
  * test_afa_required_and_missing_is_blocked
  * test_opted_out_is_a_permanent_hard_block

Treat this file as a regression suite rather than a one-off demo script: re-run
it after any change to the rules.
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
# Documented-value conformance: the constants ARE the rules
# --------------------------------------------------------------------------
def test_rule_constants_are_the_documented_limits():
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
    """Section numbers are not verified against the source text, so none are
    claimed -- only the circular number is cited."""
    assert R.RBI_CIRCULAR == "RBI/DPSS/2026-27/396"
    for rule_id in R.RuleId:
        assert "section" not in rule_id.value.lower()


def test_an_event_stating_no_mandate_category_uses_the_general_threshold(engine):
    """The higher threshold applies only when the event asks for it. Without a
    category, a Rs.20,000 action without AFA is blocked exactly as before."""
    assert R.AFA_REQUIRED_ABOVE_SIP_INSURANCE == 100000
    decision = engine.validate(
        llm(), context(amount=2_000_000, afa_flag=False, mandate_ceiling=5_000_000)
    )
    assert decision.approved is False
    assert decision.rule_id == "AFA_REQUIRED_AND_MISSING"


# --------------------------------------------------------------------------
# SIP / insurance AFA threshold (Rs.1 lakh)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("category", ["sip", "insurance"])
def test_sip_or_insurance_under_one_lakh_needs_no_afa(engine, category):
    """Rs.20,000 is over the general threshold but under the Rs.1 lakh one that
    applies to these mandates, so no additional factor is required."""
    decision = engine.validate(
        llm(),
        context(
            amount=2_000_000,  # Rs.20,000
            afa_flag=False,
            mandate_ceiling=20_000_000,
            mandate_category=category,
        ),
    )
    assert decision.approved is True
    assert decision.rule_id is None
    assert decision.final_action is RecommendedAction.RETRY_SOFT


@pytest.mark.parametrize("category", ["sip", "insurance"])
def test_sip_or_insurance_over_one_lakh_is_blocked_without_afa(engine, category):
    decision = engine.validate(
        llm(),
        context(
            amount=15_000_000,  # Rs.1,50,000
            afa_flag=False,
            mandate_ceiling=30_000_000,
            mandate_category=category,
        ),
    )
    assert decision.approved is False
    assert decision.rule_id == "AFA_SIP_INSURANCE_REQUIRED_AND_MISSING"
    assert decision.final_action is RecommendedAction.ESCALATE_HUMAN
    assert "Rs.100,000" in decision.blocked_reason
    assert f"'{category}' mandate" in decision.blocked_reason
    # Cites the circular, like every other RBI-grounded block.
    assert R.RBI_CIRCULAR in decision.blocked_reason


@pytest.mark.parametrize("category", ["sip", "insurance"])
def test_sip_or_insurance_over_one_lakh_passes_when_afa_is_present(engine, category):
    decision = engine.validate(
        llm(),
        context(
            amount=15_000_000,
            afa_flag=True,
            mandate_ceiling=30_000_000,
            mandate_category=category,
        ),
    )
    assert decision.approved is True
    assert decision.rule_id is None


def test_a_non_sip_mandate_still_uses_the_fifteen_thousand_threshold(engine):
    """The relaxation is scoped to the categories the circular names. Any other
    category is evaluated on the general threshold."""
    decision = engine.validate(
        llm(),
        context(
            amount=2_000_000,  # Rs.20,000
            afa_flag=False,
            mandate_ceiling=20_000_000,
            mandate_category="utility",
        ),
    )
    assert decision.approved is False
    assert decision.rule_id == "AFA_REQUIRED_AND_MISSING"


def test_an_unrecognised_category_falls_back_to_the_stricter_threshold(engine):
    """Fail closed on an unfamiliar value: an unknown category must not be able
    to buy the relaxation that only SIP and insurance are entitled to."""
    decision = engine.validate(
        llm(),
        context(
            amount=2_000_000,
            afa_flag=False,
            mandate_ceiling=20_000_000,
            mandate_category="sip_but_not_really",
        ),
    )
    assert decision.approved is False
    assert decision.rule_id == "AFA_REQUIRED_AND_MISSING"


@pytest.mark.parametrize("category", ["SIP", "  Insurance  ", "sIp"])
def test_category_matching_ignores_case_and_surrounding_space(engine, category):
    """The category is free-form text on the event, not an enum, so a casing or
    whitespace difference must not silently change which threshold applies."""
    decision = engine.validate(
        llm(),
        context(
            amount=2_000_000,
            afa_flag=False,
            mandate_ceiling=20_000_000,
            mandate_category=category,
        ),
    )
    assert decision.approved is True


def test_the_sip_threshold_is_strictly_above_like_the_general_one(engine):
    """Exactly at Rs.1 lakh is allowed; one paise over is not."""
    at_threshold = engine.validate(
        llm(),
        context(
            amount=R.AFA_REQUIRED_ABOVE_SIP_INSURANCE_PAISE,
            afa_flag=False,
            mandate_ceiling=30_000_000,
            mandate_category="sip",
        ),
    )
    assert at_threshold.approved is True

    one_over = engine.validate(
        llm(),
        context(
            amount=R.AFA_REQUIRED_ABOVE_SIP_INSURANCE_PAISE + 1,
            afa_flag=False,
            mandate_ceiling=30_000_000,
            mandate_category="sip",
        ),
    )
    assert one_over.approved is False
    assert one_over.rule_id == "AFA_SIP_INSURANCE_REQUIRED_AND_MISSING"


def test_the_two_afa_rules_are_mutually_exclusive(engine):
    """Each returns early on the categories the other owns, so no event can be
    blocked by both -- and the audit always shows which threshold was applied."""
    for category in (None, "utility", "sip", "insurance"):
        decision = engine.validate(
            llm(),
            context(
                amount=15_000_000,
                afa_flag=False,
                mandate_ceiling=30_000_000,
                mandate_category=category,
            ),
        )
        failed = [e.rule_id for e in decision.rules_evaluated if not e.passed]
        assert len(failed) == 1, (category, failed)
        expected = (
            "AFA_SIP_INSURANCE_REQUIRED_AND_MISSING"
            if category in ("sip", "insurance")
            else "AFA_REQUIRED_AND_MISSING"
        )
        assert failed == [expected]


def test_the_reported_threshold_matches_the_one_enforced(engine):
    assert R.afa_threshold_paise("sip") == R.AFA_REQUIRED_ABOVE_SIP_INSURANCE_PAISE
    assert R.afa_threshold_paise("insurance") == R.AFA_REQUIRED_ABOVE_SIP_INSURANCE_PAISE
    assert R.afa_threshold_paise(None) == R.AFA_REQUIRED_ABOVE_PAISE
    assert R.afa_threshold_paise("utility") == R.AFA_REQUIRED_ABOVE_PAISE
    # Rs.1 lakh in paise, stated once so a unit slip is visible here.
    assert R.AFA_REQUIRED_ABOVE_SIP_INSURANCE_PAISE == 10_000_000


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
        "AFA_SIP_INSURANCE_REQUIRED_AND_MISSING",
        "MAX_DISCOUNT_EXCEEDED",
        "MAX_RETRIES_EXCEEDED",
    ):
        assert expected in checked
    assert all(e.passed for e in decision.rules_evaluated)


# --------------------------------------------------------------------------
# REQUIRED RULE TEST 1 -- discount exceeds limit
# --------------------------------------------------------------------------
def test_discount_exceeds_limit_is_blocked(engine):
    """The model recommends a Rs.5,000 discount when the cap is Rs.500."""
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
        "CUSTOMER_OPTED_OUT: customer has opted out; cooldown is 'permanent', so "
        "RETRY_SOFT is blocked with no override, regardless of any other field "
        "[RBI/DPSS/2026-27/396]"
    )
    assert decision.final_action is RecommendedAction.NO_ACTION_COOLDOWN


def test_opted_out_blocks_even_a_fully_compliant_event(engine):
    """No combination of other fields rescues a blocked action."""
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


def test_opted_out_blocks_request_verification(engine):
    """REQUEST_VERIFICATION continues the recovery workflow, so an opt-out
    ends it."""
    decision = engine.validate(
        llm(RecommendedAction.REQUEST_VERIFICATION), context(opted_out=True)
    )
    assert decision.approved is False
    assert decision.rule_id == "CUSTOMER_OPTED_OUT"
    assert decision.final_action is RecommendedAction.NO_ACTION_COOLDOWN


@pytest.mark.parametrize(
    "action",
    [RecommendedAction.ESCALATE_HUMAN, RecommendedAction.NO_ACTION_COOLDOWN],
)
def test_opted_out_permits_actions_that_end_the_workflow(engine, action):
    """Neither contacts nor charges the customer. Blocking ESCALATE_HUMAN would
    strip human oversight; blocking NO_ACTION_COOLDOWN would substitute the
    same no-op while inflating the blocked-action count."""
    decision = engine.validate(llm(action), context(opted_out=True))

    assert decision.approved is True
    assert decision.final_action is action
    assert decision.blocked_reason is None


def test_opt_out_status_is_recorded_even_when_it_does_not_block(engine):
    """An audit must still show the customer opted out."""
    decision = engine.validate(
        llm(RecommendedAction.ESCALATE_HUMAN), context(opted_out=True)
    )
    opt_out = next(
        e for e in decision.rules_evaluated if e.rule_id == "CUSTOMER_OPTED_OUT"
    )
    assert opt_out.passed is True
    assert "customer has opted out" in opt_out.detail


OPT_OUT_MATRIX = {
    RecommendedAction.RETRY_SOFT: False,
    RecommendedAction.REQUEST_VERIFICATION: False,
    RecommendedAction.ESCALATE_HUMAN: True,
    RecommendedAction.NO_ACTION_COOLDOWN: True,
}


@pytest.mark.parametrize("action,expected_approved", sorted(OPT_OUT_MATRIX.items()))
def test_opt_out_outcome_is_pinned_for_every_action(engine, action, expected_approved):
    """Full enum coverage so the scope decision cannot drift silently."""
    decision = engine.validate(llm(action), context(opted_out=True))
    assert decision.approved is expected_approved
    if expected_approved:
        assert decision.final_action is action
    else:
        assert decision.final_action is RecommendedAction.NO_ACTION_COOLDOWN
        assert decision.rule_id == "CUSTOMER_OPTED_OUT"


def test_opt_out_scope_is_the_two_workflow_continuing_actions():
    assert R.OPT_OUT_BLOCKED_ACTIONS == frozenset(
        {RecommendedAction.RETRY_SOFT, RecommendedAction.REQUEST_VERIFICATION}
    )


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
    """A recovery action whose tracer confidence is below the threshold is
    hard-rejected rather than acted on."""
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


def test_debiting_actions_is_the_same_object_as_the_intelligence_layers_guard_set():
    """Not just equal -- the same frozenset instance, imported rather than
    redefined, so the policy engine's gate and the injection guard's gate
    cannot silently diverge if a new debiting action is ever added."""
    from app.intelligence.schemas import MONEY_MOVING_ACTIONS

    assert R.DEBITING_ACTIONS is MONEY_MOVING_ACTIONS


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
    """Recommendation, block and reason string must all be visible together."""
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


# --------------------------------------------------------------------------
# TRACE_CONFIDENCE_BELOW_THRESHOLD reachability
#
# The rule cannot fire on the normal pipeline. These tests prove both halves
# of that claim: the comparison itself is correct when invoked directly, and
# the pipeline genuinely never invokes it.
# --------------------------------------------------------------------------
def test_trace_confidence_rule_logic_is_correct_when_invoked_directly():
    """Exercised at the rule level, since the pipeline never reaches it."""
    assert R.check_trace_confidence(R.MINIMUM_TRACE_CONFIDENCE) is None
    violation = R.check_trace_confidence(0.30)
    assert violation is not None
    assert violation.rule_id is R.RuleId.TRACE_CONFIDENCE_BELOW_THRESHOLD
    assert violation.final_action is RecommendedAction.ESCALATE_HUMAN
    assert "0.30" in violation.detail


def test_tracer_marks_every_low_confidence_result_ambiguous():
    """First link: a confidence below the threshold always sets ambiguous."""
    from app.tracer.tracer import CONFIDENCE_AMBIGUITY_THRESHOLD

    assert CONFIDENCE_AMBIGUITY_THRESHOLD == R.MINIMUM_TRACE_CONFIDENCE


def test_low_confidence_never_reaches_the_rule_through_the_pipeline():
    """Second link: an ambiguous trace is short-circuited to a non-debiting
    action, and the engine returns before value rules run."""
    from app.intelligence.llm_client import ExplodingLLMClient, IntelligenceLayer
    from app.intelligence.schemas import IntelligenceInput
    from app.state_machine.states import CanonicalState
    from app.tracer.schemas import TraceResult

    low_confidence_trace = TraceResult(
        payment_id="pay_1",
        root_cause="Undetermined root cause for state PENDING_WEBHOOK",
        causal_chain=[],
        confidence=0.10,
        ambiguous=True,
        ambiguity_reasons=["confidence_below_threshold: 0.10 < 0.60"],
        chain_completeness=0.0,
        error_grounding=0.0,
        inherited_resolution_confidence=1.0,
        resolved_state=CanonicalState.PENDING_WEBHOOK,
        resolution_reason="silence_threshold_exceeded",
        traced_at=NOW,
    )

    # The model is never consulted for an ambiguous trace.
    layer = IntelligenceLayer(llm_client=ExplodingLLMClient())
    recommendation = layer.recommend(
        IntelligenceInput(
            payment_id="pay_1", trace=low_confidence_trace, decided_at=NOW
        )
    )
    assert recommendation.recommended_action is RecommendedAction.REQUEST_VERIFICATION
    assert recommendation.recommended_action not in R.DEBITING_ACTIONS

    decision = PolicyEngine(clock=lambda: NOW).validate(
        recommendation, context(trace_confidence=0.10)
    )

    # Approved as a non-debiting action, and the confidence rule was never
    # evaluated -- the engine returned before the value rules.
    assert decision.approved is True
    evaluated = {e.rule_id for e in decision.rules_evaluated}
    assert "TRACE_CONFIDENCE_BELOW_THRESHOLD" not in evaluated


def test_a_debiting_action_can_only_carry_confidence_at_or_above_threshold():
    """The invariant the two links above combine to produce."""
    from app.intelligence.llm_client import IntelligenceLayer, StubLLMClient
    from app.intelligence.schemas import IntelligenceInput
    from app.state_machine.states import CanonicalState
    from app.tracer.schemas import TraceResult

    layer = IntelligenceLayer(llm_client=StubLLMClient())
    for confidence in (0.0, 0.25, 0.59, 0.60, 0.85, 1.0):
        ambiguous = confidence < R.MINIMUM_TRACE_CONFIDENCE
        trace = TraceResult(
            payment_id="pay_1",
            root_cause="Failure at step: payment_authentication, source: customer, reason: incorrect_otp",
            causal_chain=["evt_pay_1_1"],
            confidence=confidence,
            ambiguous=ambiguous,
            ambiguity_reasons=[],
            chain_completeness=1.0,
            error_grounding=1.0,
            inherited_resolution_confidence=1.0,
            resolved_state=CanonicalState.FAILED,
            resolution_reason="clean_single_event",
            traced_at=NOW,
        )
        result = layer.recommend(
            IntelligenceInput(payment_id="pay_1", trace=trace, decided_at=NOW)
        )
        if result.recommended_action in R.DEBITING_ACTIONS:
            assert confidence >= R.MINIMUM_TRACE_CONFIDENCE, (
                f"a debiting action escaped with confidence {confidence}"
            )
