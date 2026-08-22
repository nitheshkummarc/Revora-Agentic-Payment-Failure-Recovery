# RecoverX

AI revenue recovery with a **deterministic diagnosis layer**, built for the
Razorpay AI Buildathon, Track 03.

> Razorpay's Subscription Recovery Agent decides whether to retry a failed
> payment. RecoverX traces the causal chain of *why* a batch of payments
> degraded, then decides — and it fails closed on RBI mandate rules, not just
> retry limits.

## Ground truth

[`GROUND_TRUTH.md`](GROUND_TRUTH.md) is authoritative for all schema, field
names, states, webhook event names, and RBI compliance rules. Its Appendix
lists the real Razorpay and RBI source documents. Where a value is not in that
document, this codebase does not invent one.

## Architecture

```
MockPaymentGateway (chaos injector)   <- Module 1
        v
StateMachine (deterministic)          <- Module 2
        v
FailurePropagationTracer              <- Module 3
        v
Intelligence Layer (LLM, JSON-only)   <- Module 4
        v
PolicyEngine (deterministic + RBI)    <- Module 5
        v
AgentOrchestrator                     <- Module 6
        v
X-Ray Dashboard                       <- Module 8
```

The LLM only ever sees the tracer's output, never raw events; its output only
ever reaches the gateway through the PolicyEngine.

## Build status

| Module | Component | Status |
|---|---|---|
| 1 | MockPaymentGateway + ChaosInjector | Built |
| 2 | StateMachine / StateResolver | Built |
| 3 | FailurePropagationTracer | Built |
| 4 | Intelligence Layer | Built |
| 5 | PolicyEngine | Not started |
| 6 | AgentOrchestrator | Not started |
| 7 | Synthetic dataset generator | Not started |
| 8 | X-Ray Dashboard | Not started |

## Running

```bash
pip install -r backend/requirements.txt
uvicorn app.main:app --reload --app-dir backend
pytest
```

## Scope notes

No real Razorpay API calls, no API keys, no database, no authentication —
everything is mocked in-process. See the global "do NOT build" list in
`RecoverX_Build_Prompts.md`.
# Revora
