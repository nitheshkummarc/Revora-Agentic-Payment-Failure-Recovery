"""Policy rule set, including the RBI e-mandate compliance checks.

The constants below are the rules themselves, not illustrative values. Where no
authoritative figure exists for something, the comment says so rather than
filling the gap with a plausible-looking number.

Units. The regulatory limits are expressed in rupees (MAX_DISCOUNT = 500,
AFA_REQUIRED_ABOVE = 15000) while payment amounts arrive in paise, Razorpay's
integer-paise convention where 50000 means Rs.500. Mixing the two silently
would be a compliance bug rather than merely an arithmetic one, so the rupee
figures are kept as written and every comparison runs through an explicit
derived paise constant. Nothing here compares a rupee value to a paise value.

Citation discipline. Decisions cite the circular number
(RBI/DPSS/2026-27/396) and never a section number. Section numbers are not
verified against the source text, so none are claimed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from app.intelligence.schemas import RecommendedAction

# --------------------------------------------------------------------------
# Regulatory and dunning limits
# --------------------------------------------------------------------------
MAX_RETRIES = 3  # matches Razorpay's own subscription-halt threshold
MAX_DISCOUNT = 500  # Rs
MANDATE_CEILING_CHECK = True  # variable-amount mandate -- must not exceed customer ceiling
PRE_DEBIT_NOTICE_HOURS = 24  # retry cannot fire without 24h notice already sent
AFA_REQUIRED_ABOVE = 15000  # Rs -- above this, action needs AFA flag = true, else auto-block
COOLDOWN_AFTER_OPT_OUT = "permanent"  # opted-out customers -- hard block, no override

# --------------------------------------------------------------------------
# Derived paise constants. Comparisons use these; the rupee figures above stay
# untouched so they remain greppable against the specification.
# --------------------------------------------------------------------------
PAISE_PER_RUPEE = 100
MAX_DISCOUNT_PAISE = MAX_DISCOUNT * PAISE_PER_RUPEE  # 50_000 paise
AFA_REQUIRED_ABOVE_PAISE = AFA_REQUIRED_ABOVE * PAISE_PER_RUPEE  # 1_500_000 paise

# --------------------------------------------------------------------------
# Recorded but NOT enforced.
#
# The circular sets AFA thresholds of Rs.15,000 generally and Rs.1 lakh for
# SIPs and insurance. Only the general threshold is enforced: applying the
# higher one requires a mandate-category field that events do not yet carry.
# The value is recorded here so the distinction is visible, but no code path
# consults it. Do not describe it as enforced.
# --------------------------------------------------------------------------
AFA_REQUIRED_ABOVE_SIP_INSURANCE = 100000  # Rs 1 lakh -- documented, not enforced

# --------------------------------------------------------------------------
# Minimum tracer confidence required to act autonomously. A local design
# choice with no regulatory basis; do not describe it as an RBI or Razorpay
# figure.
#
# Redundant by construction today: the tracer already marks anything below 0.60
# ambiguous, and the recommendation layer short-circuits ambiguous traces to a
# non-debiting action. It is retained as defence-in-depth against rule drift --
# if a later change lets a low-confidence trace through, this still catches it.
# --------------------------------------------------------------------------
MINIMUM_TRACE_CONFIDENCE = 0.60

#: The circular the specification's Appendix names as primary source. Number
#: only -- never a section number.
RBI_CIRCULAR = "RBI/DPSS/2026-27/396"

#: Actions that actually debit a customer or re-attempt their payment. Only
#: these are gated on the RBI fields; the rest read state or stop.
DEBITING_ACTIONS = frozenset({RecommendedAction.RETRY_SOFT})

#: Actions an opt-out blocks.
#:
#: Design choice, deliberately narrower than "every action":
#:
#:   RETRY_SOFT           blocked -- re-attempts the customer's payment.
#:   REQUEST_VERIFICATION blocked -- continues the recovery workflow; its
#:                        follow-through reconciles or escalates onward, which
#:                        is exactly the activity an opt-out ends.
#:   ESCALATE_HUMAN       permitted -- terminates the automated path and hands
#:                        the case to a reviewer. It neither contacts nor
#:                        charges the customer, and blocking it would remove
#:                        human oversight from the one population most in need
#:                        of it.
#:   NO_ACTION_COOLDOWN   permitted -- already the outcome the opt-out rule
#:                        wants. Blocking a no-op only to substitute the same
#:                        no-op changes nothing operationally, while inflating
#:                        the blocked-action count that the batch report
#:                        presents as evidence the guardrail works.
#:
#: The opt-out is still recorded on every decision either way, so an audit
#: shows the customer's status even when the action was permitted.
OPT_OUT_BLOCKED_ACTIONS = frozenset(
    {RecommendedAction.RETRY_SOFT, RecommendedAction.REQUEST_VERIFICATION}
)


class RuleId(str, Enum):
    """Every rule that can block. The value is what appears in
    `blocked_reason`, so a block always names the rule that fired."""

    CUSTOMER_OPTED_OUT = "CUSTOMER_OPTED_OUT"
    MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT = "MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT"
    MISSING_RBI_FIELD_MANDATE_CEILING = "MISSING_RBI_FIELD_MANDATE_CEILING"
    MISSING_RBI_FIELD_AFA_FLAG = "MISSING_RBI_FIELD_AFA_FLAG"
    PRE_DEBIT_NOTICE_TOO_RECENT = "PRE_DEBIT_NOTICE_TOO_RECENT"
    MANDATE_CEILING_EXCEEDED = "MANDATE_CEILING_EXCEEDED"
    AFA_REQUIRED_AND_MISSING = "AFA_REQUIRED_AND_MISSING"
    MAX_DISCOUNT_EXCEEDED = "MAX_DISCOUNT_EXCEEDED"
    MAX_RETRIES_EXCEEDED = "MAX_RETRIES_EXCEEDED"
    TRACE_CONFIDENCE_BELOW_THRESHOLD = "TRACE_CONFIDENCE_BELOW_THRESHOLD"


class Violation:
    """A single rule failure: which rule, why, and what to do instead."""

    __slots__ = ("rule_id", "detail", "final_action", "cites_rbi")

    def __init__(
        self,
        rule_id: RuleId,
        detail: str,
        final_action: RecommendedAction,
        cites_rbi: bool = False,
    ) -> None:
        self.rule_id = rule_id
        self.detail = detail
        self.final_action = final_action
        self.cites_rbi = cites_rbi

    @property
    def blocked_reason(self) -> str:
        """`<RULE_ID>: <explanation with the actual numbers>`.

        Never a generic "blocked" flag -- the rule that fired is always named.
        """
        suffix = f" [{RBI_CIRCULAR}]" if self.cites_rbi else ""
        return f"{self.rule_id.value}: {self.detail}{suffix}"


def _rupees(paise: int) -> str:
    return f"Rs.{paise / PAISE_PER_RUPEE:,.2f}"


# --------------------------------------------------------------------------
# Individual rules. Each returns a Violation or None.
# Each takes the already-validated context; none of them reach outside.
# --------------------------------------------------------------------------
def check_opted_out(
    opted_out: bool, recommended_action: RecommendedAction
) -> Optional[Violation]:
    """Permanent cooldown for opted-out customers, with no override.

    Scope is governed by OPT_OUT_BLOCKED_ACTIONS: the block covers every action
    that would continue the recovery workflow, and permits the two that end it
    without touching the customer. Within its scope there is no override -- no
    combination of other fields can rescue a blocked action.
    """
    if not opted_out:
        return None
    if recommended_action not in OPT_OUT_BLOCKED_ACTIONS:
        return None
    return Violation(
        rule_id=RuleId.CUSTOMER_OPTED_OUT,
        detail=(
            f"customer has opted out; cooldown is '{COOLDOWN_AFTER_OPT_OUT}', so "
            f"{recommended_action.value} is blocked with no override, regardless "
            "of any other field"
        ),
        final_action=RecommendedAction.NO_ACTION_COOLDOWN,
        cites_rbi=True,
    )


def check_required_rbi_fields(
    pre_debit_notice_sent_at: Optional[datetime],
    mandate_ceiling: Optional[int],
    afa_flag: Optional[bool],
) -> Optional[Violation]:
    """Fail closed: a missing RBI field is a block, never a pass-through.

    "Every recovery action must fail closed (block,
    not act) if any RBI-required field is missing or unmet." Missing is checked
    before any value comparison, so an absent field can never be read as a
    passing value.
    """
    if pre_debit_notice_sent_at is None:
        return Violation(
            rule_id=RuleId.MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT,
            detail=(
                "required RBI field `pre_debit_notice_sent_at` is missing from the "
                "event; failing closed rather than treating absence as compliance"
            ),
            final_action=RecommendedAction.ESCALATE_HUMAN,
            cites_rbi=True,
        )
    if mandate_ceiling is None:
        return Violation(
            rule_id=RuleId.MISSING_RBI_FIELD_MANDATE_CEILING,
            detail=(
                "required RBI field `mandate_ceiling` is missing from the event; "
                "failing closed rather than treating absence as compliance"
            ),
            final_action=RecommendedAction.ESCALATE_HUMAN,
            cites_rbi=True,
        )
    if afa_flag is None:
        return Violation(
            rule_id=RuleId.MISSING_RBI_FIELD_AFA_FLAG,
            detail=(
                "required RBI field `afa_flag` is missing from the event; failing "
                "closed rather than treating absence as compliance"
            ),
            final_action=RecommendedAction.ESCALATE_HUMAN,
            cites_rbi=True,
        )
    return None


def check_pre_debit_notice(
    pre_debit_notice_sent_at: datetime, now: datetime
) -> Optional[Violation]:
    """PRE_DEBIT_NOTICE_HOURS = 24 -- the notice must ALREADY have been sent.

    The notice must predate the debit by at least 24 hours, so the test is on
    elapsed time since it was sent. A notice sent two hours ago does not
    authorise a retry now; a notice dated in the future is invalid evidence.
    """
    elapsed = now - pre_debit_notice_sent_at
    required = timedelta(hours=PRE_DEBIT_NOTICE_HOURS)
    if elapsed < timedelta(0):
        return Violation(
            rule_id=RuleId.PRE_DEBIT_NOTICE_TOO_RECENT,
            detail=(
                f"`pre_debit_notice_sent_at` is in the future "
                f"({pre_debit_notice_sent_at.isoformat()}); cannot satisfy the "
                f"{PRE_DEBIT_NOTICE_HOURS}h pre-debit notice requirement"
            ),
            final_action=RecommendedAction.ESCALATE_HUMAN,
            cites_rbi=True,
        )
    if elapsed < required:
        hours = elapsed.total_seconds() / 3600
        return Violation(
            rule_id=RuleId.PRE_DEBIT_NOTICE_TOO_RECENT,
            detail=(
                f"pre-debit notice was sent {hours:.1f}h ago but "
                f"PRE_DEBIT_NOTICE_HOURS requires {PRE_DEBIT_NOTICE_HOURS}h to have "
                "already elapsed before a retry may fire"
            ),
            final_action=RecommendedAction.NO_ACTION_COOLDOWN,
            cites_rbi=True,
        )
    return None


def check_mandate_ceiling(amount_paise: int, mandate_ceiling_paise: int) -> Optional[Violation]:
    """MANDATE_CEILING_CHECK -- a variable-amount mandate action must not
    exceed the ceiling the customer set. Equal to the ceiling is allowed."""
    if not MANDATE_CEILING_CHECK:
        return None
    if amount_paise <= mandate_ceiling_paise:
        return None
    return Violation(
        rule_id=RuleId.MANDATE_CEILING_EXCEEDED,
        detail=(
            f"action amount {_rupees(amount_paise)} exceeds the customer-set "
            f"variable-amount mandate ceiling of {_rupees(mandate_ceiling_paise)}"
        ),
        final_action=RecommendedAction.ESCALATE_HUMAN,
        cites_rbi=True,
    )


def check_afa(amount_paise: int, afa_flag: bool) -> Optional[Violation]:
    """AFA_REQUIRED_ABOVE = 15000 Rs -- strictly above the threshold, an
    action needs afa_flag = true, else auto-block."""
    if amount_paise <= AFA_REQUIRED_ABOVE_PAISE:
        return None
    if afa_flag is True:
        return None
    return Violation(
        rule_id=RuleId.AFA_REQUIRED_AND_MISSING,
        detail=(
            f"action amount {_rupees(amount_paise)} is above the "
            f"AFA_REQUIRED_ABOVE threshold of Rs.{AFA_REQUIRED_ABOVE:,} and "
            f"`afa_flag` is {afa_flag!r}; additional factor of authentication is "
            "required, so the action is auto-blocked"
        ),
        final_action=RecommendedAction.ESCALATE_HUMAN,
        cites_rbi=True,
    )


def check_max_discount(discount_paise: int) -> Optional[Violation]:
    """MAX_DISCOUNT = 500 Rs. Exactly at the cap is allowed; above it blocks."""
    if discount_paise <= MAX_DISCOUNT_PAISE:
        return None
    return Violation(
        rule_id=RuleId.MAX_DISCOUNT_EXCEEDED,
        detail=(
            f"proposed discount {_rupees(discount_paise)} exceeds MAX_DISCOUNT of "
            f"Rs.{MAX_DISCOUNT:,}"
        ),
        final_action=RecommendedAction.ESCALATE_HUMAN,
    )


def check_max_retries(retry_count: int) -> Optional[Violation]:
    """MAX_RETRIES = 3, matching Razorpay's own subscription-halt threshold
    (halted after exactly 3 charge-retry attempts). No deviation, so no
    deviation reason is needed."""
    if retry_count < MAX_RETRIES:
        return None
    return Violation(
        rule_id=RuleId.MAX_RETRIES_EXCEEDED,
        detail=(
            f"{retry_count} retry attempt(s) already made; MAX_RETRIES is "
            f"{MAX_RETRIES}, matching Razorpay's subscription-halt threshold"
        ),
        final_action=RecommendedAction.NO_ACTION_COOLDOWN,
    )


def check_trace_confidence(trace_confidence: float) -> Optional[Violation]:
    """Reject an autonomous recovery whose diagnosis is not confident enough.

    UNREACHABLE ON THE NORMAL PIPELINE -- a backstop, not an independently
    exercised rule. The chain that makes it unreachable:

        tracer sets ambiguous=True whenever confidence < 0.60
          -> the intelligence layer short-circuits every ambiguous trace to
             REQUEST_VERIFICATION without consulting the model
          -> REQUEST_VERIFICATION is not a debiting action
          -> the engine returns before value rules are evaluated

    So a debiting action can only arrive here carrying a confidence at or above
    the tracer's own ambiguity threshold, and the comparison below can never be
    true. It is retained because that guarantee lives in two other modules: if
    either threshold moves, or a caller assembles a recommendation without
    going through the intelligence layer, this catches what the chain no longer
    does. The logic is exercised directly by tests rather than through the
    pipeline.
    """
    if trace_confidence >= MINIMUM_TRACE_CONFIDENCE:
        return None
    return Violation(
        rule_id=RuleId.TRACE_CONFIDENCE_BELOW_THRESHOLD,
        detail=(
            f"tracer confidence {trace_confidence:.2f} is below the "
            f"{MINIMUM_TRACE_CONFIDENCE:.2f} minimum required to act autonomously; "
            "routing to human/status-check instead of letting the agent decide"
        ),
        final_action=RecommendedAction.ESCALATE_HUMAN,
    )
