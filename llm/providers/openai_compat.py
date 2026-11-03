"""Any OpenAI-compatible endpoint: vLLM, Ollama, LM Studio, Together, Groq."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from llm.base import Capabilities


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, settings, profile: dict | None = None) -> None:
        self.settings = settings
        self.profile = profile or {}
        self.model = self.profile.get("model") or getattr(settings, "gemma_model", "local-model")
        self.base_url = (self.profile.get("base_url") or getattr(settings, "openai_base_url", "")).rstrip("/")
        self.api_key = self.profile.get("api_key") or getattr(settings, "openai_api_key", "")
        self.context_window = int(self.profile.get("context_window", 16384))

    # -- request shape ---------------------------------------------------
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def payload(self, messages, *, schema, temperature, max_tokens, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if schema and getattr(self.settings, "guided_json", True):
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "strict": False, "schema": schema}}
        return body

    def url(self, suffix: str) -> str:
        return f"{self.base_url}{suffix}"

    def capabilities(self) -> Capabilities:
        return Capabilities(context_window=self.context_window, json_mode=True, streaming=True)

    async def verify(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.url("/models"), headers=self.headers())
                resp.raise_for_status()
                ids = [m.get("id") for m in (resp.json().get("data") or [])]
            return {"resolved": self.model in ids or not ids, "model": self.model, "provider": self.name, "available": ids[:20]}
        except Exception as exc:
            return {"resolved": False, "model": self.model, "provider": self.name, "error": str(exc)}

    # -- calls -----------------------------------------------------------
    async def complete(self, messages, *, schema=None, temperature=0.2, max_tokens=1200) -> str:
        body = self.payload(messages, schema=schema, temperature=temperature, max_tokens=max_tokens, stream=False)
        async with httpx.AsyncClient(timeout=getattr(self.settings, "llm_timeout", 120.0)) as client:
            resp = await client.post(self.url("/chat/completions"), headers=self.headers(), json=body)
            resp.raise_for_status()
            data = resp.json()
        return (data["choices"][0]["message"].get("content") or "")

    async def stream(self, messages, *, schema=None, temperature=0.2, max_tokens=1200) -> AsyncIterator[str]:
        body = self.payload(messages, schema=schema, temperature=temperature, max_tokens=max_tokens, stream=True)
        timeout = httpx.Timeout(getattr(self.settings, "llm_timeout", 120.0), connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self.url("/chat/completions"), headers=self.headers(), json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:400]
                    raise RuntimeError(f"provider HTTP {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        return
                    try:
                        frame = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    for choice in frame.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield piece
