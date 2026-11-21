"""The free OpenRouter tier shares an upstream quota, so HTTP 429 is routine: retry with backoff, then say so plainly."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm import client as llm_client
from llm.client import BUSY_MESSAGE, LLMClient


class Flaky:
    """Raises ``exc`` for the first ``fails`` calls, then streams two chunks."""

    def __init__(self, fails: int, exc: Exception) -> None:
        self.fails, self.exc, self.calls = fails, exc, 0

    async def stream(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        for chunk in ("a", "b"):
            yield chunk

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return "ok"


@pytest.fixture
def make(monkeypatch, tmp_path):
    (tmp_path / "models.yaml").write_text("profiles:\n  chat:\n    provider: mock\n    model: m1\n    fallbacks: []\n")
    monkeypatch.setattr(llm_client, "RATE_LIMIT_BACKOFF_S", 0.0)
    monkeypatch.setattr(llm_client, "RATE_LIMIT_RETRIES", 3)

    def build(fails: int, exc: Exception):
        provider = Flaky(fails, exc)
        monkeypatch.setattr(llm_client, "build_provider", lambda *a, **k: provider)
        return LLMClient(SimpleNamespace(models_yaml=str(tmp_path / "models.yaml")), "chat"), provider

    return build


async def _collect(client):
    return [chunk async for chunk in client.stream([{"role": "user", "content": "hi"}])]


async def test_stream_retries_429_then_succeeds(make):
    client, provider = make(2, RuntimeError("Client error '429 Too Many Requests'"))
    assert await _collect(client) == ["a", "b"]
    assert provider.calls == 3


async def test_stream_gives_a_plain_message_when_always_rate_limited(make):
    client, provider = make(99, RuntimeError("429 rate-limited upstream"))
    with pytest.raises(RuntimeError) as err:
        await _collect(client)
    assert str(err.value) == BUSY_MESSAGE
    assert provider.calls == llm_client.RATE_LIMIT_RETRIES + 1


async def test_other_errors_are_not_retried(make):
    client, provider = make(99, RuntimeError("500 upstream exploded"))
    with pytest.raises(RuntimeError, match="all model candidates failed"):
        await _collect(client)
    assert provider.calls == 1


async def test_complete_reports_rate_limits_plainly(make):
    client, _ = make(99, RuntimeError("429 slow down"))
    with pytest.raises(RuntimeError) as err:
        await client.complete([{"role": "user", "content": "hi"}])
    assert str(err.value) == BUSY_MESSAGE
