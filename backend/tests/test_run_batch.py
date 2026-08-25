"""Conformance test for the batch runner's model-client selection.

`data/run_batch.py` is standalone (like the dataset generator), so it is
loaded by path rather than imported as a package. The behaviour under test:
a present `ANTHROPIC_API_KEY` must select the real client, not the stub --
the stub is a fallback for a missing key, not the default.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from app.intelligence.llm_client import AnthropicLLMClient, StubLLMClient

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUN_BATCH_PATH = REPO_ROOT / "data" / "run_batch.py"


def _load_run_batch():
    spec = importlib.util.spec_from_file_location("run_batch", RUN_BATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_batch():
    return _load_run_batch()


def test_selects_anthropic_client_when_key_present(run_batch, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    client = run_batch._select_llm_client()
    assert isinstance(client, AnthropicLLMClient)


def test_falls_back_to_stub_client_when_key_absent(run_batch, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = run_batch._select_llm_client()
    assert isinstance(client, StubLLMClient)
