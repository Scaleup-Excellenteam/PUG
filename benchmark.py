"""Small repeatable latency benchmark for a built autocomplete index."""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

from autocomplete_system.constants import DEFAULT_DATA_DIRECTORY
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.logging_config import configure_system_logging
from autocomplete_system.models import RankingMode

DEFAULT_QUERIES = ("this is", "python algorithm", "the quick brown", "or knot")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark autocomplete searches.")
    parser.add_argument("queries", nargs="*", default=DEFAULT_QUERIES)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIRECTORY)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument(
        "--mode",
        type=RankingMode,
        choices=list(RankingMode),
        default=RankingMode.ASSIGNMENT,
    )
    return parser.parse_args()


def main() -> None:
    configure_system_logging()
    args = parse_args()
    if args.repeat < 1:
        raise ValueError("--repeat must be at least 1")

    load_started = time.perf_counter()
    system = AutocompleteSystem.load(args.data_dir, args.mode)
    load_seconds = time.perf_counter() - load_started
    print(f"load_seconds={load_seconds:.6f}")

    try:
        for query in args.queries:
            first_started = time.perf_counter()
            first_results = system.get_best_k_completions(query)
            first_seconds = time.perf_counter() - first_started

            warm_times = []
            for _ in range(args.repeat):
                started = time.perf_counter()
                system.get_best_k_completions(query)
                warm_times.append(time.perf_counter() - started)

            top = first_results[0].completed_sentence if first_results else "<none>"
            print(
                f"query={query!r} first_ms={first_seconds * 1000:.3f} "
                f"warm_median_ms={statistics.median(warm_times) * 1000:.3f} "
                f"results={len(first_results)} top={top!r}"
            )
    finally:
        system.close()


if __name__ == "__main__":
    main()
