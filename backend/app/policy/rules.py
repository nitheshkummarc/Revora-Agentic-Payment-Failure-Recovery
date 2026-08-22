"""PolicyEngine rule set (Module 5).

Every constant below is transcribed from GROUND_TRUTH.md's Day 6-7 code block.
They are not example values -- they are the rules. Where GROUND_TRUTH.md does
not supply a number, that is stated in the comment rather than filled in with a
plausible-looking one.

**Units.** GROUND_TRUTH.md states the money rules in rupees (MAX_DISCOUNT = 500
Rs, AFA_REQUIRED_ABOVE = 15000 Rs) but the dataset carries `amount` in paise,
Razorpay's integer-paise convention (Days 11-13: "50000 = Rs.500"). Mixing the
two silently would be a compliance bug, not just an arithmetic one, so the
rupee figures are kept verbatim below and every comparison is done in paise via
an explicit derived constant. Nothing in this module compares a rupee value to
a paise value.

**Citation discipline.** Decisions cite the circular NUMBER
(RBI/DPSS/2026-27/396) and never a section number. GROUND_TRUTH.md's Appendix
warns that judges may check claimed section numbers against the real circular,
and the section numbers are not in GROUND_TRUTH.md -- so they are not claimed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from app.intelligence.schemas import RecommendedAction

# --------------------------------------------------------------------------
# GROUND_TRUTH.md Day 6-7 rule set, verbatim
# --------------------------------------------------------------------------
MAX_RETRIES = 3  # matches Razorpay's own subscription-halt threshold
MAX_DISCOUNT = 500  # Rs
MANDATE_CEILING_CHECK = True  # variable-amount mandate -- must not exceed customer ceiling
PRE_DEBIT_NOTICE_HOURS = 24  # retry cannot fire without 24h notice already sent
AFA_REQUIRED_ABOVE = 15000  # Rs -- above this, action needs AFA flag = true, else auto-block
COOLDOWN_AFTER_OPT_OUT = "permanent"  # opted-out customers -- hard block, no override

# --------------------------------------------------------------------------
# Derived paise constants. Comparisons use these; the rupee figures above stay
# untouched so they remain greppable against GROUND_TRUTH.md.
# --------------------------------------------------------------------------
PAISE_PER_RUPEE = 100
MAX_DISCOUNT_PAISE = MAX_DISCOUNT * PAISE_PER_RUPEE  # 50_000 paise
AFA_REQUIRED_ABOVE_PAISE = AFA_REQUIRED_ABOVE * PAISE_PER_RUPEE  # 1_500_000 paise

# --------------------------------------------------------------------------
# Documented but NOT enforced.
#
# GROUND_TRUTH.md's Appendix records the circular's AFA thresholds as
# "Rs.15,000 general / Rs.1 lakh for SIPs and insurance". Only the general
# threshold is in the Day 6-7 rule block, and applying the higher one would
# require a mandate-category field that the Days 11-13 dataset spec does not
# define. Rather than invent that field, the higher threshold is recorded here
# for the pitch deck and left unenforced. Do not claim the code enforces it.
# --------------------------------------------------------------------------
AFA_REQUIRED_ABOVE_SIP_INSURANCE = 100000  # Rs 1 lakh -- documented, not enforced

# --------------------------------------------------------------------------
# NOT from GROUND_TRUTH.md's rule block. Sourced from its Day 3-4 mitigation
# table, which assigns this rule to the PolicyEngine explicitly: "PolicyEngine
# hard-rejects any recovery action where confidence < threshold and routes to
# 'needs human/status-check'". GROUND_TRUTH.md does not state the threshold, so
# this number is a RecoverX design choice -- do not present it as an RBI or
# Razorpay figure.
#
# Redundant by construction today (Module 3 already marks anything below 0.60
# as ambiguous, and Module 4 short-circuits ambiguous traces to a non-debiting
# action). It is kept as defence-in-depth against exactly the "set-and-forget
# drift" failure mode GROUND_TRUTH.md Day 6-7 warns about: if a later change to
# Module 3 lets a low-confidence trace through, this still catches it.
# --------------------------------------------------------------------------
MINIMUM_TRACE_CONFIDENCE = 0.60

#: The circular GROUND_TRUTH.md's Appendix names as primary source. Number
#: only -- never a section number.
RBI_CIRCULAR = "RBI/DPSS/2026-27/396"

#: Actions that actually debit a customer or re-attempt their payment. Only
#: these are gated on the RBI fields; the rest read state or stop.
DEBITING_ACTIONS = frozenset({RecommendedAction.RETRY_SOFT})


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
def check_opted_out(opted_out: bool) -> Optional[Violation]:
    """COOLDOWN_AFTER_OPT_OUT = "permanent" -- hard block, no override.

    Deliberately blocks EVERY action, not just debiting ones. "No override"
    is read strictly: nothing at all happens for an opted-out customer. That
    is the stricter direction, which is the defensible one for a compliance
    claim.
    """
    if not opted_out:
        return None
    return Violation(
        rule_id=RuleId.CUSTOMER_OPTED_OUT,
        detail=(
            "customer has opted out; COOLDOWN_AFTER_OPT_OUT is "
            f"'{COOLDOWN_AFTER_OPT_OUT}' so this is a hard block with no override, "
            "regardless of any other field"
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

    GROUND_TRUTH.md Day 6-7: "Every recovery action must fail closed (block,
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
    """See MINIMUM_TRACE_CONFIDENCE above -- Day 3-4 mitigation, RecoverX-chosen
    threshold, defence-in-depth."""
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
