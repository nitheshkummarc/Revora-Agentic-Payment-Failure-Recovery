# Revora

> Revora separates "what happened" and "why" (both deterministic, both provable
> from evidence) from "what should we do" (the one place a model gets a say) —
> and the model's answer is a recommendation a deterministic policy engine can
> reject, never a command that reaches a payment directly.

## The idea

A failed payment rarely explains itself. Webhooks arrive late, out of order,
twice, or never; a payment marked `Failed` can flip to `Authorized` minutes
later when a delayed bank response lands. A merchant's own dashboard is
reading the same incomplete evidence a recovery bot would.

Concretely: a payment fails, a retry bot sees `Failed` and retries it. Ten
minutes later the bank's original authorization finally arrives — the payment
had actually gone through the first time. The bot has now captured the
customer twice, and nothing in "just retry failed payments" would have caught
it, because the bot was reading state that was already stale by the time it
acted. A second failure mode sits right next to the first: a payment that
*is* still failed, but retrying it would violate an RBI mandate rule (a
missing pre-debit notice, an amount over the customer's variable-mandate
ceiling) — acting fast and acting compliant are not the same requirement, and
a system tuned only for the first will eventually fail the second.

Revora separates the questions that usually get collapsed into one **"should
I retry this?"** call:

| Question | Answered by | Deterministic? |
|---|---|---|
| What state is this payment actually in? | State resolver | Yes |
| Why did it fail? | Failure propagation tracer | Yes |
| What should we do about it? | Recommendation layer | No — model |
| Are we allowed to? | Policy engine | Yes |

Only the third question involves a model, and its answer is a recommendation
that the policy engine can reject.

## Architecture

```
Mock payment gateway (fault injection)
        │  delivered webhook events only — never gateway truth
        ▼
State resolver ──────────────► what state is this in?          [deterministic]
        │  full resolution, including what it could not place
        ▼
Failure propagation tracer ──► why did it fail?                 [deterministic]
        │  root cause, causal chain, confidence, ambiguity
        ▼
Recommendation layer (model) ► what should we do?  ◄── AI STARTS HERE
        │  recommendation only, never a command
        ▼
Injection guard + grounding guard (deterministic, run on the model's output)
        ▼                                          ◄── AI STOPS HERE
Policy engine ───────────────► are we allowed to?                [deterministic]
        │  approved / blocked with the rule that fired
        ▼
Orchestrator ────────────────► execute, then verify              [deterministic]
        ▼
Dashboard
```

Two boundaries are enforced structurally, not by convention — meaning a
Pydantic schema makes the violation impossible to construct, not just
discouraged in a docstring:

- **The model only ever sees the tracer's output.** Its input schema
  (`IntelligenceInput`, `extra="forbid"`) has no field that can carry a raw
  gateway event, so raw logs cannot leak into a prompt even by accident.
- **The model's output only reaches the gateway through the policy engine.**
  The recommendation layer has no gateway import at all — there is no code
  path by which its output could execute directly.

## What makes this honest

**Proven by tests, not just claimed:**
- Root causes are quoted from the event's own error object, never invented —
  a test constructs a chain with no error object and asserts the tracer
  refuses to trace it (`NotTraceableError`) rather than fabricate a cause.
- An ambiguous trace never reaches the model — proven with
  `ExplodingLLMClient`, a test double that raises if called at all, not just
  by checking a boolean flag the code sets itself.
- A malformed or schema-invalid model response cannot become a money-moving
  action. This was also observed live, not just asserted: a real Groq
  response once failed its own strict-JSON validation
  (`"confidence":0. nine` — a garbled token), and the system failed closed to
  `ESCALATE_HUMAN` exactly as `GroqLLMClient`'s docstring says it will.
- Double-capture, illegal-transition, and duplicate-refund are structurally
  refused — direct tests capture a payment twice and assert the second call
  raises, rather than only checking that the first one succeeded.
- Injection detection catches every payload in a fixed adversarial set —
  proven against a stub (guaranteed to comply, so a pass there is the guard
  doing 100% of the work) and against a live model too. All 11 payloads end
  at `ESCALATE_HUMAN` and none moves money. Splitting that by who actually
  stopped it: the model's own answer was already safe on 7 of 11, and on the
  other 4 it returned `RETRY_SOFT` — a payment retry, on the instruction of
  an untrusted note — which the deterministic guard rewrote. Which specific
  payloads land in which half shifts between runs; the roughly 7/4 split has
  reproduced, the membership has not, so the guard is load-bearing rather
  than redundant.
- That split is measured from the model's own pre-guard answer
  (`original_llm_action`), which is a correction. The earlier figure quoted
  here — "resisted 0 of 11" — came from keying off `guard_override_reason`,
  which the injection guard sets for *every* flagged note regardless of what
  the model said. That made the reported resistance a constant rather than a
  measurement, and it understated the model. Fixed in
  `test_intelligence_live_api.py`; the safety claim above was never affected,
  only the attribution.

**Assumed, not tested:**
- That a real Razorpay webhook stream behaves the way the mock gateway's
  fault injector models it. The 45-second max webhook delay and the
  Failed→Authorized flip are calibrated to Razorpay's public documentation;
  the 5% delivery-failure rate, the 300-second silence threshold, and every
  confidence-penalty weight are Revora's own design choices with no external
  source, and are labelled as such in the code rather than presented as
  documented figures.
- That `RETRY_SOFT` executing successfully against the mock gateway says
  anything about a real retry's success probability. It does not — the mock
  gateway's retry always succeeds by construction, and the Results section
  below says so explicitly rather than letting the number imply it.

**What the numbers do NOT measure:**
- The 61.3% "Correctly Routed Rate" (see Results) is not a recovery rate. It
  is arithmetic over a synthetic, hand-authored distribution of scenarios —
  adding five high-value rows to the dataset once moved this figure 16
  percentage points without changing a single underlying decision, which is
  the clearest available demonstration that it is not a measurement of
  system quality by itself.
- The naive-baseline comparison's "79 more recoveries" for the naive
  always-retry policy is not evidence that blind retrying works. It's an
  artifact of the mock gateway, where every retry attempt succeeds by
  construction — nothing in the simulator models the probability that a real
  retry against a real bank would land.

**Known untested paths:**
- Gemini has never answered a live call *successfully* in this project.
  Every attempt has failed — either an `InternalServerError` or a `429`
  free-tier quota wall (`"limit: 20, model: gemini-3.7-flash"`). That is the
  reason it is the fallback rather than the primary. The fallback *mechanism*
  is verified live, repeatedly, under both failure types; Gemini's own
  success path is not, so a run that falls through to it is the one path here
  with no live evidence behind it.
- The grounding guard checks that a money-moving recommendation has *a* real
  error object behind it. It does not verify that every individual sentence
  in the model's `reasoning` field is true — a model could still write a
  misleading justification for an action that happens to be otherwise
  permitted, and nothing in this codebase currently catches that.
- No dataset row exercises a partial refund (the event schema has no
  partial-refund event at all) or a second `refund.created` after the first;
  duplicate-refund rejection is proven only by a direct unit test against the
  state machine, not by a dataset-level scenario.

## Status

All eight components are built.

| Component | Tests |
|---|---|
| Mock payment gateway + fault injection | 38 |
| State resolver | 30 |
| Failure propagation tracer | 22 |
| Recommendation layer | 84 |
| Policy engine | 70 |
| Orchestrator + verify | 29 |
| Batch runner (client selection, reproducibility) | 8 |
| API / CORS scoping | 9 |
| Synthetic dataset generator | 53 |
| X-Ray dashboard | 79 |

**343 backend + 79 frontend = 422 tests passing**, plus 18 that are skipped by
default — see below. Every count in this table came from running the
corresponding test file directly (`pytest -q backend/tests/test_<name>.py`)
and reading its own summary line, not from a shared total divided up by hand.

### What is not covered by default

`test_intelligence_live_api.py` (18 tests) exercises prompt injection, and
reasoning quality on ordinary evidence, against a **real** model rather than
the offline stub. It skips unless one of `GROQ_API_KEY`/`GEMINI_API_KEY`/
`GOOGLE_API_KEY` is set, and skips loudly: a green suite without a key is not
evidence it passed.

```bash
GROQ_API_KEY=... python -m pytest backend/tests/test_intelligence_live_api.py -v
```

This file was rewritten in the project's most recent audit pass. Before that,
it was worse than "never run": its `TraceResult` construction had drifted
from the real schema (missing required fields, a removed `resolution_rule`
field) and would have raised a `ValidationError` on the very first test
regardless of provider — invisible the whole time because the module-level
skip meant that code never executed. Fixed, and widened to build its client
from `data/run_batch.py`'s own `_select_llm_client()`, so it now always
exercises whichever provider chain is actually configured — not a fixed
choice hardcoded in the test file.


## Results

A 500-event synthetic batch, replayed end to end with the offline
`StubLLMClient` (always `RETRY_SOFT` — no API key needed, and nothing here
measures model judgement). Two independent runs produce byte-identical
per-event outcomes; only the run ID differs.

| Outcome | Count | Value |
|---|---|---|
| Settled via retry against the mock gateway | 316 | ₹20,15,634 |
| Blocked by policy | 63 | ₹10,02,892 |
| Escalated | 79 | ₹2,66,421 |
| Needs review | 4 | ₹1,796 |
| No action needed | 38 | ₹1,08,362 |
| **Total** | **500** | **₹33,95,105** |

All 10 policy rules fired. 31 rows carried a prompt-injection attempt; all 31
escalated and none executed.

The four `needs_review` rows are the `verify_mismatch_stale_success`
scenario, and they are there on purpose. Each one's failure webhook was
delivered while the authorization and capture that followed were dropped, so
the merchant-visible evidence reads FAILED at full confidence while the
gateway has already captured the payment. The retry planned on that evidence
cannot land — Verify holds the row for a human instead of recording a
recovery it could not confirm.

**On the headline number.** "Correctly Routed Rate" (61.3% of addressable
value) is deliberately not called a recovery rate. A retry always succeeds
against the mock gateway, so the figure measures whether each payment reached
its *correct decision* — not how often a real retry would land. What the run
does demonstrate: correct routing, every guardrail firing, and fail-closed
behaviour on missing compliance fields. What it does not demonstrate: real
retry success probability, or anything about production payment volume.

### Agent Scorecard

The orchestrator already writes these fields to every trace, so this costs
nothing extra to produce and cannot drift from the run it describes.

| Figure | This run | What it means |
|---|---|---|
| Override rate | 7.9% (31 of 392) | The deterministic guard replaced the model's answer on 31 of the answers it gave — every one `RETRY_SOFT` → `ESCALATE_HUMAN` |
| Ambiguity short-circuit rate | 14.0% (70 of 500) | Evidence too thin to ask; answered without calling the model at all |
| Never-consulted rate | 7.6% (38 of 500) | The payment had not failed, so there was nothing to recommend |
| Injection attempts | 31 detected, 0 moved money | |
| Reconciliation | 3 of 3 pass | Recomputed from the events on screen, not read from a stored result |

Override rate is overrides ÷ model consultations (392), not ÷ all 500 rows —
the other 108 rows never consulted the model at all, so they carry no
override risk to count. Nothing in this section is evidence about a real
model's judgement: the committed batch runs `StubLLMClient`, and that
provenance is stated on the dashboard itself, not only here.

### Naive-baseline comparison

Revora evaluated against a naive always-retry baseline across the same 500
synthetic incidents, same seed, same reference instant, each policy against
its own freshly reset gateway. The naive policy has no tracer, no policy
engine, and no verification stage — it retries everything. Every payment is
classified by its state *before* either policy acted, read from gateway
truth; a retry that "succeeds" against an already-captured payment is
counted as duplicate-payment risk, not a recovery.

| Category | Revora | Naive always-retry |
|---|---|---|
| Legitimately recovered | ₹19,56,950 (300) | ₹22,23,371 (379) |
| Already successful, preserved | ₹1,68,842 (58) | ₹1,68,842 (58) |
| Incorrectly put at risk | **₹0 (0)** | ₹10,02,892 (63) |
| Safely blocked | ₹10,02,892 (63) | ₹0 (0) |
| Escalated | ₹2,66,421 (79) | ₹0 (0) |

| Counter | Revora | Naive |
|---|---|---|
| Unsafe retries attempted | 0 | 63 |
| Duplicate-payment risk | 4 | 58 |
| — of which the gateway actually moved money | **0** | **54 (₹1,67,046)** |
| Correct escalations | 79 | 0 |
| Verification failures caught | 4 | 0 |

**Read this honestly, including where the naive policy wins on paper.** The
naive policy books 79 *more* recoveries than Revora — because a retry always
succeeds against the mock gateway, the rows Revora escalates for a status
check, naive simply retries into success. That gap measures the simulator,
not policy quality: nothing here models the probability a real retry would
actually land, so "naive recovers more" is not evidence naive is better.

What the comparison does show, faithfully: naive attempted 63 retries the
policy engine refuses, and retried 58 already-successful payments without
noticing any of them — the gateway actually captured 54 of those a second
time (₹1,67,046 of duplicate-payment risk realised), refusing only the other
4 because they were already `CAPTURED`. Revora attempted the same 4 retries
against already-successful payments and moved money on none of them — the
refused capture left every one still `CAPTURED`, and Verify held all 4 for
review. `already_successful_preserved_paise` is identical (₹1,68,842) for
both policies by construction (it's assigned from prior state, before either
policy acts) — but naive did not preserve that value, it captured most of
it. The naive policy has no verification stage, so its "verification
failures caught" is 0 by construction, not by performing better; it also
never blocks, so it has no safely-blocked equivalent — reported as zero
rather than mirrored across.

These are synthetic incidents under a deterministic simulator. No figure
here is a measurement of production revenue.

```bash
python data/run_baseline.py
```

### Benchmark scope

International payment failures are not modelled — the dataset is INR-only,
and every compliance rule it exercises is RBI's. Subscription-halt-specific
flows are not exercised at the dataset level either:
`SubscriptionState.HALTED` is implemented and unit-tested in the gateway, but
the dataset creates no subscriptions, so retry exhaustion here is governed by
the policy engine's `MAX_RETRIES_EXCEEDED`, not by a subscription halting.

One caveat on the four `needs_review` rows: they depend on their failure
webhook being delivered while the two that follow are dropped. At the 5%
default delivery-failure rate that is reliable and the count is reproducible,
but it is a property of the seed, not a structural guarantee — raise the
failure rate and a stale-success row degrades into ordinary silence,
resolving to `PENDING_WEBHOOK` and escalating instead of landing in
`needs_review`.

## Running

### Backend

```bash
pip install -r backend/requirements.txt
python -m pytest -q                                # 343 passed, 18 skipped (no key); 361 passed (with a key)
uvicorn app.main:app --reload --app-dir backend    # http://localhost:8000
```

Copy `.env.example` to `.env` and fill in what you have — see
[Configuration](#configuration) below for the full list and selection order.
With none set, the recommendation layer runs against a deterministic stub,
and every other layer is unaffected.

### Generating a batch run

The dashboard renders a completed batch run, so one has to exist first. On a
fresh clone `data/batch_results.json` is committed, so this is only needed to
regenerate it:

```bash
python data/generate_synthetic_dataset.py    # 500 events -> data/synthetic_events_500.json
python data/run_batch.py                     # replay -> data/batch_results.json
python data/run_batch.py --stub              # same, but forces the offline stub even if a real key is set
```

### Dashboard

```bash
cd frontend
npm install
npm run sync-data     # REQUIRED before the dashboard can render
npm run dev           # http://localhost:5173
```

`npm run sync-data` copies `data/batch_results.json` into `frontend/public/`,
which is where the dashboard reads it from when no run id is supplied. That
copy is deliberately gitignored — one source of truth for a run, rather than
two files free to drift apart — so a fresh clone has no
`frontend/public/batch_results.json` until this runs, and the page shows a
load error instead of a dashboard until it does.

`npm run dev`, `npm run build`, and `npm test` all run it automatically as a
pre-step. `npm run build` also runs a freshness check
(`npm run check-dist-fresh`) before `npm run preview` serves the built
output, so a dashboard build made before the last data regeneration fails
loudly instead of silently serving stale numbers.

Two ways to load a run:

| URL | Source |
|---|---|
| `http://localhost:5173/` | the synced snapshot |
| `http://localhost:5173/?run=<batch_run_id>` | `GET /api/batch-results/<id>`, live from the backend |

The header states which of the two it used.

### Frontend tests

```bash
npm test              # 79 offline tests, no servers needed
npm run test:live     # end-to-end; needs backend on :8000 and a served dashboard
```

## Layout

```
backend/    FastAPI app: the 6 deterministic + model-driven modules and their tests
data/       standalone dataset generator, the batch runner, and the generated JSON artifacts
frontend/   React dashboard that renders one batch run — read-only, no write path
docs/       narrative end-to-end verification notes (supplementary, not authoritative over this file)
```

```
backend/app/gateway/         mock gateway + fault injection
backend/app/state_machine/   resolves a payment's true state from delivered evidence
backend/app/tracer/          root cause, causal chain, confidence, ambiguity
backend/app/intelligence/    prompt construction, sanitiser, model client, safety guards
backend/app/policy/          the rules, and the engine that applies them in order
backend/app/orchestrator/    the loop, the batch runner integration, the results endpoint
backend/app/orchestrator/schemas.py  the API contract, kept apart from the loop that fills it in
backend/tests/                one test_*.py per module above
data/generate_synthetic_dataset.py   standalone; imports nothing from backend/
data/run_batch.py                    replays the dataset through the orchestrator
frontend/src/                 react dashboard components, api client, metrics
frontend/src/__tests__/       offline test suite (79 tests)
frontend/src/__live__/        end-to-end tests against a real backend + served build
```

## Configuration

Copy `.env.example` to `.env` and fill in what you have — it is read
automatically (`python-dotenv`, wired in `core/config.py`), and a real
exported environment variable always takes precedence over the file.

**Model client selection order:** Groq (with Gemini as an automatic fallback
if both keys below are set) → Gemini alone → the offline stub, each gated on
its own key. Both providers give the same schema-validated structured-output
guarantee, so the order is decided by which one answers reliably: Groq's free
tier carries a full 500-row batch, while Gemini's caps at a request quota a
batch can exhaust partway through.

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | unset | Default model provider (GPT-OSS-120B). Free tier at [console.groq.com](https://console.groq.com). |
| `GEMINI_API_KEY` | unset | Fallback provider, used automatically if `GROQ_API_KEY` fails or errors, or alone if only this key is set. Free tier at [ai.google.dev](https://ai.google.dev); `GOOGLE_API_KEY` also works if that is what you already have set. See [What makes this honest](#what-makes-this-honest) for its live-verification status. |
| `REVORA_CORS_ORIGINS` | `http://localhost:5173, http://127.0.0.1:5173, http://localhost:4173, http://127.0.0.1:4173` | Comma-separated origins allowed to read the API. Scoped rather than `*`, since nothing here needs to be readable by any page on the internet. |
| `REVORA_DISABLE_LLM` | off | Operational kill switch: forces the same fail-safe escalation as a missing client, without touching how the layer is wired. Recognises `""`/`0`/`false`/`no`/`off` (case-insensitive) as off; anything else is on. |
| `REVORA_DISABLED_RULES` | unset | Comma-separated `RuleId` names to skip in the policy engine's value-rule checks, for turning off a threshold fast during a live demo. Cannot reach the opt-out or missing-RBI-field gates — they aren't in the disablable list. An unrecognised name is logged separately rather than silently accepted. |

Everything else that looks like configuration (webhook delivery-failure
rate, circuit-breaker thresholds, silence threshold, per-provider timeouts)
is a Python-level default in `core/config.py`, not an environment variable —
it's changed by passing a constructor argument (e.g. `run_batch.py
--failure-rate`), not by setting a shell variable. Listing them here as env
vars would be inventing an interface that does not exist.

## Deliberately out of scope

No real Razorpay API calls, no API keys in the repo, no card data, no
database, no authentication, no message queue. These are design decisions
for a 15-day scoped build, not gaps discovered late:

- **No database.** State lives in in-memory Python dicts plus a single JSON
  file as the durability layer. This is why running more than one backend
  replica of this code today would produce disagreeing copies of gateway
  truth — a real deployment needs the state extracted into a shared store
  first, and nothing here does that yet.
- **No queue.** Batch processing is a synchronous script; a crash mid-batch
  loses progress rather than resuming.
- **No auth.** This is a backend decision engine, not a merchant-facing
  product in this build.
- **Single process, in-memory state, injectable clock throughout** — so a
  documented 45-second webhook delay is exercised instantly in tests and at
  real speed in a demo, but nothing here has been tested under real
  concurrency beyond the one-process, one-lock model FastAPI's threadpool
  already requires.

The synthetic dataset's bucket split (60/20/10/10) is a test-coverage design
choice — it allocates rows so every code path is exercised at a useful
sample size. It is not a measurement of how payments fail in production, and
nothing here supports presenting it as one. Error `code`, `field`, `source`,
`step`, and `reason` values are drawn only from Razorpay's documented
vocabulary; where a scenario needed a value the reference does not supply,
the gap is recorded in the dataset's `value_provenance` field rather than
filled with a plausible-sounding substitute.
