"""Offline corpus indexing for the in-memory and disk-backed engines."""

from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections.abc import Callable, Sequence
from pathlib import Path

from .constants import MAX_NODE_CACHE_SIZE, SQLITE_INDEX_FILENAME
from .models import SentenceRecord
from .normalization import normalize_text
from .sources import iter_source_lines
from .sqlite_index import SQLiteSubstringIndex
from .trie import CompressedSuffixTrie

ProgressCallback = Callable[[int], None]
InputSources = Path | Sequence[Path]


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

    trie = CompressedSuffixTrie()
    master_array: list[SentenceRecord] = []
    length_keys: list[tuple[object, ...]] = []
    alphabetical_keys: list[tuple[object, ...]] = []

    def length_key(sentence_id: int) -> tuple[object, ...]:
        return length_keys[sentence_id]

    def alphabetical_key(sentence_id: int) -> tuple[object, ...]:
        return alphabetical_keys[sentence_id]

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
        CREATE TABLE sentences (
            sentence_id INTEGER PRIMARY KEY,
            normalized TEXT NOT NULL,
            original TEXT NOT NULL,
            source_path TEXT NOT NULL,
            line_number INTEGER NOT NULL,
            original_length INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE assignment_fts USING fts5(
            normalized,
            sentence_id UNINDEXED,
            tokenize='trigram'
        );
        CREATE VIRTUAL TABLE length_fts USING fts5(
            normalized,
            sentence_id UNINDEXED,
            tokenize='trigram'
        );
        """
    )


def build_sqlite_index(
    sources: InputSources,
    data_directory: Path,
    progress_callback: ProgressCallback | None = None,
) -> tuple[SQLiteSubstringIndex, list[SentenceRecord]]:
    """Build the scalable disk-backed substring index atomically."""

    data_directory.mkdir(parents=True, exist_ok=True)
    database_path = data_directory / SQLITE_INDEX_FILENAME
    temporary_path = database_path.with_suffix(database_path.suffix + ".tmp")
    if temporary_path.exists():
        temporary_path.unlink()

    master_array: list[SentenceRecord] = []
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
        connection.execute("PRAGMA cache_size = -262144")
        _create_sqlite_schema(connection)

        insert_sql = """
            INSERT INTO sentences (
                sentence_id, normalized, original, source_path,
                line_number, original_length
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        pending_rows: list[tuple[object, ...]] = []

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
                    normalized,
                    source_line.original_text,
                    source_line.source_path,
                    source_line.line_number,
                    len(source_line.original_text),
                )
            )

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

            if len(pending_rows) >= 10_000:
                connection.executemany(insert_sql, pending_rows)
                connection.commit()
                pending_rows.clear()
            if progress_callback is not None and (sentence_id + 1) % 100_000 == 0:
                progress_callback(sentence_id + 1)

        if pending_rows:
            connection.executemany(insert_sql, pending_rows)
            connection.commit()

        connection.execute(
            """
            INSERT INTO assignment_fts(normalized, sentence_id)
            SELECT normalized, sentence_id
            FROM sentences
            WHERE normalized <> ''
            ORDER BY normalized, original, source_path, line_number, sentence_id
            """
        )
        connection.execute(
            "INSERT INTO assignment_fts(assignment_fts) VALUES('optimize')"
        )
        connection.execute(
            """
            INSERT INTO length_fts(normalized, sentence_id)
            SELECT normalized, sentence_id
            FROM sentences
            WHERE normalized <> ''
            ORDER BY original_length, original, sentence_id
            """
        )
        connection.execute("INSERT INTO length_fts(length_fts) VALUES('optimize')")
        connection.commit()
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
    )
    index.attach(data_directory)
    return index, master_array
