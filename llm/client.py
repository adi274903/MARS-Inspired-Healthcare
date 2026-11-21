"""Shared client: profile resolution, retry/backoff, fallbacks, streaming."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from core import registry
from core.telemetry import log
from llm.providers import build_provider


class StreamInterrupted(RuntimeError):
    def __init__(self, message: str, partial: str = "") -> None:
        super().__init__(message)
        self.partial = partial


@dataclass
class ModelStatus:
    profile: str
    provider: str
    model: str
    resolved: bool
    detail: dict[str, Any]


#: The free OpenRouter tier shares one upstream quota pool, so HTTP 429 is routine: retry with backoff before failing.
RATE_LIMIT_RETRIES = int(os.environ.get("LLM_RATE_LIMIT_RETRIES", "3"))
RATE_LIMIT_BACKOFF_S = float(os.environ.get("LLM_RATE_LIMIT_BACKOFF_S", "2.0"))
BUSY_MESSAGE = "The free language model is rate-limited right now (shared upstream quota). Please try again in a minute."


def is_rate_limited(exc: BaseException) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or "429" in str(exc)


class LLMClient:
    """One place that knows how to reach a model profile, with fallbacks."""

    def __init__(self, settings, profile_name: str = "chat", *, profiles_path: Path | None = None) -> None:
        self.settings = settings
        self.profile_name = profile_name
        self.profiles_path = Path(profiles_path or settings.models_yaml)
        self._profile: dict[str, Any] | None = None
        self._provider = None
        self.status: ModelStatus | None = None

    # -- profile lifecycle ----------------------------------------------
    def profile(self) -> dict[str, Any]:
        if self._profile is None:
            try:
                self._profile = registry.get_profile(self.profiles_path, self.profile_name)
            except KeyError:
                self._profile = dict(registry.DEFAULT_PROFILES.get(self.profile_name, registry.DEFAULT_PROFILES["chat"]))
        return self._profile

    def reload(self) -> dict[str, Any]:
        self._profile = None
        self._provider = None
        return self.profile()

    def provider(self):
        if self._provider is None:
            profile = self.profile()
            self._provider = build_provider(profile.get("provider", "mock"), self.settings, profile)
        return self._provider

    def candidates(self) -> list[dict[str, Any]]:
        profile = dict(self.profile())
        out = [profile]
        for model in profile.get("fallbacks") or []:
            out.append({**profile, "model": model})
        return out

    async def verify(self) -> ModelStatus:
        detail: dict[str, Any] = {}
        resolved = False
        for candidate in self.candidates():
            provider = build_provider(candidate.get("provider", "mock"), self.settings, candidate)
            try:
                detail = await provider.verify()
            except Exception as exc:
                detail = {"resolved": False, "error": str(exc), "model": candidate.get("model")}
            if detail.get("resolved"):
                resolved = True
                break
        profile = self.profile()
        self.status = ModelStatus(self.profile_name, profile.get("provider", "mock"), detail.get("model", profile.get("model", "")), resolved, detail)
        return self.status

    # -- generation ------------------------------------------------------
    async def complete(self, messages: list[dict[str, str]], *, schema: dict | None = None) -> str:
        errors: list[str] = []
        for candidate in self.candidates():
            provider = build_provider(candidate.get("provider", "mock"), self.settings, candidate)
            for attempt in range(2):
                try:
                    return await provider.complete(
                        messages,
                        schema=schema,
                        temperature=float(candidate.get("temperature", 0.2)),
                        max_tokens=int(candidate.get("max_tokens", 1200)),
                    )
                except Exception as exc:
                    errors.append(f"{candidate.get('model')}#{attempt}: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(RATE_LIMIT_BACKOFF_S * (attempt + 1) if is_rate_limited(exc) else 0.2 * (attempt + 1))
        if errors and all(is_rate_limited(RuntimeError(e)) for e in errors):
            raise RuntimeError(BUSY_MESSAGE)
        raise RuntimeError("all model candidates failed: " + "; ".join(errors[-4:]))

    async def stream(self, messages: list[dict[str, str]], *, schema: dict | None = None) -> AsyncIterator[str]:
        """Stream raw provider chunks. Falls back only *before* the first chunk."""
        errors: list[str] = []
        for candidate in self.candidates():
            provider = build_provider(candidate.get("provider", "mock"), self.settings, candidate)
            started = False
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    async for chunk in provider.stream(
                        messages,
                        schema=schema,
                        temperature=float(candidate.get("temperature", 0.2)),
                        max_tokens=int(candidate.get("max_tokens", 1200)),
                    ):
                        started = True
                        yield chunk
                    return
                except Exception as exc:
                    errors.append(f"{candidate.get('model')}: {type(exc).__name__}: {exc}")
                    log("llm.client", "stream.error", model=candidate.get("model"), error=str(exc), started=started,
                        attempt=attempt)
                    if started:
                        raise StreamInterrupted(str(exc)) from exc
                    if is_rate_limited(exc) and attempt < RATE_LIMIT_RETRIES:
                        await asyncio.sleep(RATE_LIMIT_BACKOFF_S * (2 ** attempt))  # 2 s, 4 s, 8 s
                        continue
                    await asyncio.sleep(0.2)
                    break
        if errors and all(is_rate_limited(RuntimeError(e)) for e in errors):
            raise RuntimeError(BUSY_MESSAGE)
        raise RuntimeError("all model candidates failed: " + "; ".join(errors[-4:]))
