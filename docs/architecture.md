# Revora — Architecture

Supplementary to `README.md`, which stays authoritative. This document goes a
level deeper for a reviewer deciding whether to read the source: what each
module owns, how the two structural boundaries are actually enforced, and what
happens to a single event as it moves through the pipeline.

Nothing here introduces a figure the README does not already state. Where
something is untested or assumed, it says so in the same terms the README's
"What's proven and what isn't" section uses.

---

## 1. The six backend modules

Each module owns one question. The list below states what it owns, what it
deliberately does *not* own, and which direction its dependencies run.

### `backend/app/gateway/` — the simulated world

Owns the mock payment gateway and its fault injection: payment and subscription
lifecycle, webhook emission, delivery with retry and a circuit breaker, and the
chaos modes that make webhooks arrive late, twice, out of order, or never. It is
the only module that holds authoritative payment state.

It deliberately does **not** own interpretation. It exposes two separate fields
— `state` (gateway truth) and `webhook_derived_state` (what a merchant would
conclude from the webhooks that actually arrived) — and keeping them apart is
the point of the module. Downstream code never reads `state`; a status query is
the only sanctioned route to gateway truth, which is what makes "the evidence
disagrees with reality" a scenario the rest of the system can be tested against.

Imports only `app.core`. Nothing in the deterministic chain below imports the
gateway's *behaviour* — the state machine and tracer import only its schemas
(`WebhookEvent`, `ErrorObject`), never `MockPaymentGateway`.

### `backend/app/state_machine/` — what state is this payment actually in?

Owns resolution: given the webhook events that were delivered, plus when the
payment was created and when it is being observed, decide the canonical state
and say how confident that is and which rule produced it. Handles out-of-order
delivery, duplicates, the documented Failed→Authorized flip, and silence past a
threshold.

It does **not** own diagnosis — it says *what* state, never *why*. It also does
not see gateway truth: its input schema `PaymentObservation` has exactly four
fields (`payment_id`, `created_at`, `events`, `observed_at`) and forbids extras,
so `state` cannot be passed in even by accident.

Notably it does not consume `webhook_derived_state` either, though that field
folds the same evidence. That fold is too lossy to resolve from: it reads
`CREATED` for both a five-second-old and a ten-minute-old silent drop, so the
silence threshold is undecidable from it, and it has already collapsed the
Failed→Authorized flip, so the flip is undetectable from it. Resolution works
from the raw delivered event list instead.

Imports `app.gateway.schemas` and `app.core`. Fully deterministic; a test scans
its own source for forbidden imports.

### `backend/app/tracer/` — why did it fail?

Owns causal diagnosis: root cause quoted from the event's own error object, the
causal chain of event IDs that supports it, a confidence score, and an explicit
ambiguity verdict with reasons.

It owns one more thing worth naming: **the refusal to answer.** It traces only
payments resolved to `FAILED` or `PENDING_WEBHOOK`; anything else raises
`NotTraceableError` rather than inventing a cause for a payment that did not
fail. A chain with no error object behind it is refused the same way. Confidence
is multiplicative rather than a weighted sum, so an ungrounded trace cannot
score well on chain completeness alone.

Ambiguity has two independent triggers, and they matter separately: confidence
falling below the threshold, and a structural gap in the event chain. The
structural rule fires on its own — a chain missing an interior sequence is
flagged even when its confidence sits above the threshold.

Imports `app.gateway.schemas` and `app.state_machine`. Deterministic, with the
same source-scanning test.

### `backend/app/intelligence/` — what should we do about it?

The only module where a model has a say. Owns prompt construction, sanitisation
of untrusted customer text, the model client (Groq primary, Gemini fallback,
offline stub), and the two deterministic guards that run on the model's output.

It does **not** own execution, and has no gateway import at all. It also does
not own the decision to consult the model: an ambiguous trace short-circuits to
`REQUEST_VERIFICATION` before any call is made, so the model is never asked to
guess in place of missing evidence.

The guards run *after* the model and are not model-dependent. The injection
guard forces `ESCALATE_HUMAN` when the customer note matched an instruction-like
pattern, regardless of what came back. The grounding guard blocks a money-moving
recommendation that has no grounded error object behind it — a backstop that is
structurally unreachable on the normal pipeline, because the tracer already
marks such a trace ambiguous and the short-circuit catches it first. It is
retained because that guarantee lives in a different module, and it is labelled
as a backstop rather than presented as an active rule.

Imports `app.tracer.schemas` (via its own schemas) and `app.core`. Nothing
imports execution capability *into* it.

### `backend/app/policy/` — are we allowed to?

Owns RBI compliance enforcement. The recommendation arrives as a proposal and
this module approves or blocks it, recording every rule evaluated and which one
decided, with the circular reference attached where one applies.

That qualifier is load-bearing. A citation is attached per rule, not
blanket-applied to the module: rules grounded in the circular carry it, and
rules that are business or vendor limits — the discount cap, the retry ceiling
— deliberately do not. The opt-out rule sits across the line and is split
accordingly: the circular obliges an issuer to honour an opt-out, so the block
cites it, but the circular sets no duration, so the permanent cooldown is
named in the block text as Revora's own choice. Nothing here claims a section
number, since none were verified against the source text.

Precedence runs in three stages, and the ordering is deliberate: the customer
opt-out gate first, then the missing-required-field gates, then the value
thresholds. Absence is checked before values because a missing compliance field
must fail closed rather than being treated as a passing value.

This next part is easy to describe wrongly. The first two stages are genuine
early returns, but the value rules are not. All of them
evaluate and are recorded, and only the earliest failure decides the outcome.
That is harmless while every check is a pure predicate, and it matters the
moment one does work rather than compare.

All money comparisons happen in paise. The reference states rupee figures and
the dataset carries paise; mixing them would be a 100× error, so the conversion
happens once at the point of use and keeps its rupee source in a comment.

Imports `RecommendedAction` from `app.intelligence.schemas` — a type-only
dependency, reusing an existing edge rather than adding a new one. It imports no
gateway code. Deterministic, source-scanned.

### `backend/app/orchestrator/` — run the loop, then check the work

Owns the sequential pipeline — Observe, Trace, Plan, Validate, Execute, Verify —
and the batch run that drives 500 rows through it. It is the only module that
holds all the others together, and correspondingly the only one that imports
from all of them. Nothing imports from it.

Verify is the part that earns its place. After execution it re-reads gateway
truth and compares observed against expected state. A mismatch produces
`NEEDS_REVIEW` and is never recorded as a recovery. The pipeline logic lives in
`orchestrator.py` and the API contract in `schemas.py`, kept apart so the shape
served to the dashboard does not drift with the loop that fills it in.

---

## 2. The two structural boundaries, and what actually enforces them

The README states these as design properties. The mechanism is worth spelling
out, because "enforced structurally" and "enforced by convention" look identical
in a diagram.

Both rest on the same base class. Every schema in the project inherits from a
model configured with `extra="forbid"`, which turns an unexpected field from
something silently ignored at runtime into a `ValidationError` at construction.
That single setting is what converts the two boundaries below from documentation
into something a caller cannot route around.

### Boundary 1 — the model only ever sees the tracer's output

Enforced by the shape of `IntelligenceInput`, which has exactly four fields:
`payment_id`, `trace`, `customer_note`, `decided_at`.

There is no field that can carry a raw gateway event, and because extras are
forbidden, one cannot be added by a caller at the call site. Passing raw webhook
logs into the recommendation layer is not "discouraged" — the object required to
make the call cannot be constructed with them present. `trace` is a
`TraceResult`, which is itself the tracer's own validated output, so the only
route to the model runs through diagnosis first.

`customer_note` is the one field carrying untrusted text, and it is deliberately
accepted raw rather than pre-sanitised. The layer sanitises internally, so a
caller cannot bypass the sanitiser by formatting the note themselves before
passing it in.

### Boundary 2 — the model's output only reaches the gateway through the policy engine

Enforced by absence rather than by a check: the recommendation layer imports no
gateway code at all. There is no function it could call to move money, so there
is no code path by which its output executes directly. A test scans the module's
own source for gateway references and fails if any appear, so the boundary
cannot erode quietly through a later import.

The complementary constraint sits on the output side. `LLMRecommendation` has
three fields — `recommended_action`, `confidence`, `reasoning` — and the action
is an enum over a closed set of four values. A model that returns an invented
action does not produce a recommendation the system then rejects downstream; it
fails schema validation and never becomes a `LLMRecommendation` at all.

---

## 3. One event, traced end to end

What a single row actually becomes at each stage.

```
BatchEvent (payment_id, amount, currency, customer_note, RBI compliance fields)
   │
   │  Observe: query the gateway for delivered webhook events only
   ▼
PaymentObservation { payment_id, created_at, events[], observed_at }
   │  4 fields, extras forbidden — gateway truth structurally cannot enter here
   ▼
StateResolution { state, confidence, resolution_rule, resolution_log }
   │
   │  Trace: refused unless state is FAILED or PENDING_WEBHOOK
   ▼
TraceResult { root_cause, causal_chain[], confidence, ambiguous,
              ambiguity_reasons[], grounded_error, resolved_state, ... }
   │
   │  Plan: ambiguous ──► REQUEST_VERIFICATION, no model call made
   ▼
IntelligenceInput { payment_id, trace, customer_note, decided_at }
   │  the only object the model layer accepts
   ▼
[ sanitise note ] ──► [ model call ] ──► LLMRecommendation
   │                                     { recommended_action: enum(4),
   │                                       confidence: float 0..1,
   │                                       reasoning: str }
   │  injection guard, then grounding guard — both deterministic
   ▼
IntelligenceDecision { recommended_action, original_llm_action,
                       guard_override_reason, sanitization, model, ... }
   │
   │  Validate: opt-out ──► missing fields ──► value thresholds
   ▼
PolicyDecision { approved, rule_id, blocked_reason, rules_evaluated[] }
   │
   │  Execute (only if approved), then Verify against gateway truth
   ▼
EventTrace  outcome ∈ { recovered, blocked, escalated, needs_review, no_action }
```

Two things about this path are worth reading closely.

**The model sees a diagnosis, not a log.** By the time anything reaches the
model it is a `TraceResult` — a root cause quoted from a real error object, a
chain of event IDs, a confidence score — plus a delimited block of untrusted
customer text. Raw webhook payloads never appear in a prompt, because the object
that carries the request has nowhere to put them.

**Validation failure is not an error path, it is the safe path.** If the
model returns something that does not satisfy `LLMRecommendation` — a malformed
response, an invented action, a garbled token in the JSON — parsing raises, and
the layer treats it exactly as it treats a network failure: escalate rather than
guess. The README records this happening live rather than only in tests: a real
Groq response once failed its own strict-JSON validation and the system failed
closed to `ESCALATE_HUMAN`.

---

## 4. Why the deterministic/model split sits where it does

The split is not "use AI where it's impressive." It follows from which questions
have a provable answer.

**What state is this payment in** and **why did it fail** are both answerable
from evidence. The delivered events either support a conclusion or they do not,
and where they do not, the honest output is an explicit ambiguity verdict rather
than a guess. A model asked these questions would produce a plausible answer in
exactly the cases where no answer is warranted — which is the failure mode that
matters most, because it is invisible. Making these deterministic means the
system can say "the evidence has a hole in it, sequence 2 never arrived" instead
of quietly filling the hole.

**What should we do about it** is genuinely a judgment call. Whether a bank
decline warrants a retry, a status check, or a human depends on context that no
lookup table in this repo can enumerate, and the honest position is that this
project does not have documented ground truth for it. That is the one place a
model earns its seat.

**Are we allowed to** goes back to deterministic, and this is where the RBI
angle from the README's opening bites. The two failure modes it describes are
different in kind: retrying an already-succeeded payment is a *correctness*
failure the tracer and Verify catch, while retrying a payment whose mandate
forbids it is a *compliance* failure, and compliance rules are written down.
A missing pre-debit notice or an amount over a mandate ceiling is a threshold
comparison against a published circular — there is nothing to be clever about,
and a probabilistic answer to a regulatory question is strictly worse than a
deterministic one. So the model's answer arrives as a proposal, and the last
word belongs to code that cannot be argued with by a customer note.

---

## 5. Failure and fallback behaviour

The consistent rule is **fail closed**: when a stage cannot produce a trustworthy
answer, the event is routed to a human or held, never pushed forward on a
degraded result.

**The model client errors, times out, or is disabled.** The layer returns
`ESCALATE_HUMAN` at confidence 0.0 with a short-circuit reason recording why.
This covers provider errors, timeouts, schema-validation failures on the
response, a missing client, and the `REVORA_DISABLE_LLM` kill switch — all the
same path. Raw exception text is logged server-side only; the reasoning field
that reaches the dashboard and the audit trail gets a fixed generic code, so
provider internals never leak into a merchant-visible record.

With both provider keys set, one extra chance is added first: `FallbackLLMClient`
tries the primary and falls back to the secondary on any failure. It adds exactly
one attempt, not a retry loop — a failure from the fallback propagates normally
into the fail-safe above. The `model` field reports whichever backend actually
answered, so the audit trail does not misattribute a fallback answer to the
primary.

**The trace is ambiguous.** No model call is made at all. The event
short-circuits to `REQUEST_VERIFICATION`, which queries payment status rather
than acting on incomplete evidence. This is the largest non-model path in the
batch by volume.

**No policy rule matches.** The action is approved. This is the intended default
and it is safe because of what precedes it: the opt-out and missing-field gates
have already run and fail closed on absence, so reaching the value rules means
the required compliance facts were present. Approval means "no rule objected",
and every rule evaluated is recorded on the decision either way — an audit shows
what was checked, not only what fired.

**Verification detects a mismatch.** The outcome is `NEEDS_REVIEW` and the event
is never recorded as recovered. This is the case the README's `verify_mismatch_stale_success`
scenario exists to exercise: evidence reads `FAILED` at full confidence while the
gateway has already captured the payment, the retry cannot land, and Verify holds
the row for a human rather than booking a recovery it could not confirm.

**Webhook delivery keeps failing.** Each webhook gets a bounded number of
attempts with backoff; after enough consecutive failures a circuit breaker opens
and delivery stops. Affected payments then read as silence, resolve to
`PENDING_WEBHOOK`, and escalate. The breaker closes itself once a cooldown has
elapsed on the injected clock.

### Where the evidence for this lives

The live-verified evidence for the fail-closed behaviour is in the README's
"What's proven and what isn't" section, and it should be read there with its own
caveats attached rather than re-stated here as if independently confirmed. In
particular: the injection guard's live results carry a specific caveat about
which payloads the model resists varying between runs, and the batch-level
figures come from a run using the offline stub, which measures the deterministic
pipeline and nothing about model judgement. `docs/methodology.md` explains why
those two bodies of evidence are kept separate.
