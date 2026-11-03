"""``data/papers.jsonl`` -> Qdrant collection (embedded by default).

Uses :class:`retrieval.vector_store.Embedder` and ``VectorStore`` so index-time
and query-time vectors are produced by exactly the same code. The offset is
checkpointed to ``data/build_manifest.json`` after every batch, so a killed
build resumes where it stopped (``--resume``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from retrieval.vector_store import Embedder, VectorStore

MANIFEST_PATH = Path("data/build_manifest.json")
ABSTRACT_CHARS = 1200


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def save_manifest(data: dict[str, Any], path: Path = MANIFEST_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def checkpoint(manifest_path: Path, collection: str, offset: int, **extra: Any) -> None:
    manifest = load_manifest(manifest_path)
    entry = dict(manifest.get("qdrant") or {})
    entry.update(
        {
            "collection": collection,
            "offset": offset,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **extra,
        }
    )
    manifest["qdrant"] = entry
    save_manifest(manifest, manifest_path)


def facet_sentence(row: dict) -> str:
    """A compact, tabular-native prefix: the structured axes as words the model can match.

    Without this, "sepsis triage models" only matches abstracts that happen to say it —
    the record's own domain / use-case / formulation / algorithm / validation tier never
    reach the dense leg (they live in the payload and in FTS, which are separate channels).
    """
    values = [
        str(row.get(column) or "").strip()
        for column in ("a_domain", "c_use_case", "e_formulation", "f_algorithm", "g_eval_tier")
    ]
    values = [value for value in values if value]
    return "[" + " · ".join(values) + "] " if values else ""


def embed_text(row: dict) -> str:
    abstract = str(row.get("abstract") or "")[:ABSTRACT_CHARS]
    parts = [facet_sentence(row), row.get("title") or "", row.get("summary") or "", abstract]
    return " ".join(str(part) for part in parts if part and str(part).strip())


def payload(row: dict) -> dict[str, Any]:
    return {
        "pmid": str(row.get("pmid") or ""),
        "year": row.get("year"),
        "a_domain": row.get("a_domain") or "",
        "b_end_user": row.get("b_end_user") or "",
        "c_use_case": row.get("c_use_case") or "",
        "d_analytics": row.get("d_analytics") or "",
        "e_formulation": row.get("e_formulation") or "",
        "f_algorithm": row.get("f_algorithm") or "",
        "g_eval_tier": row.get("g_eval_tier") or "",
        "external_val": row.get("external_val"),
        "deployed": row.get("deployed"),
        "journal": row.get("journal") or "",
    }


def _iter_rows(jsonl: Path):
    with Path(jsonl).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def index(
    jsonl: Path,
    path: Path,
    collection: str,
    *,
    embed_provider: str = "hash",
    model: str = "google/embeddinggemma-2",
    dim: int = 768,
    batch: int = 512,
    embed_batch: int = 32,
    device: str = "cpu",
    recreate: bool = False,
    limit: int | None = None,
    offset: int = 0,
    manifest: Path = MANIFEST_PATH,
    wipe_manifest: bool = False,
) -> dict:
    """Embed + upsert the corpus. Idempotent: point ids are derived from the pmid."""
    jsonl = Path(jsonl)
    if not jsonl.exists():
        raise FileNotFoundError(f"jsonl not found: {jsonl}")
    if wipe_manifest:
        manifest_data = load_manifest(manifest)
        manifest_data.pop("qdrant", None)
        save_manifest(manifest_data, manifest)

    store = VectorStore(Path(path), collection, dim=dim)
    store.ensure_collection(recreate=recreate)
    embedder = Embedder(provider=embed_provider, model=model, dim=dim, device=device, batch=embed_batch)

    stats: dict[str, Any] = {
        "jsonl": str(jsonl),
        "path": str(path),
        "collection": collection,
        "provider": embed_provider,
        "model": model,
        "dim": dim,
        "start_offset": offset,
        "indexed": 0,
        "skipped_no_pmid": 0,
        "offset": offset,
    }

    started = time.time()
    pending_ids: list[str] = []
    pending_texts: list[str] = []
    pending_titles: list[str] = []
    pending_payloads: list[dict] = []
    position = 0
    batch_no = 0

    def flush() -> None:
        nonlocal batch_no
        if not pending_ids:
            return
        vectors = embedder.documents(pending_texts, titles=pending_titles)
        store.upsert(pending_ids, vectors, pending_payloads)
        batch_no += 1
        stats["indexed"] = int(stats["indexed"]) + len(pending_ids)
        stats["offset"] = position
        checkpoint(
            manifest,
            collection,
            position,
            qdrant_path=str(path),
            dim=embedder.dim,
            provider=embed_provider,
            model=model,
        )
        elapsed = max(time.time() - started, 1e-6)
        rate = (int(stats["indexed"])) / elapsed
        print(
            f"[qdrant] batch {batch_no:>5} rows={stats['indexed']:>7} "
            f"offset={position:>7} {rate:>7.0f} rows/s",
            flush=True,
        )
        pending_ids.clear()
        pending_texts.clear()
        pending_titles.clear()
        pending_payloads.clear()

    for row in _iter_rows(jsonl):
        position += 1
        if position <= offset:
            continue
        pmid = str(row.get("pmid") or "").strip()
        if not pmid:
            stats["skipped_no_pmid"] = int(stats["skipped_no_pmid"]) + 1
            continue
        pending_ids.append(pmid)
        pending_texts.append(embed_text(row))
        pending_titles.append(str(row.get("title") or ""))
        pending_payloads.append(payload(row))
        if len(pending_ids) >= batch:
            flush()
            if limit is not None and int(stats["indexed"]) >= limit:
                break
    if limit is None or int(stats["indexed"]) < limit:
        flush()

    stats["count_in_collection"] = store.count()
    elapsed = max(time.time() - started, 1e-6)
    print(
        f"[qdrant] done: indexed={stats['indexed']} offset={stats['offset']} "
        f"collection_count={stats['count_in_collection']} in {elapsed:.1f}s",
        flush=True,
    )
    return stats


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.qdrant_index", description="Embed papers.jsonl into Qdrant.")
    parser.add_argument("--jsonl", default="data/papers.jsonl")
    parser.add_argument("--path", default="data/qdrant")
    parser.add_argument("--collection", default="evalbot_papers")
    parser.add_argument("--embed-provider", default=os.environ.get("EMBED_PROVIDER", "hash"), choices=["hash", "local", "none"])
    parser.add_argument("--model", default=os.environ.get("EMBED_MODEL", "google/embeddinggemma-2"))
    parser.add_argument("--dim", type=int, default=int(os.environ.get("EMBED_DIM", "768")))
    parser.add_argument("--batch", type=int, default=512)
    parser.add_argument("--embed-batch", type=int, default=int(os.environ.get("EMBED_BATCH", "32")))
    parser.add_argument("--device", default=os.environ.get("EMBED_DEVICE", "cpu"))
    parser.add_argument("--recreate", action="store_true", help="drop and recreate the collection (needed when the dim changes)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="continue from the checkpointed offset")
    args = parser.parse_args(argv)

    offset = args.offset
    if args.resume:
        saved = (load_manifest() or {}).get("qdrant") or {}
        if saved.get("collection") == args.collection and saved.get("offset"):
            offset = int(saved["offset"])
            print(f"[qdrant] resuming from checkpointed offset {offset}")

    try:
        index(
            Path(args.jsonl),
            Path(args.path),
            args.collection,
            embed_provider=args.embed_provider,
            model=args.model,
            dim=args.dim,
            batch=args.batch,
            embed_batch=args.embed_batch,
            device=args.device,
            recreate=args.recreate,
            limit=args.limit,
            offset=offset,
        )
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        print("  run `python -m ingest.ingest` first to produce the jsonl.", file=sys.stderr)
        return 2
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
