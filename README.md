<h1 align="center">REVORA</h1>

<p align="center">
  <strong>Agentic Payment Failure Recovery</strong><br>
  Decides whether a failed payment can be retried <em>safely</em> and <em>legally</em>, and refuses when it can't.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11+-3776AB">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-Pydantic%20v2-009688">
  <img alt="React" src="https://img.shields.io/badge/React%2019-TypeScript%20%2B%20Tailwind-61DAFB">
  <img alt="LLM" src="https://img.shields.io/badge/LLM-Groq%20%2B%20Gemini%20fallback-8A2BE2">
  <img alt="Tests" src="https://img.shields.io/badge/tests-474%20passing-2EA043">
</p>

Built solo in 15 days for Razorpay's AI Buildathon (Track 03, AI Revenue Recovery).

---

## The problem

Automatic retries are the standard way to recover failed payments. Done naively, they hurt customers:

- **Webhooks are unreliable.** They arrive late, twice, out of order, or never, so the merchant's view of a payment is often stale.
- **"Failed" isn't final.** Razorpay documents that a failed payment can turn *authorized* minutes later. A bot that retries it charges the customer **twice**.
- **Retries are regulated.** RBI's e-mandate rules require a 24-hour pre-debit notice, a mandate ceiling, extra authentication above ₹15,000, and honouring opt-outs. A bot that ignores them puts the merchant in breach.

## What Revora does

It splits "should I retry this?" into four questions and gives each to the right tool:

| Question | Answered by | How |
|---|---|---|
| What state is this payment really in? | State resolver | Deterministic code |
| Why did it fail? | Failure tracer | Deterministic code |
| What should we do about it? | **LLM** | A recommendation, never a command |
| Are we allowed to? | RBI policy engine | Deterministic code, final say |

Then it acts like a careful operator:

- **Checks before acting:** reads the payment's real status before any write, and stops if it's already paid.
- **Acts safely:** a retry is one idempotent call that creates a new payment attempt, exactly how a real Razorpay retry works.
- **Verifies after acting:** nothing counts as recovered until the charged payment reads back as captured.
- **Hands uncertain cases to a person** with the reason, and records *why* behind every decision.

## The agent

The model doesn't answer from a single prompt. It investigates first, the way an operator would, then proposes.

- **Four read-only tools:** check the payment's real status at the gateway, read the mandate and compliance facts, read the retry history, and ask the RBI rules whether an action would pass. **No tool can move money.**
- **Two phases:** the model calls tools until it has what it needs (at most five lookups), then gives its proposal through strict JSON-schema output.
- **The proposal is still only a proposal.** The same injection and note-influence guards and the same RBI rule engine apply, and Execute checks the gateway again before any write.
- **No framework.** The loop is about 250 lines on Groq's native tool calling, with no LangChain or LangGraph, so every tool, limit and fallback is explicit and tested.
- **Works offline too.** A scripted agent runs the same loop without a key, for tests and the website's recorded runs.

On a live run, the real model (Groq, `gpt-oss-120b`) checked a payment the webhooks called failed, found it already **captured**, and chose to verify instead of retrying. The earlier single-call pipeline could only hold that payment for review.

## Results at a glance

500 simulated failed-payment incidents across 29 scenarios, replayed end to end against a fault-injecting mock gateway.

| | Revora | Naive "always retry" |
|---|---|---|
| Customers charged twice | **0** | **54** (₹1,67,046) |
| Retries that break RBI rules | **0** | 63 |
| Prompt-injection attempts that moved money | **0** of 31 | n/a |

- **Every scenario routed to its designed outcome:** 316 recovered, 63 blocked by policy, 79 escalated, 4 held for review, 38 needed no action.
- **10 of 12 policy rules fired** on real rows; the other two are explained, not hidden.
- **Against a real LLM (Groq):** 14 of 14 injection attacks ended safely; the model refused about 7 of 11 on its own, and deterministic checks caught the rest.
- **70 of 500 decisions needed no LLM call at all**, because the evidence was ambiguous and a status check answers that better.
- **Reproducible:** two runs give byte-identical outcomes.

> **Honest scope.** This is a simulation, so the headline run uses an offline stub model, and a mock retry always succeeds. That's why the money metric is called a **Correctly Routed Rate (61.0%)**, not a recovery rate: it shows the system routes and protects correctly, not how much real money it would recover. Full detail: [`docs/reference.md`](docs/reference.md).

## Architecture

```
Mock payment gateway (fault injection: delay, duplicate, reorder, drop, Failed→Authorized flip)
        │  only the webhooks a merchant would actually receive
        ▼
State resolver ──────────────► what state is it in?        [deterministic]
        ▼
Failure tracer ──────────────► why did it fail?            [deterministic]
        │  ambiguous evidence? → status check, LLM not called
        ▼
Planning agent ──────────────► what should we do?          [Groq → Gemini fallback]
        │  read-only tools: payment status, mandate facts, retry history, rule dry run
        ▼
Injection guard · note-influence check · grounding guard    [deterministic]
        ▼
RBI policy engine ───────────► are we allowed to?          [deterministic, final say]
        ▼
Execute (status check first, idempotent retry) → Verify (read back) → web app
```

## Key engineering decisions

- **The LLM answers one question out of four.** State and root cause can be proven from evidence, and compliance is written in a regulation. Only "what should we do" is a judgement call, so that's the only place a model gets a say.
- **Trust boundaries are enforced by schemas, not comments.** Every module's input is a Pydantic model that rejects unknown fields. The resolver's input has no field for gateway truth, and the LLM's input has no field for raw webhooks, so neither can leak in even by accident.
- **No evidence, no guess.** An ambiguous trace never reaches the model; it triggers a status query instead. This is proven by a test client that crashes if it's ever called. It's the biggest safety win and the biggest LLM cost saving.
- **The model proposes; code disposes.** After the model answers:
  - a flagged injection note always goes to a person;
  - a retry the customer's note talked the model into (it changes when the note is removed) goes to a person;
  - a money-moving action with no real error behind it goes to a person.
- **Fail closed on missing compliance data.** A missing RBI field blocks the action and names the field. Absence is checked before any value, so missing data can never pass as compliant.
- **Never trust a stale view before moving money.** Every retry starts with a live status check. Retries are new payment attempts under idempotency keys, so a duplicated request can't charge twice.
- **Regulatory figures are verified, not paraphrased.** Every RBI threshold was checked against the circular's text (RBI/DPSS/2026-27/396), including the exact ₹15,000 boundary. Business limits don't cite the circular, and the RBI rules can't be switched off.
- **The simulator keeps two truths apart:** the payment's real state, and what the merchant can see from delivered webhooks. That makes "the evidence is wrong" a testable scenario.
- **Honest metrics over flattering ones.** The headline metric was renamed after adding five high-value rows moved it 16 points with no decision changing. It measures routing, not recovery.
- **Every decision explains itself:** the rules evaluated, the rule that fired, which model answered, what it wanted, and why a check overrode it. Every case handed to a person carries its reason in the run's review list.
- **Deterministic and reproducible:** a seeded dataset, a fixed reference time and an injectable clock (no `sleep()`), so a 45-second webhook delay runs instantly in tests.
- **The agent gets eyes, not hands.** Its tools only read. It proposes, and the rule engine and executor decide, so a manipulated or mistaken model still can't move money on its own.
- **Deliberately simple:** no LangChain or LangGraph (the agent loop is short and every step needs to be explicit), no database or queue (scoped out for a 15-day build). Each is a decision with a stated reason, not a gap.

## Safety, proven by tests

- No retry writes to a payment that's already paid, and this doesn't rely on the mock rejecting it.
- An invalid or garbled LLM response escalates. This happened live once and failed safe.
- Personal data (email, UPI, phone, card, PAN numbers) is redacted from customer notes before any LLM provider sees them.
- Double captures, illegal state transitions and duplicate refunds are refused.
- Root causes are quoted from Razorpay's error object, never invented. With no error object, the cause is "undetermined" and the trace is ambiguous.

## Tech stack

- **Backend:** Python 3.11+, FastAPI, Pydantic v2
- **LLM:** Groq (`openai/gpt-oss-120b`) with Gemini fallback, schema-enforced JSON output, a circuit breaker, and an offline stub for reproducible runs
- **Frontend (`web/`):** React 19, TypeScript, Tailwind CSS v4, Vite, with a live agent console
- **Testing:** pytest (460), Vitest (14), plus 18 live LLM tests
- **Hosting:** Render for the API (`render.yaml`), Vercel for the web app

## Known limitations

- Runs on a mock gateway and synthetic data, with no real Razorpay integration.
- Mock retries always succeed, so there's no real recovery rate.
- Live LLM evaluation is small (14 injection attacks and 4 reasoning cases), and there's no labelled accuracy benchmark.
- No database, authentication or job queue yet. Results are one JSON file.
- An authorized-but-uncaptured payment is treated as a success.
- The agent's waiting is not modelled yet: it decides once per payment and cannot schedule a retry for later.
- The public demo runs on a free server that sleeps when idle, and live runs are rate-limited.

## Quick start

```bash
# Backend
pip install -r backend/requirements-dev.txt
python -m pytest -q --ignore=backend/tests/test_intelligence_live_api.py   # 460 passed
uvicorn app.main:app --reload --app-dir backend                            # http://localhost:8000

# Regenerate the batch run and the web app's bundled data (offline, no API key needed)
python data/run_batch.py --stub
python data/export_web_data.py

# Frontend (set VITE_API_URL in web/.env to enable live agent runs)
cd web && npm install && npm run dev                                       # http://localhost:5173
```

No API key is needed. Without one, the LLM step uses the offline stub and the web app shows recorded agent runs. To use a real model, copy `.env.example` to `.env` and add a Groq or Gemini key. With a key, `run_batch.py` without `--stub` makes real API calls.

## Deployment

- **API on Render:** create a Blueprint from this repo; `render.yaml` defines the service. Enter `GROQ_API_KEY` and `REVORA_CORS_ORIGINS` (the Vercel URL) when asked. On the public server the mock gateway's write routes are switched off, and live agent runs are rate-limited.
- **Web app on Vercel:** import the repo, set the root directory to `web`, and add `VITE_API_URL` (the Render URL). Without it the site still works with its recorded runs.

## Project structure

```
backend/app/gateway/        mock payment gateway + fault injection
backend/app/state_machine/  real state from delivered webhooks
backend/app/tracer/         root cause, causal chain, ambiguity
backend/app/intelligence/   sanitiser, prompts, LLM clients, the agent loop, safety checks
backend/app/policy/         RBI rules + the engine that applies them
backend/app/orchestrator/   observe → plan → validate → execute → verify, and the agent's read-only tools
backend/app/demo/           agent demo API: replays one scenario on a fresh gateway
data/                       dataset generator, batch runner, baseline, web data export, results
web/                        frontend: public web app with the agent console (Tailwind)
docs/                       architecture, methodology, full technical reference
```

## Learn more

- [`docs/architecture.md`](docs/architecture.md): what each module owns, how the boundaries are enforced, and failure behaviour
- [`docs/methodology.md`](docs/methodology.md): how the dataset and evaluation were built, and what they can and can't show
- [`docs/reference.md`](docs/reference.md): every result and caveat, the agent scorecard, the naive baseline, API routes, configuration and scope
