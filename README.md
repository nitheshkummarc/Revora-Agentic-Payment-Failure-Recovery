# Revora

Payment-failure root-cause analysis and recovery decisioning, with a
deterministic diagnosis layer between the evidence and the model.

> Revora traces the causal chain of *why* a batch of payments degraded, then
> decides — and it fails closed on RBI mandate rules, not just retry limits.

## The idea

A failed payment rarely explains itself. Webhooks arrive late, out of order,
twice, or never; a payment marked Failed can flip to Authorized minutes later
when a delayed bank response lands. Acting on that evidence as though it were
complete is how automated recovery quietly does the wrong thing.

Revora separates the three questions that usually get collapsed into one:

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
        │  delivered webhook events only
        ▼
State resolver ──────────────► what state is this in?
        │  full resolution, including what it could not place
        ▼
Failure propagation tracer ──► why did it fail?
        │  root cause, causal chain, confidence, ambiguity
        ▼
Recommendation layer (model) ► what should we do?
        │  recommendation only, never a command
        ▼
Policy engine ───────────────► are we allowed to?
        │  approved / blocked with the rule that fired
        ▼
Orchestrator ────────────────► execute, then verify
        ▼
Dashboard
```

Two boundaries are enforced structurally rather than by convention:

- **The model only ever sees the tracer's output.** Its input schema has no
  field that can carry a raw gateway event, so raw logs cannot leak into a
  prompt even by accident.
- **The model's output only reaches the gateway through the policy engine.**
  The recommendation layer has no gateway import at all.

## What makes the diagnosis honest

- **Resolution works from evidence, not truth.** The resolver reads delivered
  webhooks and timestamps. It has no access to the gateway's internal state, so
  a silently dropped webhook reads as silence — which, past this project's
  configured evidence threshold (`SILENCE_THRESHOLD_SECONDS = 300`, a Revora
  design choice rather than a documented Razorpay figure), means "check the
  status", not "assume it failed".
- **A gap in the event chain is reported, never smoothed over.** Missing
  telemetry sets `ambiguous: true` rather than returning a shorter chain that
  looks complete.
- **Root causes are quoted, not paraphrased.** The tracer emits the literal
  `source`, `step` and `reason` from the event's own error object, so any cause
  can be traced back to the event that produced it.
- **An ambiguous trace never reaches the model.** It short-circuits to a status
  check, so the model is never asked to guess in place of missing data.
- **Compliance fails closed.** A missing pre-debit notice, mandate ceiling or
  AFA flag blocks the action. Absence is checked before any value comparison,
  so an absent field can never be read as a passing value.

## Status

All eight components are built.

| Component | Tests |
|---|---|
| Mock payment gateway + fault injection | 37 |
| State resolver | 28 |
| Failure propagation tracer | 22 |
| Recommendation layer | 74 |
| Policy engine | 70 |
| Orchestrator + verify | 29 |
| API / CORS scoping | 9 |
| Synthetic dataset generator | 53 |
| X-Ray dashboard | 79 |

**325 backend + 79 frontend = 404 tests passing**, plus 17 that are skipped by
default — see below. The frontend count is as last reported by an audit pass;
no Node.js/npm has been available in the sessions since to re-run it directly.

### What is not covered

A further 17 tests exercise prompt injection against the **real** model rather
than the offline stub. They skip unless `ANTHROPIC_API_KEY` is set, and they
skip loudly: a green suite without a key is not evidence they passed. No key
has been available in any session this project has been built or audited in,
so these have never run.

```bash
ANTHROPIC_API_KEY=... python -m pytest backend/tests/test_intelligence_live_api.py -v
```

Until that runs, every claim about injection resistance rests on the
deterministic guard, which is tested against a stub rigged to comply with
every injection case. That is a real property of the guard itself. It is not
the same claim as "the model resists injection" — that claim is currently
untested, not merely unproven, and the two must not be conflated.

## Results

A 500-event synthetic batch, replayed end to end. Two independent runs produce
byte-identical per-event outcomes.

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

The four `needs_review` rows are the `verify_mismatch_stale_success` scenario,
and they are there on purpose. Each one's failure webhook was delivered while
the authorization and capture that followed were dropped, so the merchant-visible
evidence reads FAILED at full confidence while the gateway has already captured
the payment. The retry planned on that evidence cannot land, and Verify holds the
row for a human instead of recording a recovery it could not confirm. A run with
no such rows demonstrates less, not more.

**On the headline number.** The dashboard reports a *Correctly Routed Rate*
(61.3% of addressable value), deliberately not a recovery rate. A retry always
succeeds against the mock gateway, so the figure measures whether each payment
reached its correct decision — not how often a real retry would land. Adding
five high-value rows to the dataset once moved it 16 percentage points without
changing a single decision, which is the clearest demonstration that it is
arithmetic over a synthetic distribution rather than a measurement.

What the run does demonstrate: correct routing, every guardrail firing, and
fail-closed behaviour on missing compliance fields.

## Agent Scorecard

Revora evaluates its own agent's behaviour on every run, not just whether an
action was taken. The dashboard's Agent Scorecard aggregates fields the
orchestrator already writes to each trace, so it costs nothing to produce and
cannot drift from the run it describes.

| Figure | This run | What it means |
|---|---|---|
| Override rate | **7.9%** (31 of 392) | The deterministic guard replaced the model's answer on 31 of the answers it gave — every one `RETRY_SOFT` → `ESCALATE_HUMAN` |
| Ambiguity short-circuit rate | **14.0%** (70 of 500) | Evidence too thin to ask; answered without calling the model at all |
| Never-consulted rate | **7.6%** (38 of 500) | The payment had not failed, so there was nothing to recommend |
| Injection attempts | **31 detected, 0 moved money** | |
| Reconciliation | **3 of 3 pass** | Recomputed from the events on screen, not read from a stored result |

**Override rate = overrides ÷ model consultations, not ÷ all 500 rows.** 392 is
the denominator because that is how many rows actually reached the model — the
other 108 were answered without consulting it at all (see the next two rows),
so they carry no override risk to count.

So **21.6% of rows never reached the model**, and of those that did, **one in
twelve had its answer overruled**. Both are properties of the pipeline around
the model rather than of the model itself — which is exactly why the section
leads with a provenance line stating that the run does not record which model
answered. The committed batch comes from `data/run_batch.py`, which wires
`StubLLMClient`. Nothing in this section is evidence about a real model's
judgement, and the label says so on screen rather than only here.

## Naive-baseline comparison

Revora was evaluated against a naive always-retry baseline across 500 identical
synthetic incidents. Both policies replay the same generated rows, from the same
seed and the same reference instant, each against its own freshly reset gateway,
so the only variable is the policy. The naive policy has no tracer, no policy
engine and no verification stage: it retries everything.

Every payment is classified by its state *before* either policy acted, read from
gateway truth. A retry that "succeeds" against a payment the gateway had already
captured is not a recovery — it is duplicate-payment risk, and it is counted as
one.

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

The duplicate-payment row is worth splitting, because the totals flatter the
naive policy. Of the 58 already-successful payments it retried, the gateway
captured 54 of them — money moved on a payment that had already succeeded — and
refused the other 4 only because they were already `CAPTURED`. On those 4 the
naive policy was saved by gateway enforcement, not by anything it did. Revora
attempted 4 retries against already-successful payments and moved money on none
of them: the refused capture left every one still `CAPTURED`, and Verify held
all 4 for review.

For the same reason, read `already_successful_preserved_paise` carefully. It is
assigned by prior state, so both policies show an identical ₹1,68,842 — but the
naive policy did not preserve that value, it captured most of it. The category
records what was true before either policy acted; the duplicate-payment counter
above is what records what they then did about it.

Read honestly, in both directions. The naive policy books 79 more recoveries than
Revora, because **a retry always succeeds against the mock gateway** — the rows
Revora escalates for a status check, the naive policy simply retries into
success. Nothing here models the probability that a real retry would land, so
that gap is a property of the simulator, not evidence that retrying everything
works. What the comparison does show is the cost side, which the simulator models
faithfully: the naive policy attempted 63 retries the RBI policy engine refuses,
put ₹10,02,892 at risk that Revora preserved, and retried 58 payments that had
already succeeded without noticing any of them. Revora attempted 4 such retries
and its Verify stage caught all 4; the naive policy has no verification stage, so
its "verification failures caught" is 0 by construction rather than by performing
better. The naive policy never blocks, so it has no safely-blocked equivalent —
that row is reported as zero rather than mirrored across.

These are synthetic incidents under a deterministic simulator. No figure here is
a measurement of production revenue.

```bash
python data/run_baseline.py
```

### Benchmark scope

Two things are deliberately outside this benchmark. International payment
failures are not modelled: the dataset is INR-only and every compliance rule it
exercises is RBI's, so a cross-border failure has no represented error vocabulary
or rule set. Subscription-halt-specific flows are not exercised at the dataset
level either: `SubscriptionState.HALTED` and `subscription.halted` are
implemented and unit-tested in the gateway, but the dataset creates no
subscriptions, so retry exhaustion is governed here by the policy engine's
`MAX_RETRIES_EXCEEDED` rather than by a subscription halting.

Two mechanisms that used to be in this list are not any more. Out-of-order
webhook delivery and a partial gap in the event chain were previously proven
only by unit tests, with nothing in the generated dataset exercising either.
Both are now dataset-level scenarios in the `ambiguous` bucket (`chain_gap`,
`out_of_order`): a silently dropped intermediate webhook leaves
`missing_sequences: [2]` for the tracer's structural gap rule to report, and a
Failed-to-Authorized flip with its authorization scheduled to *arrive* before
the failure confirms the resolver orders by `(occurred_at, sequence)` rather
than by delivery order. Both were verified against the real 500-row batch, not
just asserted: see `data/generate_synthetic_dataset.py`'s `build_ambiguous`.

**Known untested paths in the gateway.** All four paths previously listed here
now have direct tests, plus one more the naive-baseline comparison depends on
that was not on the original list: capturing an already-`CAPTURED` payment.
None of the five needed a behaviour change -- every one was already correct,
just previously unexercised. Named here rather than silently dropped from the
README, since "this list used to be longer" is worth being able to check:
exhausted webhook retry budget, illegal-transition refusal guarding gateway
truth, the same guarding the merchant-visible derived state, refusing to fail a
payment from a terminal state, and refusing to capture one twice.

One caveat on the four `needs_review` rows. They depend on their failure webhook
being delivered while the two that follow are dropped. At the 5% default delivery
failure rate that is reliable and the count is reproducible, but it is a property
of the seed rather than a structural guarantee: raise the failure rate and a
stale-success row degrades into ordinary silence, resolving to `PENDING_WEBHOOK`
and escalating instead.

## Running

### Backend

```bash
pip install -r backend/requirements.txt
python -m pytest -q                                # 325 passed, 17 skipped
uvicorn app.main:app --reload --app-dir backend    # http://localhost:8000
```

Set `ANTHROPIC_API_KEY` to enable live recommendation calls. Without it the
recommendation layer runs against a deterministic stub, and every other layer
is unaffected.

### Generating a batch run

The dashboard renders a completed batch run, so one has to exist first. On a
fresh clone `data/batch_results.json` is committed, so this is only needed to
regenerate it:

```bash
python data/generate_synthetic_dataset.py    # 500 events -> data/synthetic_events_500.json
python data/run_batch.py                     # replay -> data/batch_results.json
```

### Dashboard

```bash
cd frontend
npm install
npm run sync-data     # REQUIRED before the dashboard can render
npm run dev           # http://localhost:5173
```

**`npm run sync-data` is required on a fresh clone.** It copies
`data/batch_results.json` into `frontend/public/`, which is where the dashboard
reads it from when no run id is supplied. That copy is deliberately
gitignored — one source of truth for a run, rather than two files free to drift
apart — so a fresh clone has no `frontend/public/batch_results.json` until this
runs, and the page will show a load error instead of a dashboard.

`npm run dev`, `npm run build` and `npm test` all run it automatically as a
pre-step; the only time it needs running by hand is before serving a build
directly (`npx vite preview`) or when `data/batch_results.json` has been
regenerated while the dev server is already up.

Two ways to load a run:

| URL | Source |
|---|---|
| `http://localhost:5173/` | the synced snapshot |
| `http://localhost:5173/?run=<batch_run_id>` | `GET /api/batch-results/<id>`, live from the backend |

The header states which of the two it used.

### Frontend tests

```bash
npm test              # 79 offline tests, no servers needed (as last reported by
                       # an audit pass; not re-run since -- no Node.js/npm
                       # available in the sessions since)
npm run test:live     # end-to-end; needs backend on :8000 and a served dashboard
```

## Layout

```
backend/app/gateway/         mock gateway + fault injection
backend/app/state_machine/   resolves a payment's true state from delivered evidence
backend/app/tracer/          root cause, causal chain, confidence, ambiguity
backend/app/intelligence/    prompt construction, sanitiser, model client
backend/app/policy/          the rules, and the engine that applies them in order
backend/app/orchestrator/    the loop, the batch runner, the results endpoint
backend/app/orchestrator/schemas.py  the API contract, kept apart from the loop that fills it in
data/generate_synthetic_dataset.py   standalone; imports nothing from backend/
data/run_batch.py                    replays the dataset through the orchestrator
frontend/src/                react dashboard, read-only
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables live model calls. Without it the recommendation layer uses a deterministic stub and every other layer is unaffected. |
| `REVORA_CORS_ORIGINS` | the four localhost dev/preview origins | Comma-separated origins allowed to read the API. Scoped rather than `*`, since nothing here needs to be readable by any page on the internet. |
| `REVORA_DISABLE_LLM` | off | Operational kill switch: forces the same fail-safe escalation as a missing client, without touching how the layer is wired. Recognises `""`/`0`/`false`/`no`/`off` (case-insensitive) as off; anything else is on. |
| `REVORA_DISABLED_RULES` | unset | Comma-separated `RuleId` names to skip in the policy engine's value-rule checks, for turning off a threshold fast during a live demo. Cannot reach the opt-out or missing-RBI-field gates -- they aren't in the disablable list. An unrecognised name is logged separately rather than silently accepted. |

## Scope

No real Razorpay API calls, no API keys in the repo, no card data, no database,
no authentication. Everything runs in a single process with in-memory state.
Time-dependent behaviour uses an injectable clock, so a documented 45-second
webhook delay is exercised instantly in tests and at real speed in a demo.

The synthetic dataset's bucket split (60/20/10/10) is a test-coverage design
choice — it allocates rows so every code path is exercised at a useful sample
size. It is not a measurement of how payments fail in production, and nothing
here supports presenting it as one. Error `code`, `field`, `source`, `step` and
`reason` values are drawn only from Razorpay's documented vocabulary; where a
scenario needed a value the reference does not supply, the gap is recorded in
the dataset's `value_provenance` rather than filled with a plausible-sounding
substitute.
