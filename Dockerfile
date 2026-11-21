# MARS-Healthcare on Cloud Run: every service in one process (MONOLITH=1, in-memory bus),
# embedded Qdrant + SQLite baked into the image, embeddinggemma-2 on CPU, Gemma 4 26B-A4B (free) via OpenRouter.
# Built by Cloud Build from the local checkout (data/ and models/ are not in git); see infra/terraform.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-cloud.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 \
 && pip install -r requirements-cloud.txt

COPY . .

# Runtime config (secrets come from Secret Manager via Cloud Run, never from the image).
ENV MONOLITH=1 \
    BUS=memory \
    PORT=8080 \
    LLM_PROVIDER=openrouter \
    GEMMA_MODEL=google/gemma-4-26b-a4b-it:free \
    OPENROUTER_BASE_URL=https://openrouter.ai/api/v1 \
    MODELS_YAML=models.cloud.yaml \
    GUIDANCE_PATH=guidance/sources.yaml \
    TEMPERATURE=0.2 \
    MAX_TOKENS=1200 \
    LLM_TIMEOUT=120 \
    GUIDED_JSON=1 \
    TOKEN_BATCH_CHARS=24 \
    TOKEN_BATCH_MS=40 \
    CHAT_TIMEOUT_S=120 \
    LOG_LEVEL=info \
    SQLITE_ENABLED=1 \
    VECTOR_ENABLED=1 \
    GRAPH_ENABLED=1 \
    DB_PATH=/app/data/papers.db \
    QDRANT_PATH=/app/data/qdrant \
    QDRANT_COLLECTION=evalbot_papers \
    EVENTS_DB=/tmp/events.db \
    MODELS_DB=/tmp/models.db \
    NUM_EVIDENCE=8 \
    MAX_ABSTRACT_CHARS=900 \
    RRF_K=60 \
    EMBED_PROVIDER=local \
    EMBED_MODEL=/app/models/embeddinggemma-2 \
    EMBED_DIM=768 \
    EMBED_WARMUP=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

EXPOSE 8080
CMD ["python", "-m", "services.monolith.app"]
