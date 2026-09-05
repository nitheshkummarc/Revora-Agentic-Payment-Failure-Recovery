"""Conformance test for the batch runner's model-client selection.

`data/run_batch.py` is standalone (like the dataset generator), so it is
loaded by path rather than imported as a package. The behaviour under test:
the selection order is Groq (+ Gemini fallback if both keys present), Gemini
alone, then the offline stub -- each gated on its own API key, with the stub
as the fallback for no key at all, never the default.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from app.intelligence.llm_client import (
    FallbackLLMClient,
    GeminiLLMClient,
    GroqLLMClient,
    StubLLMClient,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUN_BATCH_PATH = REPO_ROOT / "data" / "run_batch.py"

#: Every env var the selection order reads, cleared before each test so the
#: real environment (or a real .env file) can never leak into the result.
_MODEL_KEY_ENV_VARS = (
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


def _load_run_batch():
    spec = importlib.util.spec_from_file_location("run_batch", RUN_BATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_batch():
    return _load_run_batch()


@pytest.fixture(autouse=True)
def _clear_model_keys(monkeypatch):
    for name in _MODEL_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_selects_groq_with_gemini_fallback_when_both_keys_present(run_batch, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-not-real")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-real")
    client = run_batch._select_llm_client()
    assert isinstance(client, FallbackLLMClient)
    assert isinstance(client._primary, GroqLLMClient)
    assert isinstance(client._fallback, GeminiLLMClient)


def test_selects_groq_alone_when_only_groq_key_present(run_batch, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-not-real")
    client = run_batch._select_llm_client()
    assert isinstance(client, GroqLLMClient)


def test_selects_gemini_alone_when_only_gemini_key_present(run_batch, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-real")
    client = run_batch._select_llm_client()
    assert isinstance(client, GeminiLLMClient)


def test_google_api_key_is_also_recognised_for_gemini(run_batch, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-real")
    client = run_batch._select_llm_client()
    assert isinstance(client, GeminiLLMClient)


def test_groq_takes_priority_over_gemini(run_batch, monkeypatch):
    """Order, not merely availability: with both keys set the primary must be
    Groq, so a demo run starts on the provider it can stay on rather than
    falling through Gemini's request quota mid-batch."""
    monkeypatch.setenv("GROQ_API_KEY", "test-not-real")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-real")
    client = run_batch._select_llm_client()
    assert isinstance(client, FallbackLLMClient)
    assert isinstance(client._primary, GroqLLMClient)


def test_falls_back_to_stub_client_when_no_key_present(run_batch):
    client = run_batch._select_llm_client()
    assert isinstance(client, StubLLMClient)


def test_force_stub_overrides_any_key_present(run_batch, monkeypatch):
    """Adding a real key to the environment must not silently change what
    `python data/run_batch.py --stub` measures -- the documented deterministic
    baseline has to stay reproducible on demand, key or no key."""
    monkeypatch.setenv("GROQ_API_KEY", "test-not-real")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-real")
    client = run_batch._select_llm_client(force_stub=True)
    assert isinstance(client, StubLLMClient)


def test_seed_and_run_twice_in_one_process_is_reproducible(run_batch, monkeypatch, tmp_path):
    """seed_and_run() constructs its own gateway per call rather than reaching
    into any process-wide singleton, so two calls in the same process must not
    interfere with each other's RNG stream or circuit-breaker state.

    seed_and_run() persists to the module-level RESULTS_PATH on every call --
    that is its real job when run as a script, but here it pointed at the real
    data/batch_results.json (the file the dashboard reads), so running this
    test silently rewrote the repo's committed batch data as a side effect.
    Redirected to a throwaway path so the test can't touch real repo state."""
    import json

    monkeypatch.setattr(run_batch, "RESULTS_PATH", tmp_path / "batch_results.json")
    dataset = json.loads(run_batch.DATASET_PATH.read_text(encoding="utf-8"))

    first = run_batch.seed_and_run(dataset, failure_rate=0.05)
    second = run_batch.seed_and_run(dataset, failure_rate=0.05)

    first_outcomes = [(e.payment_id, e.outcome.value) for e in first.events]
    second_outcomes = [(e.payment_id, e.outcome.value) for e in second.events]
    assert first_outcomes == second_outcomes
