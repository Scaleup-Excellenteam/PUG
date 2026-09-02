"""Offline corpus indexing for the in-memory and disk-backed engines."""

from __future__ import annotations

import logging
import sqlite3
import time
from bisect import bisect_left
from collections.abc import Callable, Sequence
from pathlib import Path

from .constants import (
    MAX_NODE_CACHE_SIZE,
    SQLITE_BUILD_CACHE_MIB,
    SQLITE_INDEX_FILENAME,
    SQLITE_INSERT_BATCH_SIZE,
)
from .models import SentenceRecord
from .logging_config import log_event
from .normalization import normalize_text
from .sources import iter_source_lines
from .sqlite_index import SQLiteSubstringIndex
from .sqlite_store import SQLiteSentenceStore
from .trie import CompressedSuffixTrie

ProgressCallback = Callable[[int], None]
InputSources = Path | Sequence[Path]
LOGGER = logging.getLogger("autocomplete.indexer")


def _coerce_sources(sources: InputSources) -> tuple[Path, ...]:
    if isinstance(sources, Path):
        return (sources,)
    return tuple(Path(source) for source in sources)


def discover_text_files(input_directory: Path) -> list[Path]:
    """Return recursive ``.txt`` files in deterministic path order."""

    if not input_directory.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_directory}")
    return sorted(
        path
        for path in input_directory.rglob("*")
        if path.is_file() and path.suffix.lower() == ".txt"
    )


def build_index(sources: InputSources) -> tuple[CompressedSuffixTrie, list[SentenceRecord]]:
    """Build the literal all-character suffix Trie.

    This is the structurally exact assignment backend. Its quadratic suffix
    construction cost makes it appropriate for small and medium corpora.
    """

    started = time.perf_counter()
    resolved_sources = _coerce_sources(sources)
    log_event(
        LOGGER,
        "trie_build_started",
        sources=[str(source) for source in resolved_sources],
    )
    trie = CompressedSuffixTrie()
    master_array: list[SentenceRecord] = []
    length_keys: list[tuple[object, ...]] = []
    alphabetical_keys: list[tuple[object, ...]] = []

    def length_key(sentence_id: int) -> tuple[object, ...]:
        return length_keys[sentence_id]

    def alphabetical_key(sentence_id: int) -> tuple[object, ...]:
        return alphabetical_keys[sentence_id]

    for source_line in iter_source_lines(resolved_sources):
        normalized = normalize_text(source_line.original_text)
        sentence_id = len(master_array)
        master_array.append(
            SentenceRecord(
                original_text=source_line.original_text,
                normalized_text=normalized,
                source_path=source_line.source_path,
                line_number=source_line.line_number,
            )
        )
        length_keys.append(
            (len(source_line.original_text), source_line.original_text, sentence_id)
        )
        alphabetical_keys.append(
            (
                normalized,
                source_line.original_text,
                source_line.source_path,
                source_line.line_number,
                sentence_id,
            )
        )
        if normalized:
            trie.insert_sentence(normalized, sentence_id, length_key, alphabetical_key)

    log_event(
        LOGGER,
        "trie_build_completed",
        sources=[str(source) for source in resolved_sources],
        sentence_count=len(master_array),
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    return trie, master_array


def build_array_index(sources: InputSources) -> tuple[SuffixArrayIndex, list[SentenceRecord]]:
    from .suffix_array import SuffixArrayIndex
    alphabet = set()
    master_array: list[SentenceRecord] = []
    
    index = SuffixArrayIndex(alphabet=tuple())

    for source_line in iter_source_lines(_coerce_sources(sources)):
        normalized = normalize_text(source_line.original_text)
        sentence_id = len(master_array)
        master_array.append(
            SentenceRecord(
                original_text=source_line.original_text,
                normalized_text=normalized,
                source_path=source_line.source_path,
                line_number=source_line.line_number,
            )
        )
        if normalized:
            alphabet.update(normalized)
            index.insert_sentence(normalized, sentence_id, None, None)

    index.alphabet = tuple(sorted(alphabet))
    index.build()
    return index, master_array


def _cache_ranked_id(
    cache: dict[str, list[tuple[tuple[object, ...], int]]],
    substring: str,
    sentence_id: int,
    key: tuple[object, ...],
) -> None:
    candidates = cache.setdefault(substring, [])
    ranked_id = (key, sentence_id)
    if len(candidates) == MAX_NODE_CACHE_SIZE and ranked_id >= candidates[-1]:
        return
    position = bisect_left(candidates, ranked_id)
    if position >= MAX_NODE_CACHE_SIZE:
        return
    candidates.insert(position, ranked_id)
    if len(candidates) > MAX_NODE_CACHE_SIZE:
        candidates.pop()


def _create_sqlite_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE sources (
            source_id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE
        );
        CREATE TABLE source_statistics (
            source_id INTEGER PRIMARY KEY,
            sentence_count INTEGER NOT NULL,
            searchable_count INTEGER NOT NULL,
            original_characters INTEGER NOT NULL
        );
        CREATE TABLE sentences (
            sentence_id INTEGER PRIMARY KEY,
            original TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            original_length INTEGER NOT NULL,
            searchable INTEGER NOT NULL,
            FOREIGN KEY (source_id) REFERENCES sources(source_id)
        );
        CREATE TABLE assignment_order (sentence_id INTEGER NOT NULL);
        CREATE TABLE length_order (sentence_id INTEGER NOT NULL);
        CREATE VIRTUAL TABLE assignment_fts USING fts5(
            normalized,
            content='',
            columnsize=0,
            tokenize='trigram'
        );
        CREATE VIRTUAL TABLE length_fts USING fts5(
            normalized,
            content='',
            columnsize=0,
            tokenize='trigram'
        );
        CREATE TEMP TABLE normalized_build (
            sentence_id INTEGER PRIMARY KEY,
            normalized TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )


def _populate_sqlite_search_data(
    connection: sqlite3.Connection,
    sentence_count: int,
    normalized_relation: str = "temp.normalized_build",
) -> None:
    """Build both immutable rank orders and their contentless FTS indexes."""

    if normalized_relation not in {"temp.normalized_build", "legacy.sentences"}:
        raise ValueError("Unsupported normalized-text build relation.")
    metadata = (
            ("sentence_count", sentence_count),
            (
                "searchable_sentences",
                connection.execute(
                    "SELECT COUNT(*) FROM sentences WHERE searchable"
                ).fetchone()[0],
            ),
            (
                "original_characters",
                connection.execute(
                    "SELECT COALESCE(SUM(original_length), 0) FROM sentences"
                ).fetchone()[0],
            ),
            (
                "normalized_characters",
                connection.execute(
                    f"SELECT COALESCE(SUM(LENGTH(normalized)), 0) FROM {normalized_relation}"
                ).fetchone()[0],
            ),
            (
                "longest_original_length",
                connection.execute(
                    "SELECT COALESCE(MAX(original_length), 0) FROM sentences"
                ).fetchone()[0],
            ),
        )
    for item in metadata:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            item,
        )
    connection.execute(
        """
        INSERT INTO source_statistics(
            source_id, sentence_count, searchable_count, original_characters
        )
        SELECT source_id, COUNT(*), SUM(searchable), SUM(original_length)
        FROM sentences
        GROUP BY source_id
        """
    )
    connection.execute(
        f"""
        INSERT INTO assignment_order(sentence_id)
        SELECT sentences.sentence_id
        FROM sentences
        JOIN sources ON sources.source_id = sentences.source_id
        JOIN {normalized_relation} AS normalized_rows
          ON normalized_rows.sentence_id = sentences.sentence_id
        WHERE sentences.searchable
        ORDER BY normalized_rows.normalized, sentences.original,
                 sources.source_path, sentences.line_number,
                 sentences.sentence_id
        """
    )
    connection.execute(
        f"""
        INSERT INTO assignment_fts(rowid, normalized)
        SELECT assignment_order.rowid, normalized_rows.normalized
        FROM assignment_order
        JOIN {normalized_relation} AS normalized_rows
          ON normalized_rows.sentence_id = assignment_order.sentence_id
        ORDER BY assignment_order.rowid
        """
    )
    connection.execute("INSERT INTO assignment_fts(assignment_fts) VALUES('optimize')")
    connection.execute(
        """
        INSERT INTO length_order(sentence_id)
        SELECT sentence_id
        FROM sentences
        WHERE searchable
        ORDER BY original_length, original, sentence_id
        """
    )
    connection.execute(
        f"""
        INSERT INTO length_fts(rowid, normalized)
        SELECT length_order.rowid, normalized_rows.normalized
        FROM length_order
        JOIN {normalized_relation} AS normalized_rows
          ON normalized_rows.sentence_id = length_order.sentence_id
        ORDER BY length_order.rowid
        """
    )
    connection.execute("INSERT INTO length_fts(length_fts) VALUES('optimize')")
    connection.commit()


def build_sqlite_index(
    sources: InputSources,
    data_directory: Path,
    progress_callback: ProgressCallback | None = None,
    *,
    insert_batch_size: int = SQLITE_INSERT_BATCH_SIZE,
    cache_mib: int = SQLITE_BUILD_CACHE_MIB,
) -> tuple[SQLiteSubstringIndex, SQLiteSentenceStore]:
    """Build the scalable disk-backed substring index atomically."""

    if insert_batch_size <= 0:
        raise ValueError("insert_batch_size must be positive.")
    if cache_mib <= 0:
        raise ValueError("cache_mib must be positive.")
    started = time.perf_counter()
    resolved_sources = _coerce_sources(sources)
    log_event(
        LOGGER,
        "sqlite_index_build_started",
        sources=[str(source) for source in resolved_sources],
        data_directory=str(data_directory),
    )
    data_directory.mkdir(parents=True, exist_ok=True)
    database_path = data_directory / SQLITE_INDEX_FILENAME
    temporary_path = database_path.with_suffix(database_path.suffix + ".tmp")
    if temporary_path.exists():
        temporary_path.unlink()

    sentence_count = 0
    source_ids: dict[str, int] = {}
    alphabet: set[str] = set()
    ranked_assignment_cache: dict[
        str, list[tuple[tuple[object, ...], int]]
    ] = {}
    ranked_length_cache: dict[str, list[tuple[tuple[object, ...], int]]] = {}
    connection: sqlite3.Connection | None = None

    try:
        connection = sqlite3.connect(temporary_path)
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA temp_store = FILE")
        connection.execute(f"PRAGMA cache_size = -{cache_mib * 1024}")
        connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        _create_sqlite_schema(connection)

        insert_sql = """
            INSERT INTO sentences (
                sentence_id, original, source_id,
                line_number, original_length, searchable
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        pending_rows: list[tuple[object, ...]] = []
        pending_normalized: list[tuple[int, str]] = []

        for source_line in iter_source_lines(resolved_sources):
            normalized = normalize_text(source_line.original_text)
            sentence_id = sentence_count
            sentence_count += 1
            source_id = source_ids.get(source_line.source_path)
            if source_id is None:
                source_id = len(source_ids)
                source_ids[source_line.source_path] = source_id
                connection.execute(
                    "INSERT INTO sources(source_id, source_path) VALUES (?, ?)",
                    (source_id, source_line.source_path),
                )
            assignment_key = (
                normalized,
                source_line.original_text,
                source_line.source_path,
                source_line.line_number,
                sentence_id,
            )
            length_key = (
                len(source_line.original_text),
                source_line.original_text,
                sentence_id,
            )
            pending_rows.append(
                (
                    sentence_id,
                    source_line.original_text,
                    source_id,
                    source_line.line_number,
                    len(source_line.original_text),
                    int(bool(normalized)),
                )
            )
            pending_normalized.append((sentence_id, normalized))

            if normalized:
                alphabet.update(normalized)
                short_substrings = set(normalized)
                short_substrings.update(
                    normalized[index : index + 2]
                    for index in range(len(normalized) - 1)
                )
                for substring in short_substrings:
                    _cache_ranked_id(
                        ranked_assignment_cache,
                        substring,
                        sentence_id,
                        assignment_key,
                    )
                    _cache_ranked_id(
                        ranked_length_cache,
                        substring,
                        sentence_id,
                        length_key,
                    )

            if len(pending_rows) >= insert_batch_size:
                connection.executemany(insert_sql, pending_rows)
                connection.executemany(
                    "INSERT INTO temp.normalized_build VALUES (?, ?)",
                    pending_normalized,
                )
                connection.commit()
                pending_rows.clear()
                pending_normalized.clear()
            if progress_callback is not None and (sentence_id + 1) % 100_000 == 0:
                progress_callback(sentence_id + 1)
            if (sentence_id + 1) % 100_000 == 0:
                log_event(
                    LOGGER,
                    "sqlite_index_build_progress",
                    sentence_count=sentence_id + 1,
                    duration_seconds=round(time.perf_counter() - started, 3),
                )

        if pending_rows:
            connection.executemany(insert_sql, pending_rows)
            connection.executemany(
                "INSERT INTO temp.normalized_build VALUES (?, ?)",
                pending_normalized,
            )
            connection.commit()

        _populate_sqlite_search_data(connection, sentence_count)
        connection.close()
        connection = None
        temporary_path.replace(database_path)
    except BaseException:
        if connection is not None:
            connection.close()
        if temporary_path.exists():
            temporary_path.unlink()
        raise

    short_assignment_cache = {
        substring: [sentence_id for _, sentence_id in candidates]
        for substring, candidates in ranked_assignment_cache.items()
    }
    short_length_cache = {
        substring: [sentence_id for _, sentence_id in candidates]
        for substring, candidates in ranked_length_cache.items()
    }
    index = SQLiteSubstringIndex(
        database_filename=SQLITE_INDEX_FILENAME,
        alphabet=tuple(sorted(alphabet)),
        short_assignment_cache=short_assignment_cache,
        short_length_cache=short_length_cache,
        compact_schema=2,
    )
    index.attach(data_directory)
    master_array = SQLiteSentenceStore(
        database_path,
        sentence_count=sentence_count,
        schema_version=2,
    )
    log_event(
        LOGGER,
        "sqlite_index_build_completed",
        sources=[str(source) for source in resolved_sources],
        data_directory=str(data_directory),
        sentence_count=sentence_count,
        alphabet_size=len(alphabet),
        duration_seconds=round(time.perf_counter() - started, 3),
    )
    return index, master_array
