"""Reproducible local retrieval benchmark; no API key or model calls required."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import numpy as np

from app.resources import BlockingWorkPool
from app.retrieval import SearchIndex
from app.schemas import Passage


async def run(passages: int, questions: int, dimensions: int) -> dict:
    random = np.random.default_rng(7)
    source = [
        Passage(
            str(i),
            f"Security policy {i}: incidents, cloud regions, access controls.",
            "benchmark.json",
        )
        for i in range(passages)
    ]
    started = time.perf_counter()
    index = SearchIndex.build(source, random.normal(size=(passages, dimensions)).tolist())
    build_ms = (time.perf_counter() - started) * 1000
    pool = BlockingWorkPool(4)
    timings = []

    def search(vector):
        start = time.perf_counter()
        index.search("cloud incidents access controls", vector, 5)
        timings.append((time.perf_counter() - start) * 1000)

    started = time.perf_counter()
    try:
        await asyncio.gather(
            *(
                pool.run(search, vector)
                for vector in random.normal(size=(questions, dimensions)).tolist()
            )
        )
    finally:
        await pool.aclose()
    return {
        "passages": passages,
        "questions": questions,
        "dimensions": dimensions,
        "workers": 4,
        "index_build_ms": round(build_ms, 2),
        "search_p50_ms": round(float(np.percentile(timings, 50)), 2),
        "search_p95_ms": round(float(np.percentile(timings, 95)), 2),
        "total_search_ms": round((time.perf_counter() - started) * 1000, 2),
        "vector_bytes": index.vectors.nbytes,
        "scope": "Synthetic retrieval CPU benchmark; excludes PDF extraction and network/model latency.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--passages", type=int, default=500, choices=range(1, 501), metavar="1..500"
    )
    parser.add_argument("--questions", type=int, default=50, choices=range(1, 51), metavar="1..50")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    serialized = json.dumps(asyncio.run(run(args.passages, args.questions, 1536)), indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
