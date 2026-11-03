"""Dense retrieval: Qdrant (embedded or server) + a pluggable text embedder.

``EMBED_PROVIDER=hash`` is a deterministic hashing embedder: no model download,
identical vectors at index and query time, so tests and offline demos are real.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Iterable

from core.telemetry import log

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-]{1,}")


def hash_embed(text: str, dim: int = 256) -> list[float]:
    """Signed feature hashing over word unigrams + bigrams, L2 normalised."""
    tokens = _TOKEN.findall(text.lower())
    if not tokens:
        return [0.0] * dim
    vec = [0.0] * dim
    pairs = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
    for token in pairs:
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class Embedder:
    """provider: hash | local | none.

    ``local`` runs a real embedding model (SentenceTransformer, falling back to a
    transformers mean-pooling path for architectures ST cannot wrap). Queries and
    documents get *different* prompts on purpose: EmbeddingGemma and bge are trained
    with asymmetric instructions, so one shared ``embed()`` silently costs retrieval
    quality with no error to warn you.
    """

    GEMMA_QUERY = "task: search result | query: {text}"
    GEMMA_DOCUMENT = "title: {title} | text: {text}"
    BGE_QUERY = "Represent this sentence for searching relevant passages: {text}"
    BGE_DOCUMENT = "passage: {text}"

    def __init__(
        self,
        provider: str = "hash",
        model: str = "google/embeddinggemma-2",
        dim: int | None = None,
        device: str = "cpu",
        batch: int = 32,
    ) -> None:
        self.provider = provider
        self.model_name = model
        self.device = device
        self.batch = max(1, int(batch))
        self._model: Any = None
        self._tokenizer: Any = None
        self._torch: Any = None
        self._backend = "local" if provider == "local" else ("none" if provider == "none" else "hash")
        self._model_dim = 0
        self._target_dim = int(dim) if dim else 0
        self._error: str | None = None

    # -- metadata --------------------------------------------------------
    @property
    def family(self) -> str:
        name = (self.model_name or "").lower()
        if "embeddinggemma" in name:
            return "gemma"
        if any(tag in name for tag in ("bge", "e5", "gtr", "jina")):
            return "bge"
        return "plain"

    @property
    def dim(self) -> int:
        if self._target_dim:
            return self._target_dim
        if self._model_dim:
            return self._model_dim
        return 768 if self._backend == "local" else 256

    def available(self) -> bool:
        return self.provider in {"hash", "local"}

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "backend": self._backend,
            "model": self.model_name,
            "family": self.family,
            "dim": self.dim,
            "device": self.device,
            "batch": self.batch,
            "loaded": self._model is not None,
            "error": self._error,
        }

    # -- prompts ---------------------------------------------------------
    def query_prompt(self, text: str) -> str:
        family = self.family
        if family == "gemma":
            return self.GEMMA_QUERY.format(text=text)
        if family == "bge":
            return self.BGE_QUERY.format(text=text)
        return text

    def document_prompt(self, text: str, title: str = "") -> str:
        family = self.family
        if family == "gemma":
            if title.strip():
                return self.GEMMA_DOCUMENT.format(title=title.strip()[:200], text=text)
            return f"text: {text}"
        if family == "bge":
            return self.BGE_DOCUMENT.format(text=text)
        return text

    # -- public API ------------------------------------------------------
    def documents(self, texts: Iterable[str], titles: Iterable[str] | None = None) -> list[list[float]]:
        passages = list(texts)
        if not passages:
            return []
        if self._backend == "none":
            return []
        if self._backend == "hash":
            return [hash_embed(text, self.dim) for text in passages]
        title_list = list(titles) if titles is not None else [""] * len(passages)
        if len(title_list) < len(passages):
            title_list += [""] * (len(passages) - len(title_list))
        prompts = [self.document_prompt(text, title) for text, title in zip(passages, title_list)]
        return self._encode_local(prompts)

    def query(self, text: str) -> list[float]:
        if self._backend == "none":
            return []
        if self._backend == "hash":
            return hash_embed(text, self.dim)
        vectors = self._encode_local([self.query_prompt(text)])
        return vectors[0] if vectors else []

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        """Back-compat alias: treat the input as documents."""
        return self.documents(texts)

    # -- local backends --------------------------------------------------
    def _encode_local(self, prompts: list[str]) -> list[list[float]]:
        model = self._load()
        if self._backend == "sentence-transformers":
            vectors = model.encode(
                prompts,
                batch_size=self.batch,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return [self._finalise(list(map(float, vector))) for vector in vectors]
        torch = self._torch
        out: list[list[float]] = []
        with torch.no_grad():
            for start in range(0, len(prompts), self.batch):
                chunk = prompts[start:start + self.batch]
                batch = self._tokenizer(
                    chunk, padding=True, truncation=True, max_length=512, return_tensors="pt"
                ).to(self.device)
                hidden = model(**batch).last_hidden_state
                mask = batch["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
                summed = (hidden * mask).sum(1)
                counts = mask.sum(1).clamp(min=1e-9)
                out.extend(self._finalise(vector) for vector in (summed / counts).cpu().tolist())
        return out

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._error:
            raise RuntimeError(f"embedder unavailable: {self._error}")
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self.model_name, device=self.device)
            self._model_dim = int(model.get_sentence_embedding_dimension() or 0)
            self._backend = "sentence-transformers"
            self._model = model
        except Exception as exc:  # multimodal module / missing package: try raw transformers
            log("embedder", "st.load_failed", model=self.model_name, error=f"{type(exc).__name__}: {exc}")
            try:
                self._load_transformers()
            except Exception as exc2:
                self._error = f"{type(exc2).__name__}: {exc2}"
                log("embedder", "load_failed", model=self.model_name, error=self._error)
                raise RuntimeError(
                    f"could not load embedding model {self.model_name}: {self._error}. "
                    "Set EMBED_PROVIDER=hash to run lexically, or install requirements-optional.txt."
                ) from exc2
        self._validate_dim()
        log("embedder", "loaded", **self.describe())
        return self._model

    def _load_transformers(self) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        # bf16 on GPU: an 8 GB card cannot hold the fp32 weights plus activations.
        dtype = torch.bfloat16 if str(self.device).startswith("cuda") else torch.float32
        model = AutoModel.from_pretrained(self.model_name, torch_dtype=dtype)
        model.eval().to(self.device)
        self._model = model
        self._model_dim = int(getattr(model.config, "hidden_size", 0) or 0)
        self._backend = "transformers"

    def _validate_dim(self) -> None:
        if not self._target_dim:
            self._target_dim = self._model_dim
            return
        if self._model_dim and self._target_dim > self._model_dim:
            self._error = (
                f"EMBED_DIM={self._target_dim} exceeds {self.model_name} dimension {self._model_dim}"
            )
            raise ValueError(self._error)

    def _finalise(self, vector: list[float]) -> list[float]:
        """Matryoshka truncation (never above the model dim) + L2 normalisation."""
        if self._target_dim and len(vector) > self._target_dim:
            vector = vector[: self._target_dim]
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class VectorStore:
    """Thin wrapper over qdrant-client. Embedded (path=) by default, server when url is set."""

    def __init__(self, path: Path, collection: str, dim: int = 256, url: str | None = None) -> None:
        self.path = Path(path)
        self.collection = collection
        self.dim = dim
        self.url = url
        self._client: Any = None

    def client(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            if self.url:
                self._client = QdrantClient(url=self.url)
            else:
                self.path.mkdir(parents=True, exist_ok=True)
                self._client = QdrantClient(path=str(self.path))
        return self._client

    def available(self) -> bool:
        try:
            self.client().get_collection(self.collection)
            return True
        except Exception:
            return False

    def ensure_collection(self, *, recreate: bool = False) -> None:
        from qdrant_client.models import Distance, VectorParams

        client = self.client()
        existing = {c.name for c in client.get_collections().collections}
        if self.collection in existing and recreate:
            client.delete_collection(self.collection)
            existing.discard(self.collection)
        if self.collection not in existing:
            client.create_collection(
                self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    def count(self) -> int:
        try:
            return int(self.client().count(self.collection, exact=True).count)
        except Exception:
            return 0

    def dimension(self) -> int | None:
        """Vector size the collection was created with (None if it does not exist)."""
        try:
            info = self.client().get_collection(self.collection)
            vectors = info.config.params.vectors
            size = getattr(vectors, "size", None)
            if size is None and isinstance(vectors, dict):
                size = vectors.get("size")
            return int(size) if size else None
        except Exception:
            return None

    def upsert(self, ids: list[str], vectors: list[list[float]], payloads: list[dict[str, Any]]) -> None:
        from qdrant_client.models import PointStruct

        points = [PointStruct(id=_point_id(pmid), vector=vec, payload=payload) for pmid, vec, payload in zip(ids, vectors, payloads)]
        self.client().upsert(self.collection, points=points, wait=True)

    def search(self, vector: list[float], limit: int = 20) -> list[str]:
        hits = self.client().query_points(self.collection, query=vector, limit=limit, with_payload=True)
        out: list[str] = []
        for point in getattr(hits, "points", hits):
            pmid = (point.payload or {}).get("pmid")
            if pmid:
                out.append(str(pmid))
        return out

    def by_pmid(self, pmid: str) -> dict[str, Any] | None:
        try:
            records = self.client().retrieve(self.collection, ids=[_point_id(pmid)], with_payload=True)
        except Exception:
            return None
        return dict(records[0].payload or {}) if records else None


    def close(self) -> None:
        """Release the embedded client so process shutdown is clean."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


def _point_id(pmid: str) -> int:
    """Qdrant ids must be uint64 or uuid: derive a stable 63-bit id from the pmid."""
    return int.from_bytes(hashlib.blake2b(str(pmid).encode(), digest_size=8).digest(), "big") >> 1
