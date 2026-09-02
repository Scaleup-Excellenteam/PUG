"""Command-line entry point for the offline indexing phase."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from autocomplete_system.constants import (
    DEFAULT_DATA_DIRECTORY,
    DEFAULT_INPUT_SOURCES,
    SQLITE_BUILD_CACHE_MIB,
    SQLITE_INSERT_BATCH_SIZE,
)
from autocomplete_system.build_metrics import write_build_metrics
from autocomplete_system.indexer import build_index, build_sqlite_index
from autocomplete_system.logging_config import configure_system_logging, log_event
from autocomplete_system.storage import save_index
from autocomplete_system.source_manifest import (
    build_source_manifest,
    manifests_match,
    write_source_manifest,
)


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
            "multiple sources (default: Archive, including nested TXT and ZIP files)."
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
        choices=("sqlite", "trie", "array"),
        default="sqlite",
        help=(
            "sqlite is scalable for the supplied archive; trie builds the literal "
            "all-character compressed suffix Trie for smaller corpora; array builds the Suffix Array."
        ),
    )
    parser.add_argument(
        "--insert-batch-size",
        type=int,
        default=SQLITE_INSERT_BATCH_SIZE,
        help="Rows buffered per SQLite insert transaction.",
    )
    parser.add_argument(
        "--cache-mib",
        type=int,
        default=SQLITE_BUILD_CACHE_MIB,
        help="Maximum SQLite page cache used while building.",
    )
    return parser.parse_args()


def main() -> None:
    configure_system_logging()
    args = parse_args()
    sources = tuple(args.sources) if args.sources else DEFAULT_INPUT_SOURCES
    input_manifest = build_source_manifest(sources)
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

        build_options = {}
        if hasattr(args, "insert_batch_size"):
            build_options["insert_batch_size"] = args.insert_batch_size
        if hasattr(args, "cache_mib"):
            build_options["cache_mib"] = args.cache_mib
        index, master_array = build_sqlite_index(
            sources,
            args.data_dir,
            progress_callback=report_progress,
            **build_options,
        )
    elif args.backend == "array":
        print("Building the suffix array...", flush=True)
        from autocomplete_system.indexer import build_array_index
        index, master_array = build_array_index(sources)
    else:
        print("Building the literal compressed suffix Trie...", flush=True)
        index, master_array = build_index(sources)

    save_index(args.data_dir, index, master_array)
    final_manifest = build_source_manifest(sources, previous=input_manifest)
    if not manifests_match(input_manifest, final_manifest):
        raise RuntimeError(
            "Input files changed during index construction; the new index was not published."
        )
    write_source_manifest(args.data_dir, final_manifest)
    elapsed = time.perf_counter() - started
    sentence_count = len(master_array)
    source_bytes = int(final_manifest.get("total_bytes", 0))
    write_build_metrics(
        args.data_dir,
        {
            "backend": args.backend,
            "sources": [str(source) for source in sources],
            "source_file_count": len(final_manifest.get("files", [])),
            "source_bytes": source_bytes,
            "sentence_count": sentence_count,
            "duration_seconds": round(elapsed, 3),
            "sentences_per_second": round(sentence_count / elapsed, 3)
            if elapsed
            else 0.0,
            "input_mib_per_second": round(
                source_bytes / (1024 * 1024) / elapsed, 3
            )
            if elapsed
            else 0.0,
            "insert_batch_size": getattr(args, "insert_batch_size", None),
            "sqlite_cache_mib": getattr(args, "cache_mib", None),
        },
    )
    print(
        f"Indexed {sentence_count:,} sentences into {args.data_dir} "
        f"using {args.backend} in {elapsed:.1f}s."
    )
    log_event(
        LOGGER,
        "offline_build_completed",
        sources=[str(source) for source in sources],
        data_directory=str(args.data_dir),
        backend=args.backend,
        sentence_count=sentence_count,
        duration_seconds=round(elapsed, 3),
    )


if __name__ == "__main__":
    main()
