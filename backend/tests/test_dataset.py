"""Conformance tests for the generated synthetic dataset.

The dataset is the only module-boundary artefact that is data rather than code,
so nothing else type-checks it. These tests are that check, and they run in two
directions.

*Contract conformance*: every row's `batch_event` must construct a `BatchEvent`.
That schema forbids unknown keys, so a row carrying an extra field or a renamed
one fails here rather than at batch time.

*Value provenance*: the generator is standalone and keeps its own copy of the
whitelisted error values, the webhook event names and the policy thresholds.
That independence is deliberate -- the script must not import the application --
but a private copy can drift. These tests hold the two copies against each
other, so a threshold changed in the engine fails here instead of silently
producing a dataset whose "violation" rows no longer violate anything.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest

from app.gateway.schemas import ChaosConfig, ChaosMode, ErrorSource, WebhookEventName
from app.intelligence.sanitizer import sanitize_customer_note
from app.orchestrator.orchestrator import BatchEvent
from app.policy import rules as R
from app.state_machine.states import SILENCE_THRESHOLD_SECONDS

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DATASET_PATH = REPO_ROOT / "data" / "synthetic_events_500.json"
GENERATOR_PATH = REPO_ROOT / "data" / "generate_synthetic_dataset.py"

#: Rules the violation bucket is expected to cover. The two omissions are
#: deliberate: opt-out is exercised by the adversarial bucket, and the tracer
#: confidence rule is a backstop that no pipeline path reaches.
EXPECTED_VIOLATION_RULES = {
    "MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT",
    "MISSING_RBI_FIELD_MANDATE_CEILING",
    "MISSING_RBI_FIELD_AFA_FLAG",
    "PRE_DEBIT_NOTICE_TOO_RECENT",
    "MANDATE_CEILING_EXCEEDED",
    "AFA_REQUIRED_AND_MISSING",
    "AFA_SIP_INSURANCE_REQUIRED_AND_MISSING",
    "MAX_DISCOUNT_EXCEEDED",
    "MAX_RETRIES_EXCEEDED",
}

RULES_COVERED_ELSEWHERE = {
    "CUSTOMER_OPTED_OUT",
    "TRACE_CONFIDENCE_BELOW_THRESHOLD",
}

BUCKET_TARGETS = {
    "standard_failure": 0.60,
    "ambiguous": 0.20,
    "policy_violation": 0.10,
    "adversarial": 0.10,
}

#: Operations the batch runner knows how to replay. A row using anything else
#: would seed a payment into a state its scenario does not describe.
SUPPORTED_SEED_OPS = {"create_payment", "fail_payment", "simulate_webhook"}


def _load_generator():
    """Import the generator by path. It lives outside the app package, which is
    the point -- it must remain runnable without the backend installed."""
    spec = importlib.util.spec_from_file_location("generate_synthetic_dataset", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _load_generator()


@pytest.fixture(scope="module")
def dataset() -> Dict[str, Any]:
    assert DATASET_PATH.exists(), (
        f"{DATASET_PATH} is missing; run `python data/generate_synthetic_dataset.py`"
    )
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(dataset) -> List[Dict[str, Any]]:
    return dataset["events"]


def _all_steps(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(row["gateway_seed"]["setup"]) + list(row["gateway_seed"]["at_observation"])


def _bucket(rows, name: str) -> List[Dict[str, Any]]:
    return [r for r in rows if r["bucket"] == name]


# --------------------------------------------------------------------------
# Shape and size
# --------------------------------------------------------------------------
def test_dataset_holds_between_200_and_500_events(dataset, rows):
    assert 200 <= len(rows) <= 500
    assert dataset["count"] == len(rows)


def test_row_ids_and_payment_ids_are_unique(rows):
    """A duplicate payment id would collide in the gateway's store and the
    second row would seed on top of the first."""
    assert len({r["row_id"] for r in rows}) == len(rows)
    assert len({r["batch_event"]["payment_id"] for r in rows}) == len(rows)


def test_bucket_proportions_match_their_targets(rows):
    total = len(rows)
    for bucket, share in BUCKET_TARGETS.items():
        actual = len(_bucket(rows, bucket))
        expected = round(total * share)
        assert abs(actual - expected) <= 1, f"{bucket}: expected ~{expected}, got {actual}"


def test_every_row_belongs_to_a_declared_bucket(rows):
    assert {r["bucket"] for r in rows} == set(BUCKET_TARGETS)


# --------------------------------------------------------------------------
# Contract conformance with the orchestrator
# --------------------------------------------------------------------------
def test_every_row_constructs_a_batch_event(rows):
    """BatchEvent forbids unknown keys, so this rejects a row with an extra or
    renamed field rather than letting it reach the batch."""
    for row in rows:
        event = BatchEvent(**row["batch_event"])
        assert event.payment_id == row["batch_event"]["payment_id"]


def test_batch_event_keys_match_the_contract_exactly(rows):
    expected = set(BatchEvent.model_fields)
    for row in rows:
        assert set(row["batch_event"]) == expected, row["row_id"]


def test_mandate_category_is_part_of_the_batch_event(rows):
    """It selects which AFA threshold applies, so it has to reach the policy
    engine. It lives on the BatchEvent itself and nowhere else -- a second copy
    at row level could drift from the one actually enforced."""
    assert "mandate_category" in BatchEvent.model_fields
    for row in rows:
        assert row["batch_event"]["mandate_category"]
        assert "mandate_category" not in row
    assert len({r["batch_event"]["mandate_category"] for r in rows}) > 1


def test_the_dataset_exercises_both_afa_thresholds(rows):
    """Both sides of the new rule need rows, or the batch proves only one."""
    categories = {r["batch_event"]["mandate_category"] for r in rows}
    assert categories & R.SIP_INSURANCE_MANDATE_CATEGORIES
    assert categories - R.SIP_INSURANCE_MANDATE_CATEGORIES


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------
def test_all_money_fields_are_integer_paise(rows):
    """Rupee figures anywhere in the data would be a silent 100x error, since
    the policy engine compares in paise and would not reject the value."""
    for row in rows:
        event = row["batch_event"]
        assert isinstance(event["amount"], int) and not isinstance(event["amount"], bool)
        assert event["amount"] > 0
        assert isinstance(event["discount_amount"], int)
        if event["mandate_ceiling"] is not None:
            assert isinstance(event["mandate_ceiling"], int)
            assert event["mandate_ceiling"] >= 0


def test_every_row_states_its_currency(rows):
    for row in rows:
        assert row["batch_event"]["currency"] == "INR"


def test_amounts_span_the_afa_threshold(rows):
    """Both sides of the AFA rule need ordinary rows, not only the row built to
    violate it."""
    amounts = [r["batch_event"]["amount"] for r in rows]
    assert any(a > R.AFA_REQUIRED_ABOVE_PAISE for a in amounts)
    assert any(a <= R.AFA_REQUIRED_ABOVE_PAISE for a in amounts)


def test_generator_thresholds_still_agree_with_the_policy_engine(generator):
    """The generator keeps a private copy so it can stay standalone. If the
    engine's figures move, the dataset's violation rows stop violating what
    they claim to; this is where that shows."""
    assert generator.MAX_DISCOUNT_PAISE == R.MAX_DISCOUNT_PAISE
    assert generator.AFA_REQUIRED_ABOVE_PAISE == R.AFA_REQUIRED_ABOVE_PAISE
    assert generator.MAX_RETRIES == R.MAX_RETRIES
    assert generator.PRE_DEBIT_NOTICE_HOURS == R.PRE_DEBIT_NOTICE_HOURS
    # The generator doesn't mirror this one as an exact figure -- it picks an
    # amount it only needs to sit safely above the threshold. If the engine's
    # threshold ever rose to meet or pass it, the row would stop violating
    # anything, so the relationship itself is what has to hold.
    assert generator.SIP_INSURANCE_VIOLATION_AMOUNT_PAISE > R.AFA_REQUIRED_ABOVE_SIP_INSURANCE_PAISE


# --------------------------------------------------------------------------
# Value provenance
# --------------------------------------------------------------------------
def _errors(rows) -> List[Dict[str, Any]]:
    return [
        step["error"]
        for row in rows
        for step in _all_steps(row)
        if step.get("error") is not None
    ]


def test_every_error_object_uses_only_documented_values(dataset, rows):
    provenance = dataset["value_provenance"]
    for error in _errors(rows):
        assert error["code"] in provenance["documented_error_codes"]
        assert error["source"] in provenance["documented_error_sources"]
        assert error["step"] in provenance["documented_error_steps"]
        assert error["reason"] in provenance["documented_error_reasons"]
        if error["field"] is not None:
            assert error["field"] in provenance["documented_error_fields"]


def test_error_objects_carry_the_full_documented_schema(rows):
    """The real schema, not a flattened error-code string."""
    expected = {"code", "description", "field", "source", "step", "reason", "metadata"}
    for error in _errors(rows):
        assert set(error) == expected


def test_error_sources_are_valid_gateway_enum_members(rows):
    """Cross-checks the generator's private whitelist against the enum the
    gateway will actually parse these into."""
    valid = {member.value for member in ErrorSource}
    for error in _errors(rows):
        assert error["source"] in valid


def test_seeded_webhook_events_are_real_event_names(rows):
    valid = {member.value for member in WebhookEventName}
    for row in rows:
        for step in _all_steps(row):
            if step["op"] == "simulate_webhook":
                assert step["event"] in valid


def test_unfilled_schema_gaps_are_recorded_rather_than_invented(dataset):
    """Where the reference schema had no value for a scenario, the gap is
    declared. An empty list here would mean the generator either found every
    value it needed or quietly invented one."""
    gaps = dataset["value_provenance"]["gaps"]
    assert gaps
    for gap in gaps:
        assert gap["gap"] and gap["detail"]


def test_closed_gaps_are_kept_alongside_the_open_ones(dataset):
    """Both lists are published. Deleting a gap once it is closed would lose the
    record of what was missing and what fixed it."""
    provenance = dataset["value_provenance"]
    assert provenance["resolved_gaps"]
    for gap in provenance["resolved_gaps"]:
        assert gap["gap"] and gap["resolved_by"]


def test_the_hard_decline_and_gateway_gaps_are_actually_closed_in_the_data(rows):
    """A gap marked closed has to be closed in the rows, not just in the note."""
    errors = _errors(rows)
    reasons = {e["reason"] for e in errors}
    assert "card_declined" in reasons
    assert "gateway_technical_error" in reasons
    assert any(e["source"] == "gateway" for e in errors)
    assert any(e["source"] == "bank" for e in errors)


def test_every_description_is_quoted_from_a_documented_source(generator):
    """Two documented sources, and each profile uses exactly one of them.

    `incorrect_otp` keeps the error-object example's own `description` string,
    which is the documented value for that field. The rest quote the reference
    explanation for their reason. Neither is composed here, which is what the
    original gap was about.
    """
    api_example_description = "Payment failed due to incorrect OTP"

    for name, profile in generator.FAILURE_PROFILES.items():
        assert profile["reason"] == name
        description = profile["description"]
        assert description
        if name == "incorrect_otp":
            assert description == api_example_description
        else:
            # Quoted prose from the reference explanation, not a template built
            # from the reason string.
            assert description != f"Payment failed due to {name.replace('_', ' ')}"
            assert description.endswith(".")


def test_the_malformed_source_value_is_excluded(generator, rows):
    """The reference sheet carries `psp_app_ not_available` with a stray space.
    It is a typo in that file, so it is not a usable value."""
    assert "psp_app_ not_available" not in generator.DOCUMENTED_ERROR_REASONS
    assert all(" " not in reason for reason in generator.DOCUMENTED_ERROR_REASONS)
    assert all(" " not in e["reason"] for e in _errors(rows))


def test_bucket_split_is_labelled_as_coverage_design(dataset):
    """The proportions are a coverage choice, not a measurement. The file has
    to say so, because the number travels into the pitch."""
    note = dataset["bucket_design"]["note"].lower()
    assert "not a measurement" in note


# --------------------------------------------------------------------------
# Gateway seed steps
# --------------------------------------------------------------------------
def test_every_seed_step_is_one_the_runner_supports(rows):
    for row in rows:
        for step in _all_steps(row):
            assert step["op"] in SUPPORTED_SEED_OPS


def test_seed_steps_only_touch_their_own_payment(rows):
    """A step naming another row's payment would silently corrupt that row."""
    for row in rows:
        payment_id = row["batch_event"]["payment_id"]
        for step in _all_steps(row):
            assert step["payment_id"] == payment_id


def test_every_row_creates_its_payment_first(rows):
    for row in rows:
        steps = _all_steps(row)
        assert steps[0]["op"] == "create_payment"
        assert sum(1 for s in steps if s["op"] == "create_payment") == 1


def test_chaos_configs_are_valid_and_within_documented_bounds(rows):
    """Constructing the real ChaosConfig proves both: the modes are members of
    the enum and the timings are inside the documented 0-45s window, which the
    schema enforces rather than clamps."""
    valid_modes = {member.value for member in ChaosMode}
    for row in rows:
        for step in _all_steps(row):
            chaos = step.get("chaos")
            if chaos is None:
                continue
            assert set(chaos["modes"]) <= valid_modes
            ChaosConfig(**chaos)


def test_seed_offset_outlasts_the_silence_threshold(dataset):
    """Setup runs this far before the batch instant. A dropped webhook only
    reads as silence once it has been silent longer than the resolver's
    threshold; a shorter offset would leave those rows merely in progress."""
    assert dataset["seed_offset_seconds"] > SILENCE_THRESHOLD_SECONDS


# --------------------------------------------------------------------------
# Bucket: ambiguous
# --------------------------------------------------------------------------
def test_ambiguous_bucket_covers_both_documented_behaviours(rows):
    """Webhook delay and the Failed-to-Authorized flip are the two documented
    ambiguous cases; a dropped webhook is the third named variant."""
    scenarios = {r["scenario"] for r in _bucket(rows, "ambiguous")}
    assert "flip" in scenarios
    assert "delay_failed" in scenarios
    assert "delay_authorized" in scenarios
    assert "silent_drop" in scenarios


def test_delay_cases_are_seeded_at_the_observation_instant(rows):
    """A delayed webhook seeded in the setup phase would have landed long
    before the batch reads it, resolving the ambiguity the row exists to
    present. The delay must be emitted within its own window of observation."""
    delay_rows = [
        r for r in _bucket(rows, "ambiguous") if r["scenario"].startswith("delay_")
    ]
    assert delay_rows
    for row in delay_rows:
        at_observation = row["gateway_seed"]["at_observation"]
        assert at_observation, row["row_id"]
        for step in at_observation:
            assert step["chaos"]["modes"] == [ChaosMode.DELAYED_WEBHOOK.value]
            assert step["chaos"]["delay_seconds"] > 0


def test_flip_and_drop_cases_are_seeded_before_the_observation_instant(rows):
    """Both need elapsed time to be visible: the flip's later authorization has
    to have arrived, and the drop has to have been silent long enough."""
    for scenario, mode in (
        ("flip", ChaosMode.FAILED_AUTHORIZED_FLIP),
        ("silent_drop", ChaosMode.SILENT_DROP),
    ):
        rows_for = [r for r in _bucket(rows, "ambiguous") if r["scenario"] == scenario]
        assert rows_for
        for row in rows_for:
            assert row["gateway_seed"]["at_observation"] == []
            modes = [
                m
                for step in row["gateway_seed"]["setup"]
                if step.get("chaos")
                for m in step["chaos"]["modes"]
            ]
            assert mode.value in modes


# --------------------------------------------------------------------------
# Bucket: policy violations
# --------------------------------------------------------------------------
def test_violation_bucket_covers_every_reachable_rule(rows):
    covered = {
        r["expected_policy_rule"]
        for r in _bucket(rows, "policy_violation")
        if r["expected_policy_rule"]
    }
    assert covered == EXPECTED_VIOLATION_RULES


def test_the_two_uncovered_rules_are_the_ones_covered_elsewhere(rows):
    """Named explicitly so an added rule fails this test rather than being
    quietly absent from the dataset."""
    all_rules = {member.value for member in R.RuleId}
    assert all_rules == EXPECTED_VIOLATION_RULES | RULES_COVERED_ELSEWHERE


def test_each_violation_row_trips_exactly_one_rule(rows):
    """The engine returns on its first violation, so a row breaking two rules
    would only ever prove the earlier one. Each row is checked against the
    engine's own predicates, in the engine's evaluation order."""
    reference = datetime.fromisoformat(
        json.loads(DATASET_PATH.read_text(encoding="utf-8"))["batch_reference_time"]
    )
    for row in _bucket(rows, "policy_violation"):
        event = BatchEvent(**row["batch_event"])
        fired = []

        missing = R.check_required_rbi_fields(
            event.pre_debit_notice_sent_at, event.mandate_ceiling, event.afa_flag
        )
        if missing:
            fired.append(missing.rule_id.value)
        else:
            for violation in (
                R.check_pre_debit_notice(event.pre_debit_notice_sent_at, reference),
                R.check_mandate_ceiling(event.amount, event.mandate_ceiling),
                R.check_afa(event.amount, event.afa_flag, event.mandate_category),
                R.check_afa_sip_insurance(
                    event.amount, event.afa_flag, event.mandate_category
                ),
                R.check_max_discount(event.discount_amount),
                R.check_max_retries(event.retry_count),
            ):
                if violation:
                    fired.append(violation.rule_id.value)

        assert fired == [row["expected_policy_rule"]], row["row_id"]


def test_non_violation_rows_break_no_rule(rows):
    """Standard-failure rows must be blocked by nothing, or the recovery
    figures would be measuring the dataset rather than the pipeline."""
    reference = datetime.fromisoformat(
        json.loads(DATASET_PATH.read_text(encoding="utf-8"))["batch_reference_time"]
    )
    for row in _bucket(rows, "standard_failure"):
        event = BatchEvent(**row["batch_event"])
        assert (
            R.check_required_rbi_fields(
                event.pre_debit_notice_sent_at, event.mandate_ceiling, event.afa_flag
            )
            is None
        )
        assert R.check_pre_debit_notice(event.pre_debit_notice_sent_at, reference) is None
        assert R.check_mandate_ceiling(event.amount, event.mandate_ceiling) is None
        assert R.check_afa(event.amount, event.afa_flag, event.mandate_category) is None
        assert (
            R.check_afa_sip_insurance(
                event.amount, event.afa_flag, event.mandate_category
            )
            is None
        )
        assert R.check_max_discount(event.discount_amount) is None
        assert R.check_max_retries(event.retry_count) is None
        assert event.opted_out is False


def test_pre_debit_notice_timestamps_sit_where_intended(rows, dataset):
    """The notice is compared against the batch instant, so its distance from
    that instant is the whole content of the field."""
    reference = datetime.fromisoformat(dataset["batch_reference_time"])
    required = timedelta(hours=R.PRE_DEBIT_NOTICE_HOURS)
    for row in rows:
        raw = row["batch_event"]["pre_debit_notice_sent_at"]
        if raw is None:
            assert row["expected_policy_rule"] == "MISSING_RBI_FIELD_PRE_DEBIT_NOTICE_SENT_AT"
            continue
        elapsed = reference - datetime.fromisoformat(raw)
        if row["expected_policy_rule"] == "PRE_DEBIT_NOTICE_TOO_RECENT":
            assert timedelta(0) <= elapsed < required
        else:
            assert elapsed >= required


# --------------------------------------------------------------------------
# Bucket: adversarial
# --------------------------------------------------------------------------
def test_injection_notes_are_flagged_by_the_real_sanitizer(rows):
    """Asserted against the shipping sanitizer rather than a copy of its
    patterns, so a note that stops matching is caught here."""
    injection_rows = [
        r for r in _bucket(rows, "adversarial") if "injection" in r["scenario"]
    ]
    assert injection_rows
    for row in injection_rows:
        _, report = sanitize_customer_note(row["batch_event"]["customer_note"])
        assert report.looks_like_instruction, row["row_id"]
        assert report.injection_patterns_flagged


def test_injection_text_sits_in_the_free_text_note_not_a_structured_field(rows):
    """Realistic placement matters: an injection arrives in customer prose, not
    in a field the gateway parses."""
    for row in _bucket(rows, "adversarial"):
        _, report = sanitize_customer_note(row["batch_event"]["customer_note"])
        if not report.looks_like_instruction:
            continue
        for step in _all_steps(row):
            if step.get("error"):
                assert not sanitize_customer_note(step["error"]["description"])[
                    1
                ].looks_like_instruction
                assert step["error"]["reason"].isidentifier()


def test_adversarial_bucket_contains_opted_out_customers(rows):
    opted_out = [r for r in _bucket(rows, "adversarial") if r["batch_event"]["opted_out"]]
    assert opted_out


def test_opted_out_rows_span_varied_underlying_failures(rows):
    """The opt-out rule's outcome depends on which action gets proposed, and
    that depends on the underlying failure. One shape would only ever exercise
    one row of the opt-out decision table."""
    opted_out = [r for r in _bucket(rows, "adversarial") if r["batch_event"]["opted_out"]]
    assert len({r["scenario"] for r in opted_out}) >= 3
    reasons = {
        step["error"]["reason"]
        for row in opted_out
        for step in _all_steps(row)
        if step.get("error")
    }
    assert reasons == {"incorrect_otp", "insufficient_funds"}


def test_opt_out_is_confined_to_the_adversarial_bucket(rows):
    """Opt-out rows elsewhere would block actions the other buckets are
    measuring, so their outcomes would no longer mean what they claim."""
    for row in rows:
        if row["batch_event"]["opted_out"]:
            assert row["bucket"] == "adversarial", row["row_id"]


# --------------------------------------------------------------------------
# Generator behaviour
# --------------------------------------------------------------------------
def test_generation_is_reproducible_for_a_given_seed(generator):
    assert generator.generate(count=200, seed=7) == generator.generate(count=200, seed=7)


def test_a_different_seed_produces_a_different_dataset(generator):
    assert generator.generate(count=200, seed=7) != generator.generate(count=200, seed=8)


def test_generator_rejects_a_count_outside_the_required_range(generator):
    for count in (199, 501):
        with pytest.raises(ValueError):
            generator.generate(count=count)


def test_generator_output_matches_the_committed_file(generator, dataset):
    """The committed dataset is what the batch run and the dashboard read. If
    it drifts from what the generator now produces, one of them is stale."""
    regenerated = generator.generate(count=dataset["count"], seed=dataset["seed"])
    assert regenerated == dataset


def test_generator_does_not_import_the_backend(generator):
    """It must stay runnable on its own: a standalone script that only writes a
    JSON file, with no path into the application."""
    source = GENERATOR_PATH.read_text(encoding="utf-8")
    for forbidden in ("from app.", "import app", "fastapi", "requests", "httpx"):
        assert forbidden not in source, f"generator must not reference {forbidden}"


def test_generator_never_sleeps(generator):
    """Time is modelled as timestamps against an injectable clock everywhere in
    this system; a sleep here would make generation slow for no gain."""
    source = GENERATOR_PATH.read_text(encoding="utf-8")
    assert "sleep" not in source
