"""Settings. Everything is env-driven; nothing reads the dataset at runtime."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(key)
    if not raw:
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


@dataclass
class Settings:
    # data
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH") or ROOT / "data" / "papers.db"))
    jsonl_path: Path = field(default_factory=lambda: Path(_env("JSONL_PATH") or ROOT / "data" / "papers.jsonl"))
    qdrant_path: Path = field(default_factory=lambda: Path(_env("QDRANT_PATH") or ROOT / "data" / "qdrant"))
    events_db: Path = field(default_factory=lambda: Path(_env("EVENTS_DB") or ROOT / "events" / "events.db"))
    qdrant_collection: str = field(default_factory=lambda: _env("QDRANT_COLLECTION", "evalbot_papers"))

    # bus
    bus_kind: str = field(default_factory=lambda: _env("BUS", "memory"))  # memory | redis
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    events_stream: str = field(default_factory=lambda: _env("EVENTS_STREAM", "evalbot.events"))

    # retrieval
    num_evidence: int = field(default_factory=lambda: _env_int("NUM_EVIDENCE", 8))
    max_abstract_chars: int = field(default_factory=lambda: _env_int("MAX_ABSTRACT_CHARS", 900))
    rrf_k: int = field(default_factory=lambda: _env_int("RRF_K", 60))
    vector_enabled: bool = field(default_factory=lambda: _env_bool("VECTOR_ENABLED", True))
    graph_enabled: bool = field(default_factory=lambda: _env_bool("GRAPH_ENABLED", True))
    sqlite_enabled: bool = field(default_factory=lambda: _env_bool("SQLITE_ENABLED", True))
    embed_provider: str = field(default_factory=lambda: _env("EMBED_PROVIDER", "hash"))  # hash | local | none
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "google/embeddinggemma-2"))
    embed_dim: int = field(default_factory=lambda: _env_int("EMBED_DIM", 768))  # Matryoshka: <= model dim
    embed_device: str = field(default_factory=lambda: _env("EMBED_DEVICE", "cpu"))
    embed_batch: int = field(default_factory=lambda: _env_int("EMBED_BATCH", 32))
    neo4j_uri: str = field(default_factory=lambda: _env("NEO4J_URI"))
    neo4j_user: str = field(default_factory=lambda: _env("NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: _env("NEO4J_PASSWORD", ""))
    router: str = field(default_factory=lambda: _env("ROUTER", "rules"))  # rules | llm

    # llm + model profiles
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "mock"))
    models_yaml: Path = field(default_factory=lambda: Path(_env("MODELS_YAML") or ROOT / "models.yaml"))
    models_db: Path = field(default_factory=lambda: Path(_env("MODELS_DB") or ROOT / "data" / "models.db"))
    guidance_path: Path = field(default_factory=lambda: Path(_env("GUIDANCE_PATH") or ROOT / "guidance" / "sources.yaml"))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    openrouter_base_url: str = field(default_factory=lambda: _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "http://localhost:8001/v1"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", "sk-local"))
    gemma_model: str = field(default_factory=lambda: _env("GEMMA_MODEL", "google/gemma-4-26b-a4b-it"))
    might: int = 0  # placeholder keeps dataclass field ordering stable for older pickles
    temperature: float = field(default_factory=lambda: _env_float("TEMPERATURE", 0.2))
    max_tokens: int = field(default_factory=lambda: _env_int("MAX_TOKENS", 1200))
    llm_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 120.0))
    guided_json: bool = field(default_factory=lambda: _env_bool("GUIDED_JSON", True))
    token_batch_chars: int = field(default_factory=lambda: _env_int("TOKEN_BATCH_CHARS", 24))
    token_batch_ms: int = field(default_factory=lambda: _env_int("TOKEN_BATCH_MS", 40))

    # gateway
    port: int = field(default_factory=lambda: _env_int("PORT", 8080))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _env_list("CORS_ORIGINS", ("http://localhost:8000", "http://127.0.0.1:8000"))
    )
    chat_timeout_s: float = field(default_factory=lambda: _env_float("CHAT_TIMEOUT_S", 120.0))
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", "dev-token"))
    monolith: bool = field(default_factory=lambda: _env_bool("MONOLITH", False))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "info"))

    service_ports: dict[str, int] = field(
        default_factory=lambda: {
            "gateway": _env_int("GATEWAY_PORT", 8080),
            "orchestrator": _env_int("ORCHESTRATOR_PORT", 8081),
            "sqlite_service": _env_int("SQLITE_SERVICE_PORT", 8082),
            "vector_service": _env_int("VECTOR_SERVICE_PORT", 8083),
            "graph_service": _env_int("GRAPH_SERVICE_PORT", 8084),
            "llm_service": _env_int("LLM_SERVICE_PORT", 8085),
            "answer_service": _env_int("ANSWER_SERVICE_PORT", 8086),
            "event_log": _env_int("EVENT_LOG_PORT", 8087),
            "control_plane": _env_int("CONTROL_PLANE_PORT", 8090),
        }
    )


def get_settings() -> Settings:
    """Fresh read of the environment (tests swap env per case)."""
    return Settings()
