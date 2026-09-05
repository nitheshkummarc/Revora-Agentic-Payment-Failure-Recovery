<h1 align="center">REVORA</h1>

<p align="center">
  <strong>Agentic Payment Failure Recovery</strong>
</p>

<p align="center">
  Revora works out whether a failed payment can be safely and legally retried.
</p>

The parts that decide **what happened** and **why** are deterministic and
provable from evidence. Only one question gets a model: **what should we do
about it?** And the model's answer is a recommendation, not a command. A policy
engine sits behind it and can throw it out.

## The idea

A failed payment rarely explains itself. Webhooks show up late, out of order,
twice, or never. A payment marked `Failed` can flip to `Authorized` minutes
later when a delayed bank response finally lands. The merchant's own dashboard
is reading the same incomplete evidence a recovery bot would.

Here's the concrete version. A payment fails. A retry bot sees `Failed` and
retries it. Ten minutes later the bank's original authorization arrives, and it
turns out the payment went through the first time. The bot has now charged the
customer twice. Nothing in "just retry failed payments" catches that, because
the bot was reading state that had already gone stale.

There's a second failure sitting right next to it. Say the payment really did
fail, but retrying it would break an RBI mandate rule: no pre-debit notice, or
an amount over the customer's mandate ceiling. Acting fast and acting compliant
aren't the same requirement, and a system tuned only for the first will
eventually fail the second.

So Revora splits the questions that usually get collapsed into one
**"should I retry this?"** call:

| Question | Answered by | Deterministic? |
|---|---|---|
| What state is this payment actually in? | State resolver | Yes |
| Why did it fail? | Failure propagation tracer | Yes |
| What should we do about it? | Recommendation layer | No — model |
| Are we allowed to? | Policy engine | Yes |

Only the third one involves a model.

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

Two boundaries are enforced by schema rather than by convention. A Pydantic
model makes the violation impossible to construct, where a docstring only asks
nicely.

**The model only ever sees the tracer's output.** `IntelligenceInput` has four
fields and `extra="forbid"`. There's nowhere to put a raw gateway event, so raw
logs can't leak into a prompt even by accident.

**The model's output reaches the gateway only through the policy engine.** The
recommendation layer imports no gateway code at all. There's no function it
could call to move money.

Two more documents go deeper if you want detail without reading source:

- [`docs/architecture.md`](docs/architecture.md) covers what each backend module
  owns and doesn't own, how the two boundaries above are actually enforced, a
  stage-by-stage trace of one event, and what every stage does when it fails.
- [`docs/methodology.md`](docs/methodology.md) covers how the 500-event dataset
  was built, what its bucket split is for, why the headline batch runs against a
  stub while the live tests prove something else entirely, and what would have
  to change to evaluate this against a real payment stream.

Neither introduces a number that isn't already here. This file stays
authoritative.

## What's proven and what isn't

### Proven by tests, not just claimed

Root causes get quoted from the event's own error object, never invented. A test
builds a chain with no error object and asserts the tracer refuses it
(`NotTraceableError`) instead of making something up.

An ambiguous trace never reaches the model. That's proven with
`ExplodingLLMClient`, a test double that raises if it's called at all, rather
than by checking a boolean the code sets itself.

A malformed model response can't become a money-moving action. I've seen this
one live, not just in tests: a real Groq response failed its own strict-JSON
validation once (`"confidence":0. nine`, a garbled token) and the system failed
closed to `ESCALATE_HUMAN`, exactly as `GroqLLMClient`'s docstring says it will.

Double-capture, illegal transitions and duplicate refunds are structurally
refused. The tests capture a payment twice and assert the second call raises,
rather than just checking the first one worked.

Every regulatory threshold was checked against the circular's actual text, not a
paraphrase. The 24-hour pre-debit notice, the ₹15,000 general AFA threshold, the
₹1 lakh threshold for the three categories the circular names (mutual-fund
subscriptions, insurance premiums, credit card bills), and the customer-set
mandate ceiling all match RBI/DPSS/2026-27/396. That includes the boundary: the
circular allows "up to ₹15,000" without AFA, and the code lets exactly ₹15,000
through.

Limits that aren't regulatory don't cite the circular. `MAX_DISCOUNT` is a
business cap and `MAX_RETRIES` is Razorpay's own subscription-halt threshold, so
both block without invoking it. The minimum-trace-confidence constant is
commented as having no regulatory basis at all. The opt-out block cites the
circular for the obligation, then names the permanent cooldown as my own choice
rather than something the circular sets. No section numbers appear anywhere,
because I never verified any against the source text.

Injection detection catches every payload in a fixed adversarial set. That's
proven against a stub (which is rigged to comply, so a pass there means the
guard did 100% of the work) and against a live model. All 11 payloads end at
`ESCALATE_HUMAN` and none moves money.

Split by who actually stopped it: the model's own answer was
already safe on 7 of 11. On the other 4 it returned `RETRY_SOFT`, meaning it
wanted to retry a payment because an untrusted customer note told it to, and the
deterministic guard rewrote that. Which payloads land in which half moves
between runs. The rough 7/4 ratio has held; the membership hasn't. So the guard
is load-bearing, not redundant.

That split comes from the model's pre-guard answer (`original_llm_action`), and
it's a correction. The number I had here before was "resisted 0 of 11", which
came from keying off `guard_override_reason` instead. The injection guard sets
that field for *every* flagged note regardless of what the model said, so the
figure could never have been anything but zero. It was a constant dressed up as
a measurement, and it understated the model. Fixed in
`test_intelligence_live_api.py`. The safety claim above was never affected, only
the attribution.

### Assumed, not tested

That a real Razorpay webhook stream behaves the way my fault injector models it.
The 45-second max delay and the Failed→Authorized flip come from Razorpay's
public docs. The 5% delivery-failure rate, the 300-second silence threshold and
every confidence-penalty weight are my own design choices with no external
source, and the code labels them that way instead of dressing them up as
documented figures.

That `RETRY_SOFT` succeeding against the mock gateway says anything about a real
retry's odds. It doesn't. The mock gateway's retry always succeeds by
construction, and the Results section below says so rather than letting the
number imply otherwise.

### What the numbers do NOT measure

The 61.3% "Correctly Routed Rate" is not a recovery rate. It's arithmetic over a
synthetic, hand-authored spread of scenarios. Adding five high-value rows to the
dataset once moved that figure 16 percentage points without changing a single
underlying decision. So it isn't a measure of system quality on its own.

The naive baseline booking "79 more recoveries" isn't evidence that blind
retrying works. It's an artifact of the mock gateway, where every retry succeeds
by construction. Nothing in the simulator models whether a real retry against a
real bank would land.

### Known untested paths

Gemini has never once answered a live call successfully here. Every attempt
failed, either an `InternalServerError` or a `429` free-tier quota wall
(`"limit: 20, model: gemini-3.7-flash"`). That's exactly why it's the fallback
and not the primary. The fallback *mechanism* is verified live and repeatedly,
under both failure types. Gemini's own success path isn't, so a run that falls
through to it is the one path here with no live evidence behind it.

The grounding guard checks that a money-moving recommendation has *a* real error
object behind it. It does not check that every sentence in the model's
`reasoning` is true. A model could still write a misleading justification for an
action that's otherwise permitted, and nothing in this codebase catches that.

No dataset row exercises a partial refund (the event schema has no
partial-refund event at all) or a second `refund.created` after the first.
Duplicate-refund rejection is proven by a direct unit test against the state
machine, not by a dataset scenario.

## Status

All eight components are built.

| Component | Tests |
|---|---|
| Mock payment gateway + fault injection | 38 |
| State resolver | 30 |
| Failure propagation tracer | 22 |
| Recommendation layer | 84 |
| Policy engine | 73 |
| Orchestrator + verify | 29 |
| Batch runner (client selection, reproducibility) | 8 |
| API / CORS scoping | 9 |
| Synthetic dataset generator | 53 |
| X-Ray dashboard | 79 |

**346 backend + 79 frontend = 425 tests passing**, plus 18 skipped by default.
Every count in that table came from running the file directly
(`pytest -q backend/tests/test_<name>.py`) and reading its summary line. None of
it is a shared total divided up by hand.

### What isn't covered by default

`test_intelligence_live_api.py` (18 tests) runs prompt injection and
reasoning-quality checks against a real model instead of the stub. It skips
unless one of `GROQ_API_KEY`/`GEMINI_API_KEY`/`GOOGLE_API_KEY` is set, and it
skips loudly. A green suite with no key is not evidence it passed.

```bash
GROQ_API_KEY=... python -m pytest backend/tests/test_intelligence_live_api.py -v
```

This file got rewritten in the last audit pass, and it was worse than "never
run" beforehand. Its `TraceResult` construction had drifted from the real schema
(missing required fields, plus a `resolution_rule` field that no longer exists)
and would have thrown a `ValidationError` on the very first test, whichever
provider was configured. The module-level skip meant that code never executed,
so nobody found out. It's fixed now, and widened to build its client from
`data/run_batch.py`'s own `_select_llm_client()` so it always exercises whatever
provider chain is actually configured.

## Results

A 500-event synthetic batch, replayed end to end against the offline
`StubLLMClient`, which always returns `RETRY_SOFT`. No API key needed, and
nothing here measures model judgement. Two independent runs produce
byte-identical per-event outcomes; only the run ID changes.

| Outcome | Count | Value |
|---|---|---|
| Settled via retry against the mock gateway | 316 | ₹20,15,634 |
| Blocked by policy | 63 | ₹10,02,892 |
| Escalated | 79 | ₹2,66,421 |
| Needs review | 4 | ₹1,796 |
| No action needed | 38 | ₹1,08,362 |
| **Total** | **500** | **₹33,95,105** |

All 10 policy rules fired. 31 rows carried a prompt-injection attempt. All 31
escalated, none executed.

The four `needs_review` rows are the `verify_mismatch_stale_success` scenario,
and they're there on purpose. Each one's failure webhook was delivered while the
authorization and capture that followed got dropped. So the merchant-visible
evidence reads FAILED at full confidence while the gateway has already captured
the payment. The retry planned on that evidence can't land, and Verify holds the
row for a human instead of booking a recovery it couldn't confirm.

"Correctly Routed Rate" (61.3% of addressable value) is not called a recovery
rate, and that's on purpose. A retry always succeeds
against the mock gateway, so the figure measures whether each payment reached
its *correct decision*, not how often a real retry would land. The run
demonstrates correct routing, every guardrail firing, and fail-closed behaviour
on missing compliance fields. It demonstrates nothing about real retry success
probability or production volume.

### Agent Scorecard

The orchestrator already writes these fields to every trace, so this costs
nothing to produce and can't drift from the run it describes.

| Figure | This run | What it means |
|---|---|---|
| Override rate | 7.9% (31 of 392) | The guard replaced the model's answer on 31 of the answers it gave, every one `RETRY_SOFT` → `ESCALATE_HUMAN` |
| Ambiguity short-circuit rate | 14.0% (70 of 500) | Evidence too thin to ask, so it answered without calling the model |
| Never-consulted rate | 7.6% (38 of 500) | The payment hadn't failed, so there was nothing to recommend |
| Injection attempts | 31 detected, 0 moved money | |
| Reconciliation | 3 of 3 pass | Recomputed from the events on screen, not read from a stored result |

Override rate is overrides ÷ model consultations (392), not ÷ 500. The other 108
rows never consulted the model, so they carry no override risk to count. None of
this is evidence about a real model's judgement, since the committed batch runs
`StubLLMClient`. The dashboard says so on screen too, not just here.

### Naive-baseline comparison

Revora against a naive always-retry baseline, same 500 incidents, same seed,
same reference instant, each policy against its own freshly reset gateway. The
naive policy has no tracer, no policy engine and no verification stage. It
retries everything.

Every payment is classified by its state *before* either policy acted, read from
gateway truth. A retry that "succeeds" against an already-captured payment
counts as duplicate-payment risk, not a recovery.

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

Naive books 79 more recoveries than Revora. That's because a retry always
succeeds against the mock gateway, so the rows Revora escalates for a status
check are rows naive simply retries into success. That gap measures the
simulator, not policy quality. Nothing here models whether a real retry would
land, so "naive recovers more" isn't evidence naive is better.

What the comparison does show faithfully is the cost side, which the simulator
models properly. Naive attempted 63 retries the policy engine refuses. It
retried 58 already-successful payments without noticing any of them, and the
gateway captured 54 of those a second time. That's ₹1,67,046 of
duplicate-payment risk actually realised. Revora attempted the same 4 retries
against already-successful payments and moved money on none of them: the refused
capture left every one still `CAPTURED`, and Verify held all 4 for review.

`already_successful_preserved_paise` in that first table is ₹1,68,842 for both
policies by construction, since it's assigned from prior state before either
policy acts. Naive did not preserve that value. It captured most of it. Naive's
"verification failures caught" is 0 because it has no verification stage, not
because it did better, and it never blocks so it has no safely-blocked
equivalent.

These are synthetic incidents under a deterministic simulator. No figure here
measures production revenue.

```bash
python data/run_baseline.py
```

### Benchmark scope

International payment failures aren't modelled. The dataset is INR-only and
every compliance rule it exercises is RBI's.

Subscription-halt flows aren't exercised at dataset level either.
`SubscriptionState.HALTED` is implemented and unit-tested in the gateway, but
the dataset creates no subscriptions, so retry exhaustion here is governed by the
policy engine's `MAX_RETRIES_EXCEEDED` rather than by a subscription halting.

One caveat on those four `needs_review` rows. They depend on the failure webhook
landing while the two after it get dropped. At the 5% default failure rate that's
reliable and the count reproduces, but it's a property of the seed rather than a
structural guarantee. Raise the failure rate and a stale-success row degrades
into ordinary silence, resolving to `PENDING_WEBHOOK` and escalating instead of
landing in `needs_review`.

## Running

### Backend

```bash
pip install -r backend/requirements.txt
python -m pytest -q                                # 346 passed, 18 skipped (no key); 364 passed (with a key)
uvicorn app.main:app --reload --app-dir backend    # http://localhost:8000
```

Copy `.env.example` to `.env` and fill in whatever you have. See
[Configuration](#configuration) for the full list. With nothing set, the
recommendation layer runs against the deterministic stub and every other layer
is unaffected.

### Generating a batch run

The dashboard renders a completed batch run, so one has to exist first.
`data/batch_results.json` is committed, so you only need this to regenerate it:

```bash
python data/generate_synthetic_dataset.py    # 500 events -> data/synthetic_events_500.json
python data/run_batch.py --stub              # replay against the offline stub -> data/batch_results.json
python data/run_batch.py                     # same, but uses a real model if a key is set
```

Use `--stub` to reproduce the committed baseline. Without it, and with a key in
`.env`, the batch makes around 392 real API calls.

### Dashboard

```bash
cd frontend
npm install
npm run sync-data     # REQUIRED before the dashboard can render
npm run dev           # http://localhost:5173
```

`npm run sync-data` copies `data/batch_results.json` into `frontend/public/`,
which is where the dashboard reads it from when no run id is given. That copy is
gitignored on purpose, so there's one source of truth for a run rather than two
files free to drift apart. On a fresh clone it doesn't exist yet, and the page
shows a load error until you run it.

`npm run dev`, `npm run build` and `npm test` all run it automatically first.
`npm run build` also runs a freshness check (`npm run check-dist-fresh`) before
`npm run preview` serves the built output, so a build made before the last data
regeneration fails loudly instead of quietly serving stale numbers.

Two ways to load a run:

| URL | Source |
|---|---|
| `http://localhost:5173/` | the synced snapshot |
| `http://localhost:5173/?run=<batch_run_id>` | `GET /api/batch-results/<id>`, live from the backend |

The header tells you which one it used.

### Frontend tests

```bash
npm test              # 79 offline tests, no servers needed
npm run test:live     # end-to-end; needs backend on :8000 and a served dashboard
```

## Backend routes

Seven routes, and only one of them matters to the dashboard. The rest exist so
the mock gateway can be driven directly, which is how the batch runner seeds
each scenario.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/health` | Liveness check. Returns `{"status":"ok"}`. |
| `GET` | `/api/batch-results/{batch_run_id}` | The completed batch run the dashboard renders. Served from an in-memory store, capped at 20 runs, falling back to the JSON file on disk. Unknown id returns 404. |
| `POST` | `/payments/create` | Create a payment in the mock gateway. |
| `POST` | `/payments/fail` | Fail a payment, with an error object and optional chaos mode. |
| `POST` | `/payments/capture` | Capture an authorized payment. Refuses if the payment isn't in a capturable state. |
| `GET` | `/payments/{payment_id}/status` | Query gateway truth for one payment. This is the only sanctioned route to the real state, and it's what `REQUEST_VERIFICATION` calls instead of retrying blind. |
| `POST` | `/webhooks/simulate` | Emit a webhook event, subject to whatever chaos mode is configured. |

There's no route at `/`, so hitting `http://localhost:8000` directly returns a
404. That's expected: this serves JSON, not HTML. The dashboard is on `:5173`.
`/docs` gives you the generated Swagger UI if you want to poke at any of the
above by hand.

No authentication on any of them. See [Deliberately out of
scope](#deliberately-out-of-scope).

## Layout

```
backend/    FastAPI app: the 6 deterministic + model-driven modules and their tests
data/       standalone dataset generator, the batch runner, and the generated JSON artifacts
frontend/   React dashboard that renders one batch run — read-only, no write path
docs/       architecture and evaluation-methodology notes (supplementary to this file)
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

Copy `.env.example` to `.env`. It's read automatically via `python-dotenv`,
wired in `core/config.py`, and a real exported environment variable always wins
over the file.

**Model client selection order:** Groq first (with Gemini as an automatic
fallback if both keys are set), then Gemini alone, then the offline stub. Each is
gated on its own key. Both providers give the same schema-validated
structured-output guarantee, so the order comes down to which one actually
answers. Groq's free tier carries a full 500-row batch. Gemini's caps out at a
request quota a batch can exhaust partway through.

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | unset | Default model provider (GPT-OSS-120B). Free tier at [console.groq.com](https://console.groq.com). |
| `GEMINI_API_KEY` | unset | Fallback provider, used automatically if `GROQ_API_KEY` fails or errors, or alone if only this key is set. Free tier at [ai.google.dev](https://ai.google.dev); `GOOGLE_API_KEY` works too. See [What's proven and what isn't](#whats-proven-and-what-isnt) for its live-verification status. |
| `REVORA_CORS_ORIGINS` | `http://localhost:5173, http://127.0.0.1:5173, http://localhost:4173, http://127.0.0.1:4173` | Comma-separated origins allowed to read the API. Scoped rather than `*`, since nothing here needs to be readable by any page on the internet. |
| `REVORA_DISABLE_LLM` | off | Kill switch. Forces the same fail-safe escalation as a missing client without touching how the layer is wired. Reads `""`/`0`/`false`/`no`/`off` (case-insensitive) as off; anything else is on. |
| `REVORA_DISABLED_RULES` | unset | Comma-separated `RuleId` names to skip in the policy engine's value-rule checks, for turning off a threshold quickly during a demo. Can't reach the opt-out or missing-RBI-field gates, which aren't in the disablable list. An unrecognised name gets logged rather than silently accepted. |

Everything else that looks like configuration (webhook delivery-failure rate,
circuit-breaker thresholds, silence threshold, per-provider timeouts) is a
Python-level default in `core/config.py`, not an environment variable. You
change it by passing a constructor argument, like `run_batch.py --failure-rate`.
Listing those here as env vars would be inventing an interface that doesn't
exist.

## Deliberately out of scope

No real Razorpay API calls, no API keys in the repo, no card data, no database,
no auth, no message queue. These were decisions for a 15-day build, not gaps I
discovered late.

**No database.** State lives in in-memory Python dicts plus one JSON file as the
durability layer. That's why running more than one backend replica today would
produce disagreeing copies of gateway truth. A real deployment needs the state
pulled out into a shared store first, and nothing here does that.

**No queue.** Batch processing is a synchronous script. A crash mid-batch loses
progress rather than resuming.

**No auth.** This is a backend decision engine, not a merchant-facing product in
this build.

**Single process, in-memory state, injectable clock throughout.** That's what
lets a documented 45-second webhook delay run instantly in tests and at real
speed in a demo. But nothing here has been tested under real concurrency beyond
the one-process, one-lock model FastAPI's threadpool already requires.

The dataset's 60/20/10/10 bucket split is a test-coverage choice. It allocates
rows so every code path gets exercised at a useful sample size. It is not a
measurement of how payments fail in production and nothing here supports
presenting it as one. Error `code`, `field`, `source`, `step` and `reason` values
come only from Razorpay's documented vocabulary. Where a scenario needed a value
the reference doesn't supply, the gap is recorded in the dataset's
`value_provenance` field instead of being filled with a plausible-sounding
substitute.