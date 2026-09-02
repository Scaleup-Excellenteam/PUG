"""Convert a legacy SQLite data directory to the compact v2 layout safely."""

from __future__ import annotations

import argparse
import pickle
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from autocomplete_system.constants import (
    ANALYTICS_EVENTS_FILENAME,
    INDEX_FILENAME,
    INDEX_VERSION,
    RANKING_SETTINGS_FILENAME,
    SQLITE_INDEX_FILENAME,
    USAGE_STATS_FILENAME,
)
from autocomplete_system.indexer import (
    _create_sqlite_schema,
    _populate_sqlite_search_data,
)
from autocomplete_system.sqlite_index import SQLiteSubstringIndex
from autocomplete_system.sqlite_store import SQLiteSentenceStore
from autocomplete_system.storage import load_usage_stats, save_index


COPY_BATCH_SIZE = 25_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a compact copy of an existing legacy SQLite autocomplete "
            "data directory. The source is never modified."
        )
    )
    parser.add_argument("--source", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("data_compact"))
    return parser.parse_args()


def _load_legacy_metadata(source: Path) -> SQLiteSubstringIndex:
    index_path = source / INDEX_FILENAME
    if not index_path.is_file():
        raise FileNotFoundError(f"Index metadata not found: {index_path}")
    with index_path.open("rb") as index_file:
        envelope: Any = pickle.load(index_file)
    if (
        not isinstance(envelope, dict)
        or envelope.get("version") != INDEX_VERSION
        or not isinstance(envelope.get("index"), SQLiteSubstringIndex)
    ):
        raise ValueError("Source is not a supported SQLite autocomplete index.")
    index = envelope["index"]
    if index.compact_schema:
        raise ValueError("Source data already uses the compact SQLite layout.")
    return index


def _populate_search_indexes(connection: sqlite3.Connection, sentence_count: int) -> None:
    print("Building compact rank orders and contentless FTS indexes...", flush=True)
    _populate_sqlite_search_data(
        connection,
        sentence_count,
        normalized_relation="legacy.sentences",
    )


def _copy_sentences(
    source_connection: sqlite3.Connection,
    target_connection: sqlite3.Connection,
) -> int:
    source_paths = [
        str(row[0])
        for row in source_connection.execute(
            "SELECT DISTINCT source_path FROM sentences ORDER BY source_path"
        )
    ]
    source_ids = {source_path: number for number, source_path in enumerate(source_paths)}
    target_connection.executemany(
        "INSERT INTO sources(source_id, source_path) VALUES (?, ?)",
        [(source_id, source_path) for source_path, source_id in source_ids.items()],
    )
    cursor = source_connection.execute(
        """
        SELECT sentence_id, normalized, original, source_path,
               line_number, original_length
        FROM sentences
        ORDER BY sentence_id
        """
    )
    copied = 0
    while True:
        rows = cursor.fetchmany(COPY_BATCH_SIZE)
        if not rows:
            break
        target_connection.executemany(
            """
            INSERT INTO sentences(
                sentence_id, original, source_id,
                line_number, original_length, searchable
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row[0],
                    row[2],
                    source_ids[str(row[3])],
                    row[4],
                    row[5],
                    int(bool(row[1])),
                )
                for row in rows
            ],
        )
        copied += len(rows)
        if copied % 250_000 == 0:
            target_connection.commit()
            print(f"Copied {copied:,} sentences...", flush=True)
    target_connection.commit()
    return copied


def _verify_equivalent_searches(
    source_connection: sqlite3.Connection,
    target_connection: sqlite3.Connection,
    sentence_count: int,
) -> None:
    source_count = int(source_connection.execute("SELECT COUNT(*) FROM sentences").fetchone()[0])
    target_count = int(target_connection.execute("SELECT COUNT(*) FROM sentences").fetchone()[0])
    if source_count != sentence_count or target_count != sentence_count:
        raise RuntimeError(
            f"Sentence-count verification failed: {source_count} != {target_count}"
        )
    searchable = int(
        source_connection.execute(
            "SELECT COUNT(*) FROM sentences WHERE normalized <> ''"
        ).fetchone()[0]
    )
    for order_table in ("assignment_order", "length_order"):
        actual = int(target_connection.execute(f"SELECT COUNT(*) FROM {order_table}").fetchone()[0])
        if actual != searchable:
            raise RuntimeError(f"{order_table} contains {actual:,}, expected {searchable:,}")

    sample_ids = sorted(
        {
            0,
            max(sentence_count // 4, 0),
            max(sentence_count // 2, 0),
            max((3 * sentence_count) // 4, 0),
            max(sentence_count - 1, 0),
        }
    )
    for sentence_id in sample_ids:
        row = source_connection.execute(
            "SELECT normalized FROM sentences WHERE sentence_id = ?",
            (sentence_id,),
        ).fetchone()
        if row is None or len(str(row[0])) < 3:
            continue
        query = str(row[0])[: min(12, len(str(row[0])))]
        expression = f'"{query.replace(chr(34), chr(34) * 2)}"'
        for table, order_table in (
            ("assignment_fts", "assignment_order"),
            ("length_fts", "length_order"),
        ):
            legacy_ids = [
                int(item[0])
                for item in source_connection.execute(
                    f"SELECT sentence_id FROM {table} WHERE {table} MATCH ? ORDER BY rowid LIMIT 20",
                    (expression,),
                )
            ]
            compact_ids = [
                int(item[0])
                for item in target_connection.execute(
                    f"""
                    SELECT {order_table}.sentence_id
                    FROM {table}
                    JOIN {order_table} ON {order_table}.rowid = {table}.rowid
                    WHERE {table} MATCH ?
                    ORDER BY {table}.rowid
                    LIMIT 20
                    """,
                    (expression,),
                )
            ]
            if compact_ids != legacy_ids:
                raise RuntimeError(
                    f"Search verification failed for {table} and query {query!r}."
                )


def optimize(source: Path, output: Path) -> Path:
    source = source.resolve()
    output = output.resolve()
    if source == output:
        raise ValueError("--output must differ from --source.")
    if not source.is_dir():
        raise FileNotFoundError(f"Source data directory not found: {source}")
    if output.exists():
        raise FileExistsError(f"Output path already exists: {output}")
    legacy_index = _load_legacy_metadata(source)
    legacy_database = source / legacy_index.database_filename
    if not legacy_database.is_file():
        raise FileNotFoundError(f"SQLite database not found: {legacy_database}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{uuid.uuid4().hex}")
    if temporary.parent != output.parent:
        raise ValueError("Temporary output escaped the requested parent directory.")
    temporary.mkdir()

    compact_database = temporary / SQLITE_INDEX_FILENAME
    started = time.perf_counter()
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    store: SQLiteSentenceStore | None = None
    try:
        source_connection = sqlite3.connect(
            legacy_database.as_uri() + "?mode=ro", uri=True
        )
        target_connection = sqlite3.connect(compact_database)
        target_connection.execute("PRAGMA journal_mode = OFF")
        target_connection.execute("PRAGMA synchronous = OFF")
        target_connection.execute("PRAGMA temp_store = FILE")
        target_connection.execute("PRAGMA cache_size = -262144")
        _create_sqlite_schema(target_connection)
        target_connection.execute(
            "ATTACH DATABASE ? AS legacy",
            (str(legacy_database),),
        )

        sentence_count = _copy_sentences(source_connection, target_connection)
        _populate_search_indexes(target_connection, sentence_count)
        print("Verifying row counts and representative searches...", flush=True)
        _verify_equivalent_searches(
            source_connection, target_connection, sentence_count
        )
        target_connection.close()
        target_connection = None
        source_connection.close()
        source_connection = None

        compact_index = SQLiteSubstringIndex(
            database_filename=SQLITE_INDEX_FILENAME,
            alphabet=legacy_index.alphabet,
            short_assignment_cache=legacy_index.short_assignment_cache,
            short_length_cache=legacy_index.short_length_cache,
            compact_schema=2,
        )
        compact_index.attach(temporary)
        store = SQLiteSentenceStore(
            compact_database,
            sentence_count=sentence_count,
            schema_version=2,
        )
        load_usage_stats(source, store)
        save_index(temporary, compact_index, store)
        compact_index.close()
        store.close()
        store = None

        for filename in (RANKING_SETTINGS_FILENAME, ANALYTICS_EVENTS_FILENAME):
            source_file = source / filename
            if source_file.is_file():
                shutil.copy2(source_file, temporary / filename)

        temporary.rename(output)
        old_bytes = sum(path.stat().st_size for path in source.iterdir() if path.is_file())
        new_bytes = sum(path.stat().st_size for path in output.iterdir() if path.is_file())
        elapsed = time.perf_counter() - started
        print(
            f"Compact data written to {output}\n"
            f"Size: {old_bytes / (1024**3):.2f} GiB -> "
            f"{new_bytes / (1024**3):.2f} GiB "
            f"({(1 - new_bytes / old_bytes) * 100:.1f}% smaller)\n"
            f"Duration: {elapsed:.1f}s",
            flush=True,
        )
        return output
    except BaseException:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        if store is not None:
            store.close()
        if temporary.is_dir() and temporary.parent == output.parent:
            shutil.rmtree(temporary)
        raise


def main() -> None:
    args = parse_args()
    optimize(args.source, args.output)


if __name__ == "__main__":
    main()
