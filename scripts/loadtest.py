#!/usr/bin/env python3
"""Client-side latency test for a deployed MARS: p50 / p95 / p99 of time-to-first-token, retrieval and total.

    python3 scripts/loadtest.py --url https://<service>.run.app --n 40 --concurrency 2

Uses the streaming endpoint (what the UI uses) and parses its SSE frames: ``meta`` (retrieval finished),
first ``delta`` (first answer token), ``done``. The FIRST request is reported separately as "first": run
against an idle (scaled-to-zero) service to see the cold-start cost, then again for warm numbers.
Be gentle: the free OpenRouter model is rate-limited, so failures are counted, not hidden.
Needs only ``httpx`` (already in requirements.txt).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter

import httpx

QUESTIONS = [
    "What should I evaluate for a sepsis-prediction model?",
    "Which sepsis models were validated on external data?",
    "What fairness metrics are reported for clinical risk scores?",
    "How do papers evaluate calibration of prediction models?",
    "What privacy risks matter when evaluating a medical imaging model?",
    "Which studies report prospective validation of ICU mortality models?",
    "What regulatory considerations apply to a clinical decision support tool?",
    "How is clinical utility measured beyond AUROC?",
    "What robustness checks are used for models deployed across hospitals?",
    "Which papers discuss monitoring for model drift in healthcare AI?",
    "What explainability methods are evaluated for clinical prediction?",
    "How do studies handle missing data when validating a readmission model?",
]


def pct(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (honest for small samples: p99 of 40 requests is simply the max)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))  # ceil
    return ordered[int(rank) - 1]


async def one(client: httpx.AsyncClient, url: str, question: str, timeout: float) -> dict:
    started = time.perf_counter()
    result: dict = {"ok": False, "retrieval_ms": None, "ttft_ms": None, "total_ms": None, "error": None, "status": None}
    body = {"messages": [{"role": "user", "text": question}], "context": {}}
    try:
        async with client.stream("POST", url.rstrip("/") + "/api/chat/stream", json=body, timeout=timeout) as response:
            result["status"] = response.status_code
            if response.status_code != 200:
                result["error"] = f"http {response.status_code}"
            else:
                event = ""
                async for line in response.aiter_lines():
                    now = (time.perf_counter() - started) * 1000
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        if event == "meta" and result["retrieval_ms"] is None:
                            result["retrieval_ms"] = now
                        elif event == "delta" and result["ttft_ms"] is None:
                            result["ttft_ms"] = now
                        elif event == "done":
                            result["ok"] = True
                        elif event == "error":
                            result["error"] = (json.loads(line[5:]).get("message") or "error")[:80]
    except Exception as exc:  # timeout, reset, DNS ...
        result["error"] = f"{type(exc).__name__}"
    result["total_ms"] = (time.perf_counter() - started) * 1000
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--pause", type=float, default=0.0, help="seconds each worker waits between requests")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    async with httpx.AsyncClient() as client:
        first = await one(client, args.url, QUESTIONS[0], args.timeout)  # cold when the service is idle
        queue: asyncio.Queue[str] = asyncio.Queue()
        for i in range(args.n):
            queue.put_nowait(QUESTIONS[(i + 1) % len(QUESTIONS)])
        results: list[dict] = []

        async def worker() -> None:
            while True:
                try:
                    question = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                results.append(await one(client, args.url, question, args.timeout))
                if args.pause:
                    await asyncio.sleep(args.pause)

        wall = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(args.concurrency)])
        wall = time.perf_counter() - wall

    good = [r for r in results if r["ok"]]
    summary = {
        "url": args.url, "requests": len(results), "concurrency": args.concurrency, "wall_s": round(wall, 1),
        "ok": len(good), "error_rate": round(1 - len(good) / max(1, len(results)), 3),
        "errors": dict(Counter(r["error"] for r in results if r["error"])),
        "first_request": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in first.items()},
    }
    for key in ("retrieval_ms", "ttft_ms", "total_ms"):
        values = [r[key] for r in good if r[key] is not None]
        summary[key] = {
            "n": len(values), "p50": pct(values, 50), "p95": pct(values, 95), "p99": pct(values, 99),
            "max": max(values) if values else None, "mean": statistics.fmean(values) if values else None,
        }
        summary[key] = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in summary[key].items()}
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(summary, fh, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
