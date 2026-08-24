# Revora

Payment-failure root-cause analysis and recovery decisioning, with a
deterministic diagnosis layer between the evidence and the model.

> Most recovery agents decide *whether* to retry a failed payment. Revora
> traces the causal chain of *why* a batch of payments degraded, then decides —
> and it fails closed on RBI mandate rules, not just retry limits.

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
  a silently dropped webhook reads as silence — which, past a 5-minute
  threshold, means "check the status", not "assume it failed".
- **A gap in the event chain is reported, never smoothed over.** Missing
  telemetry sets `ambiguous: true` rather than returning a shorter chain that
  looks complete.
- **Root causes are quoted, not paraphrased.** The tracer emits the literal
  `source`, `step` and `reason` from the event's own error object, so any cause
  can be grepped back to the event that produced it.
- **An ambiguous trace never reaches the model.** It short-circuits to a status
  check, so the model is never asked to guess in place of missing data.
- **Compliance fails closed.** A missing pre-debit notice, mandate ceiling or
  AFA flag blocks the action. Absence is checked before any value comparison,
  so an absent field can never be read as a passing value.

## Status

All eight components are built.

| Component | Tests |
|---|---|
| Mock payment gateway + fault injection | 26 |
| State resolver | 28 |
| Failure propagation tracer | 22 |
| Recommendation layer | 43 |
| Policy engine | 64 |
| Orchestrator + verify | 17 |
| API / CORS scoping | 9 |
| Synthetic dataset generator | 53 |
| X-Ray dashboard | 64 |

**262 backend + 64 frontend = 326 tests passing**, plus 16 that are skipped by
default — see below.

### What is not covered

A further 16 tests exercise prompt injection against the **real** model rather
than the offline stub. They skip unless `ANTHROPIC_API_KEY` is set, and they
skip loudly: a green suite without a key is not evidence they passed.

```bash
ANTHROPIC_API_KEY=... python -m pytest backend/tests/test_intelligence_live_api.py -v
```

Until that runs, every claim about injection resistance rests on the
deterministic guard — which is tested against a stub rigged to comply with each
injection, so it is a real guarantee. It is not the same claim as "the model
resists injection", and the two are worth keeping apart.

## Results

A 500-event synthetic batch, replayed end to end. Two independent runs produce
byte-identical per-event outcomes.

| Outcome | Count | Value |
|---|---|---|
| Recovered | 325 | ₹20,40,275 |
| Blocked by policy | 63 | ₹10,02,892 |
| Escalated | 81 | ₹2,83,719 |
| Needs review | 0 | ₹0 |
| No action needed | 31 | ₹68,219 |
| **Total** | **500** | **₹33,95,105** |

All 10 policy rules fired. 31 rows carried a prompt-injection attempt; all 31
escalated and none executed. No stage passed a degraded result forward — hence
zero `needs_review`.

**On the headline number.** The dashboard reports a *Correctly Routed Rate*
(61.3% of addressable value), deliberately not a recovery rate. A retry always
succeeds against the mock gateway, so the figure measures whether each payment
reached its correct decision — not how often a real retry would land. Adding
five high-value rows to the dataset once moved it 16 percentage points without
changing a single decision, which is the clearest demonstration that it is
arithmetic over a synthetic distribution rather than a measurement.

What the run does demonstrate: correct routing, every guardrail firing, and
fail-closed behaviour on missing compliance fields.

## Running

### Backend

```bash
pip install -r backend/requirements.txt
python -m pytest -q                                # 262 passed, 16 skipped
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
npm test              # 64 offline tests, no servers needed
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
data/generate_synthetic_dataset.py   standalone; imports nothing from backend/
data/run_batch.py                    replays the dataset through the orchestrator
frontend/src/                react dashboard, read-only
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables live model calls. Without it the recommendation layer uses a deterministic stub and every other layer is unaffected. |
| `REVORA_CORS_ORIGINS` | the four localhost dev/preview origins | Comma-separated origins allowed to read the API. Scoped rather than `*`, since nothing here needs to be readable by any page on the internet. |

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
