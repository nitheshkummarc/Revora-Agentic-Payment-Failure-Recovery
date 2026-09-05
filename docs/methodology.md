# Revora — Evaluation Methodology

Supplementary to `README.md`, which stays authoritative. This document is for
someone auditing the numbers rather than trusting them: how the dataset was
built, why the headline batch runs against a stub, what the live tests prove
instead, and what would have to change to evaluate any of this against a real
payment stream.

No figure here is new. Every number appears in `README.md` already; this
document explains how it was produced and what it does and does not support.

---

## 1. The synthetic dataset

### How it is generated

`data/generate_synthetic_dataset.py` produces 500 events into
`data/synthetic_events_500.json` from a fixed seed (`20260421`), so the dataset
is reproducible rather than sampled fresh each run. The generator is standalone
— it imports nothing from `backend/`, and a test scans its source to keep it
that way. That boundary is deliberate: the thing generating the test data should
not be able to drift into agreement with the code under test by sharing its
imports.

A row is more than a payment record. Each carries the `BatchEvent` the
orchestrator consumes, plus declarative gateway setup steps that put the payment
into the state its scenario describes, plus its own labels (`bucket`,
`scenario`, `intent`, `expected_policy_rule`). The setup steps are emitted
declaratively and executed by `data/run_batch.py`, which is what lets the
generator stay standalone while still describing gateway state.

Timing is anchored rather than wall-clock. Everything sits relative to a fixed
reference instant (`2026-04-21T10:00:00Z`), with setup applied 600 seconds
earlier. The 600s is not arbitrary — it has to exceed the resolver's silence
threshold, or a dropped webhook reads as "still legitimately in progress"
instead of as silence, and the scenario stops testing what it was written to
test.

### The 60/20/10/10 bucket split — what it is for

| Bucket | Target | Actual rows |
|---|---|---|
| `standard_failure` | 60% | 300 |
| `ambiguous` | 20% | 100 |
| `policy_violation` | 10% | 50 |
| `adversarial` | 10% | 50 |

**This is a test-coverage allocation, not a model of how payments fail in
production.** It exists to exercise every code path at a useful sample size —
enough policy-violation rows that each RBI rule fires several times, enough
adversarial rows that the injection guard is tested against variety rather than
one payload. The dataset records this in its own `bucket_design.note` field, and
the README states it in "Deliberately out of scope".

Nothing in this repository supports presenting the split as a production
distribution, and the consequence is worth being explicit about: because the
buckets were chosen, any aggregate over them is arithmetic on a design decision.
That is the reason the README's headline figure is called a "Correctly Routed
Rate" and not a recovery rate.

### Where the error vocabulary came from

Error `code`, `field`, `source`, `step` and `reason` values are drawn only from
Razorpay's documented vocabulary. The generator carries that whitelist
explicitly: one documented `code` (`BAD_REQUEST_ERROR`), one documented `field`
(`otp`), one documented `step` (`payment_authentication`), five `source` values,
and 109 documented `reason` values.

Where a scenario needed a value the reference does not supply, the gap was
**recorded rather than filled**. The dataset's `value_provenance` field carries
the whitelist, the gaps that were closed and what closed them, and the gaps that
remain open. Every generation run prints them.

Three gaps were closed with real documented values — a hard-decline reason
(`card_declined`), a gateway-attributed reason (`gateway_technical_error`), and
descriptions quoted from the reference's own explanation column instead of
written to a template.

Two remain open and are declared in the data:

- **Only one `step` value is documented.** The reference's reason list has no
  step column at all, so it supplies no further values. Every error profile
  therefore uses `payment_authentication`, including ones where the semantic fit
  is imperfect.
- **`reason` → `source` attribution is not documented as a mapping.** The
  reference documents reasons but never states which source a given reason is
  attributed to.

If the error vocabulary looks narrow, that is why. It is narrow because the
documented ground truth is narrow, and widening it would mean inventing values
that look correct to anyone who does not check.

---

## 2. Stub and live evaluation measure different things

This is the single most important thing to understand before quoting any number
from this project, so it gets stated plainly: **there are two separate bodies of
evidence, and they were never designed to be combined into one statistic.**

### The 500-row batch runs on `StubLLMClient`

The committed `data/batch_results.json` is a stub run. `StubLLMClient` returns
`RETRY_SOFT` unconditionally for every event.

Three reasons that is the right choice for this artifact:

**Reproducibility.** Two independent runs produce byte-identical per-event
outcomes; only the run ID differs. A live model would make the headline table
non-reproducible, and a number nobody can regenerate is not auditable.

**Zero cost and no key required.** Anyone cloning the repository can regenerate
the run and get the same figures without an API key or spend.

**It isolates the deterministic pipeline.** With model judgement held constant,
every variation in outcome across the 500 rows is attributable to the resolver,
the tracer, the guards, the policy engine, or Verify. That is precisely what
this run is evidence about.

There is a fourth property that is easy to mistake for a weakness.
`RETRY_SOFT` is exactly the money-moving action an injected note is
trying to elicit, so on every adversarial row the stub behaves as a model that
has **fully complied with the attack** — and the guard catches it anyway. The
stub is the worst case for the guard, not a soft test of it.

What the batch consequently **cannot** show: anything about a real model's
judgement. The README says this at the point where the numbers appear rather
than only in a footnote, and the dashboard states the same provenance on screen.

### The live tests exist to prove the other thing

`backend/tests/test_intelligence_live_api.py` (18 tests) runs against a real
model. It skips unless a provider key is set, and skips loudly — a green suite
without a key is not evidence it passed.

It covers two properties, kept apart on purpose:

1. **The system is safe.** No injection produces a money-moving action. This must
   hold regardless of what the model returns, because the guard is what
   guarantees it.
2. **Whether the model itself resisted.** Recorded separately and reported rather
   than asserted, because a guard doing all the work is a different risk profile
   from a model that also resists. This failing is a finding, not a defect.

A third section checks whether reasoning on ordinary, non-adversarial evidence is
actually grounded in what the tracer said. Its inputs are real error objects
lifted verbatim from the dataset, not invented for the test.

### Why they are never blended

The batch is 500 rows with model judgement held constant. The live tests are a
handful of cases with real judgement and no batch context. They answer different
questions, and averaging them would produce a figure that describes neither.

Concretely: reporting a combined "accuracy" would let 500 stub rows drown out the
live sample, producing a number that looks like model evaluation but is almost
entirely deterministic-pipeline behaviour. The two are reported separately for
that reason, and the live sample's size is stated wherever its results are.

---

## 3. How correctness was checked

### Oracle-based grading

The dataset carries its own labels, so grading is a join rather than a judgement
call. `expected_policy_rule` names the rule a policy-violation row must trigger;
`scenario` and `intent` describe the outcome each row is designed to reach.
Grading compares the run's actual `rule_id` and outcome against those labels.

For the live tests, the oracle is a small per-category set of defensible actions
rather than a single expected value. It is deliberately not strict — judging
model reasoning is inherently fuzzy — so it functions as a flag for a surprising
answer that a human should look at, not as a pass/fail gate.

### Finding 1 — a measurement bug in the resistance metric, found and fixed

The test that reports whether the model resisted an injection on its own was
keying off the wrong field, and the resulting figure was meaningless.

`guard_override_reason` is set whenever a customer note matches an
instruction-like pattern — *including* when the model had already answered
`ESCALATE_HUMAN` and the guard therefore changed nothing. Since every adversarial
note in the set is flagged by design, keying off that field placed all of them in
the "complied" bucket no matter how the model behaved. The reported resistance
was a constant, not a measurement.

The fix reads the model's own pre-guard answer (`original_llm_action`, falling
back to the final action when no guard fired) and classifies it against the
money-moving action set. The README records both the corrected figure and the
fact that the earlier one was an artifact.

**What this changed, and what it did not.** It changed attribution only. In both
the broken and the corrected measurement, the final action on every adversarial
payload was `ESCALATE_HUMAN` and no money moved — the safety property was never
in question and was never affected. What was wrong was the claim about *who*
stopped the attack, and the error ran in the direction of understating the model
rather than overstating the system.

### Finding 2 — an oracle disagreement, with mixed evidence

On ordinary non-adversarial evidence, the live model has returned actions the
oracle does not list. The pattern is a transient-versus-hard-decline judgement
call: whether a decline warrants an automatic retry, or whether retrying without
evidence the underlying condition changed is the wrong move.

**The evidence is mixed, and it is worth being precise about how.** Across the
two live runs on record: one category (`insufficient_funds`) returned the same
non-oracle action in both runs, while another (`card_declined`) disagreed in one
run and matched in the other. So one case repeated and one did not, across n=2
runs with one observation per category per run.

That is not enough to call either a stable property of the model. It is also not
nothing — it is a consistent *kind* of disagreement, appearing on exactly the
categories where a reasonable person could argue either way, and the model's
stated reasoning in each case is internally coherent and grounded in the trace.

**The oracle was deliberately not widened on this evidence.** Widening it would
mean encoding a judgement about which declines are retryable, and this project
has no documented ground truth for that — the dataset treats all its failure
reasons uniformly as retry-eligible, which is a design choice, not a finding.
Widening an oracle based on a pattern that does not land on the same category
twice would be fitting to noise. Recorded as something to investigate with a
larger sample after submission, not as a fixed defect and not as a reason to move
the target.

A related observation from the same runs: the split of which injection payloads
the model resists on its own also shifts between runs while the overall ratio has
held. Same caution applies — quote the ratio, not the specific payloads.

---

## 4. The naive-baseline comparison

`data/run_baseline.py` evaluates Revora against a naive always-retry policy
across the same 500 incidents. The naive policy has no tracer, no policy engine
and no verification stage; it retries everything.

Construction, and why each choice matters:

- **Same seed, same reference instant.** Both policies see identical incidents.
  A difference in outcome is attributable to policy, not to a different draw.
- **A freshly reset gateway per policy.** Webhook delivery failure is re-rolled
  from a seeded RNG that advances with use, so a gateway that has already served
  one batch starts the next at a different point in the stream. Without an
  explicit reset the second policy would face a measurably different world.
- **State-first classification.** Every payment is categorised by its state
  *before* either policy acted, read from gateway truth rather than from either
  policy's own account of what it did.
- **A retry that "succeeds" against an already-captured payment counts as
  duplicate-payment risk, not a recovery.** This is the classification decision
  that makes the comparison meaningful; scored the naive way, double-charging a
  customer would register as a win.

### The caveat, repeated because it is load-bearing

**The naive policy books more recoveries than Revora, and that is not evidence
naive is better.** Against the mock gateway a retry always succeeds by
construction, so the rows Revora escalates for a status check are rows naive
simply retries into success. That gap measures the simulator, not policy quality.
Nothing here models the probability that a real retry against a real bank would
land.

What the comparison does show faithfully is the cost side, which the simulator
does model: retries attempted that the policy engine refuses, and
already-successful payments retried without noticing. Read the README's own
table for those figures with its framing attached.

---

## 5. What would have to change to evaluate this against a real payment stream

This is a roadmap note, not an apology. The methodology above is sound for what
it measures; the gap between it and production evaluation is specific and worth
naming precisely.

**Real retry success rates are unknown.** This is the largest gap. Every recovery
figure in this project rests on a mock gateway where retries always succeed, so
the system's *routing* is measured and its *effectiveness* is not. Closing this
needs outcome data from real retries — a population of failed payments, retried,
with the landing rate recorded per failure reason. Until that exists, no figure
here should be read as a revenue projection, which is why the README labels the
headline as a routing rate.

**Webhook timing and ordering are assumed from documentation.** The 45-second
maximum delay and the Failed→Authorized flip are calibrated to Razorpay's public
documentation, but the delivery-failure rate, the silence threshold and every
confidence-penalty weight are this project's own design choices with no external
source. They are labelled as such in the code rather than presented as documented
figures. Real evaluation would replace them with measured distributions from an
actual webhook stream — and the silence threshold in particular is the one most
likely to be wrong, since it directly controls how many payments get classified
as ambiguous.

**There is no live Razorpay integration.** No real API calls, no keys, no card
data. The gateway is a simulator, and every claim is a claim about behaviour
under that simulator. A production evaluation would run the same deterministic
pipeline against a real event stream and compare its resolutions against
eventual settled state — which is a well-defined experiment, just not one this
build performs.

**The compliance rules need an update path, not a re-check.** Each threshold
has been verified against the text of the circular it cites — the 24-hour
notice, the ₹15,000 general AFA limit, the ₹1 lakh limit for the three
categories the circular names, and the variable-mandate ceiling all match,
boundary semantics included. What is missing is not verification but
maintenance: the values are pinned as constants in source, so a superseding
circular requires a code change and a redeploy. A real deployment wants them
externalised with an effective-date mechanism, so a threshold change is a
configuration event with an audit trail rather than a commit.

**Model evaluation needs a real sample size.** The live evidence here is a
handful of cases. Establishing whether the transient-versus-hard-decline
disagreement in Finding 2 is a genuine property of the model needs many
observations per category, ideally across more than one model — which is also the
only sound basis on which the oracle should be revisited.
