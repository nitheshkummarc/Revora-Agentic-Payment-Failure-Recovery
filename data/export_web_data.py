"""Export the data the public site bundles: `web/src/data/site.json`.

Contents:

* one scenario per dataset scenario, each with a scripted agent run made by
  the real pipeline (`app.demo.service.run_scenario`), so the site's agent
  console works instantly and offline;
* the headline figures of the committed 500-row run (`data/batch_results.json`);
* the naive-baseline comparison (`data/baseline_comparison.json`).

Run after regenerating either results file:

    python data/export_web_data.py
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "data"))

from app.demo.service import list_scenarios, run_scenario  # noqa: E402
from app.orchestrator.schemas import BatchResults  # noqa: E402

import run_batch  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "web" / "src" / "data" / "site.json"

#: Duplicate-charge counters for the naive policy. `run_baseline.py` prints the
#: first; the second was measured by instrumenting the same run and is recorded
#: in docs/reference.md's baseline table. Neither is stored in
#: baseline_comparison.json, which is unchanged since they were measured.
NAIVE_RETRIES_ON_PAID_PAYMENTS = 58
NAIVE_CHARGED_TWICE = 54
NAIVE_CHARGED_TWICE_PAISE = 16_704_600


def _metrics() -> dict:
    results = BatchResults.model_validate_json(
        (REPO_ROOT / "data" / "batch_results.json").read_text(encoding="utf-8")
    )
    events = results.events
    money = run_batch.money_figures(events)
    rules = Counter(e.rule_id for e in events if e.rule_id)
    return {
        "batch_run_id": results.batch_run_id,
        "total_events": results.summary.total_events,
        "outcomes": {
            "recovered": results.summary.recovered,
            "blocked": results.summary.blocked,
            "escalated": results.summary.escalated,
            "needs_review": results.summary.needs_review,
            "no_action": results.summary.no_action,
        },
        "money_paise": {k: v for k, v in money.items() if k != "by_outcome"},
        "outcome_paise": money["by_outcome"],
        "rules_fired": dict(sorted(rules.items(), key=lambda kv: -kv[1])),
        "injection_attempts": sum(1 for e in events if e.injection_patterns_flagged),
        "injection_attempts_that_moved_money": sum(
            1
            for e in events
            if e.injection_patterns_flagged
            and e.execution is not None
            and e.execution.charged_payment_id is not None
        ),
        "model_consultations": sum(1 for e in events if e.llm_called),
        "decided_without_model": sum(1 for e in events if e.ambiguous),
        "guard_overrides": sum(1 for e in events if e.original_llm_action is not None),
    }


def _baseline() -> dict:
    payload = json.loads((REPO_ROOT / "data" / "baseline_comparison.json").read_text(encoding="utf-8"))
    revora = payload["policies"]["Revora"]
    naive = payload["policies"]["naive always-retry"]
    return {
        "revora": {"counts": revora["counts"], "amounts_paise": revora["amounts_paise"]},
        "naive": {"counts": naive["counts"], "amounts_paise": naive["amounts_paise"]},
        "naive_retries_on_paid_payments": NAIVE_RETRIES_ON_PAID_PAYMENTS,
        "naive_charged_twice": NAIVE_CHARGED_TWICE,
        "naive_charged_twice_paise": NAIVE_CHARGED_TWICE_PAISE,
        "revora_charged_twice": 0,
    }


def main() -> None:
    logging.disable(logging.CRITICAL)
    scenarios = []
    for scenario in list_scenarios():
        run = run_scenario(scenario.payment_id)
        scenarios.append(json.loads(run.model_dump_json()))
    site = {"metrics": _metrics(), "baseline": _baseline(), "scenarios": scenarios}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(site, indent=1), encoding="utf-8")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}: {len(scenarios)} scenarios")


if __name__ == "__main__":
    main()
