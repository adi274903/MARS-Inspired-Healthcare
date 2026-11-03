"""OpenRouter: OpenAI-compatible + attribution headers, provider routing, model catalogue."""
from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from llm.providers.openai_compat import OpenAICompatProvider


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"

    def __init__(self, settings, profile: dict | None = None) -> None:
        super().__init__(settings, profile)
        self.base_url = (self.profile.get("base_url") or getattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")).rstrip("/")
        self.api_key = self.profile.get("api_key") or getattr(settings, "openrouter_api_key", "")
        self.model = self.profile.get("model") or getattr(settings, "gemma_model", "google/gemma-4-26b-a4b-it")
        self.context_window = int(self.profile.get("context_window", 16384))

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.profile.get("referer", "http://localhost:8080"),
            "X-Title": self.profile.get("title", "EvalBot"),
        }

    def payload(self, messages, *, schema, temperature, max_tokens, stream: bool) -> dict[str, Any]:
        body = super().payload(messages, schema=schema, temperature=temperature, max_tokens=max_tokens, stream=stream)
        # Only route to providers that honour the request parameters and do not train on data.
        body["provider"] = {"data_collection": "deny", "require_parameters": bool(schema), "sort": "throughput"}
        return body

    async def verify(self) -> dict[str, Any]:
        if not self.api_key:
            return {"resolved": False, "model": self.model, "provider": self.name, "error": "OPENROUTER_API_KEY not set"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self.url("/models"), headers=self.headers())
                resp.raise_for_status()
                catalogue = resp.json().get("data") or []
        except Exception as exc:
            return {"resolved": False, "model": self.model, "provider": self.name, "error": str(exc)}
        entry = next((m for m in catalogue if m.get("id") == self.model), None)
        gemma = sorted(m.get("id") for m in catalogue if "gemma" in str(m.get("id", "")).lower())
        if entry is None:
            return {"resolved": False, "model": self.model, "provider": self.name, "available_gemma": gemma[:20],
                    "error": f"model {self.model!r} not in catalogue"}
        pricing = entry.get("pricing") or {}
        return {
            "resolved": True,
            "model": self.model,
            "provider": self.name,
            "context_window": entry.get("context_length"),
            "price_per_1m_in": _per_million(pricing.get("prompt")),
            "price_per_1m_out": _per_million(pricing.get("completion")),
        }

    async def stream(self, messages, *, schema=None, temperature=0.2, max_tokens=1200) -> AsyncIterator[str]:
        async for piece in super().stream(messages, schema=schema, temperature=temperature, max_tokens=max_tokens):
            yield piece


def _per_million(value: Any) -> float | None:
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None
