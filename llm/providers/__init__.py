from llm.providers.hf_local import HFLocalProvider
from llm.providers.mock import MockProvider
from llm.providers.openai_compat import OpenAICompatProvider
from llm.providers.openrouter import OpenRouterProvider

REGISTRY = {
    "openrouter": OpenRouterProvider,
    "openai_compat": OpenAICompatProvider,
    "vllm": OpenAICompatProvider,
    "hf_local": HFLocalProvider,
    "mock": MockProvider,
}


def build_provider(name: str, settings, profile: dict | None = None):
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown LLM_PROVIDER {name!r}; have {sorted(REGISTRY)}") from None
    return cls(settings, profile or {})


__all__ = ["REGISTRY", "build_provider"]
