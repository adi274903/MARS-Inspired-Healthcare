"""Provider protocol + capability record."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


@dataclass
class Capabilities:
    context_window: int = 8192
    json_mode: bool = False
    streaming: bool = True
    price_per_1m_in: float | None = None
    price_per_1m_out: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> str: ...

    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> AsyncIterator[str]: ...

    async def verify(self) -> dict[str, Any]:
        """Confirm the configured model resolves; return resolution details."""
        ...
