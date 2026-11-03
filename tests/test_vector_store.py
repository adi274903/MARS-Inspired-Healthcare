"""Offline tests for the model-aware embedder (no model download, no network)."""
from __future__ import annotations

import math

import pytest

from retrieval.vector_store import Embedder, hash_embed


def test_prompt_families_are_asymmetric():
    gemma = Embedder("local", "google/embeddinggemma-2", 768)
    assert gemma.family == "gemma"
    assert gemma.query_prompt("sepsis risk") == "task: search result | query: sepsis risk"
    assert gemma.document_prompt("about sepsis", "A title") == "title: A title | text: about sepsis"
    assert gemma.document_prompt("about sepsis") == "text: about sepsis"

    bge = Embedder("local", "BAAI/bge-base-en-v1.5", 768)
    assert bge.family == "bge"
    assert bge.query_prompt("q").startswith("Represent this sentence")
    assert bge.document_prompt("d") == "passage: d"

    plain = Embedder("local", "some/model", 768)
    assert plain.family == "plain"
    assert plain.query_prompt("q") == "q" and plain.document_prompt("d") == "d"


def test_hash_backend_is_deterministic_and_prompt_free():
    embedder = Embedder("hash", "google/embeddinggemma-2", 256)
    first = embedder.documents(["hello world"])
    second = embedder.query("hello world")
    assert first == [second]
    assert len(first[0]) == 256
    assert hash_embed("hello world", 256) == first[0]


def test_matryoshka_truncation_and_normalisation():
    embedder = Embedder("local", "google/embeddinggemma-2", 512)
    assert embedder.dim == 512  # requested dim, not the model's 768
    vector = embedder._finalise([0.5] * 768)
    assert len(vector) == 512
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_target_dim_above_model_dim_is_rejected():
    embedder = Embedder("local", "google/embeddinggemma-2", 1024)
    embedder._model_dim = 768
    with pytest.raises(ValueError, match="exceeds"):
        embedder._validate_dim()


def test_dim_falls_back_to_model_then_provider_default():
    assert Embedder("local", "google/embeddinggemma-2", 768).dim == 768
    assert Embedder("local", "google/embeddinggemma-2").dim == 768
    assert Embedder("hash").dim == 256
    assert Embedder("none").dim == 256


def test_embed_is_a_documents_alias():
    embedder = Embedder("hash", "google/embeddinggemma-2", 64)
    assert embedder.embed(["a", "b"]) == embedder.documents(["a", "b"])


@pytest.mark.embedder
@pytest.mark.skipif(__import__("os").environ.get("EMBEDDER_TESTS") != "1", reason="set EMBEDDER_TESTS=1")
def test_real_model_shape():
    embedder = Embedder("local", "google/embeddinggemma-2", 768, device="cpu")
    vector = embedder.query("sepsis prediction in the emergency department")
    assert len(vector) == 768
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)
