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

| Component | State | Tests |
|---|---|---|
| Mock payment gateway + fault injection | Built | 26 |
| State resolver | Built | 28 |
| Failure propagation tracer | Built | 22 |
| Recommendation layer | Built | 43 |
| Policy engine | Built | 50 |
| Orchestrator + verify | Not started | — |
| Synthetic dataset generator | Not started | — |
| Dashboard | Not started | — |

## Running

```bash
pip install -r backend/requirements.txt
uvicorn app.main:app --reload --app-dir backend
pytest
```

Set `ANTHROPIC_API_KEY` to enable live recommendation calls. Without it the
recommendation layer runs against a deterministic stub, and every other layer
is unaffected.

## Scope

No real Razorpay API calls, no API keys in the repo, no card data, no database,
no authentication. Everything runs in a single process with in-memory state.
Time-dependent behaviour uses an injectable clock, so a documented 45-second
webhook delay is exercised instantly in tests and at real speed in a demo.
