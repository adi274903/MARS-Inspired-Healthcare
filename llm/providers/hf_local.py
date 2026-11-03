"""In-process transformers fallback (small Gemma on a modest GPU/CPU)."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from llm.base import Capabilities


class HFLocalProvider:
    name = "hf_local"

    def __init__(self, settings, profile: dict | None = None) -> None:
        self.settings = settings
        self.profile = profile or {}
        self.model_name = self.profile.get("model") or "google/gemma-3-4b-it"
        self._pipe = None

    def _load(self):
        if self._pipe is None:
            from transformers import pipeline

            self._pipe = pipeline(
                "text-generation",
                model=self.model_name,
                device_map="auto",
                torch_dtype="auto",
                max_new_tokens=int(self.profile.get("max_tokens", 800)),
            )
        return self._pipe

    def capabilities(self) -> Capabilities:
        return Capabilities(context_window=8192, json_mode=False, streaming=True)

    async def verify(self) -> dict[str, Any]:
        try:
            self._load()
            return {"resolved": True, "model": self.model_name, "provider": self.name}
        except Exception as exc:
            return {"resolved": False, "model": self.model_name, "provider": self.name, "error": str(exc)}

    def _render(self, messages: list[dict[str, str]]) -> str:
        return "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages) + "\n\nASSISTANT:"

    def _run(self, messages, temperature, max_tokens) -> str:
        pipe = self._load()
        out = pipe(self._render(messages), do_sample=temperature > 0, temperature=max(temperature, 1e-5), max_new_tokens=max_tokens)
        text = out[0]["generated_text"]
        return text.split("ASSISTANT:", 1)[-1].strip()

    async def complete(self, messages, *, schema=None, temperature=0.2, max_tokens=1200) -> str:
        return await asyncio.to_thread(self._run, messages, temperature, max_tokens)

    async def stream(self, messages, *, schema=None, temperature=0.2, max_tokens=1200) -> AsyncIterator[str]:
        text = await self.complete(messages, schema=schema, temperature=temperature, max_tokens=max_tokens)
        for start in range(0, len(text), 24):
            await asyncio.sleep(0)
            yield text[start:start + 24]
