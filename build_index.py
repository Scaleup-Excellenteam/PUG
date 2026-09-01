"""Command-line entry point for the offline indexing phase."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from autocomplete_system.constants import (
    DEFAULT_DATA_DIRECTORY,
    DEFAULT_INPUT_SOURCES,
)
from autocomplete_system.indexer import build_index, build_sqlite_index
from autocomplete_system.logging_config import configure_system_logging, log_event
from autocomplete_system.storage import save_index


LOGGER = logging.getLogger("autocomplete.build_cli")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the autocomplete index.")
    parser.add_argument(
        "--source",
        action="append",
        type=Path,
        dest="sources",
        help=(
            "Input .txt file, recursive directory, or ZIP archive. Repeat for "
            "multiple sources (default: Archive/Archive.zip)."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIRECTORY,
        help="Output directory for serialized data (default: data).",
    )
    parser.add_argument(
        "--backend",
        choices=("sqlite", "trie"),
        default="sqlite",
        help=(
            "sqlite is scalable for the supplied archive; trie builds the literal "
            "all-character compressed suffix Trie for smaller corpora."
        ),
    )
    return parser.parse_args()


def main() -> None:
    configure_system_logging()
    args = parse_args()
    sources = tuple(args.sources) if args.sources else DEFAULT_INPUT_SOURCES
    started = time.perf_counter()
    log_event(
        LOGGER,
        "offline_build_started",
        sources=[str(source) for source in sources],
        data_directory=str(args.data_dir),
        backend=args.backend,
    )

    if args.backend == "sqlite":
        print("Building the scalable substring index...", flush=True)

        def report_progress(sentence_count: int) -> None:
            elapsed = time.perf_counter() - started
            print(
                f"Read {sentence_count:,} sentences ({elapsed:.1f}s elapsed)...",
                flush=True,
            )

        index, master_array = build_sqlite_index(
            sources,
            args.data_dir,
            progress_callback=report_progress,
        )
    else:
        print("Building the literal compressed suffix Trie...", flush=True)
        index, master_array = build_index(sources)

    save_index(args.data_dir, index, master_array)
    elapsed = time.perf_counter() - started
    print(
        f"Indexed {len(master_array):,} sentences into {args.data_dir} "
        f"using {args.backend} in {elapsed:.1f}s."
    )
    log_event(
        LOGGER,
        "offline_build_completed",
        sources=[str(source) for source in sources],
        data_directory=str(args.data_dir),
        backend=args.backend,
        sentence_count=len(master_array),
        duration_seconds=round(elapsed, 3),
    )


if __name__ == "__main__":
    main()
