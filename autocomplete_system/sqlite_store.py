"""Lazy, compact access to sentence records stored inside the SQLite index."""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from pathlib import Path

from .models import SentenceRecord
from .normalization import normalize_text


# Keep a stable reference so a test double for the index builder's connection
# does not also replace the sentence store's independent read connection.
_sqlite_connect = sqlite3.connect


class SQLiteSentenceStore(Sequence[SentenceRecord]):
    """Expose the SQLite master table through the historical sequence API.

    The old scalable build duplicated every sentence in a multi-gigabyte pickle.
    This store keeps the public ``master_array`` behavior while fetching only the
    records a request actually needs.  Popularity remains a sparse in-memory map.
    """

    _CACHE_SIZE = 4096

    def __init__(
        self,
        database_path: Path,
        sentence_count: int | None = None,
        schema_version: int | None = None,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        if not self.database_path.is_file():
            raise FileNotFoundError(
                f"SQLite search database not found: {self.database_path}"
            )
        self._lock = threading.RLock()
        self._usage_counts: dict[int, int] = {}
        self._record_cache: OrderedDict[int, tuple[str, str, str, int]] = (
            OrderedDict()
        )
        if sentence_count is None or schema_version is None:
            connection = self._open_connection()
            try:
                if sentence_count is None:
                    metadata = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'sentence_count'"
                    ).fetchone()
                    if metadata is None:
                        raise ValueError(
                            "Compact SQLite index is missing sentence-count metadata."
                        )
                    sentence_count = int(metadata[0])
                if schema_version is None:
                    columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(sentences)")
                    }
                    schema_version = 1 if "normalized" in columns else 2
            finally:
                connection.close()
        assert sentence_count is not None and schema_version is not None
        self._length = sentence_count
        self._schema_version = schema_version

    def _open_connection(self) -> sqlite3.Connection:
        connection = _sqlite_connect(
            self.database_path.as_uri() + "?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA cache_size = -65536")
        connection.execute("PRAGMA mmap_size = 30000000000")
        return connection

    def __len__(self) -> int:
        return self._length

    def _raw_record(self, sentence_id: int) -> tuple[str, str, str, int]:
        with self._lock:
            cached = self._record_cache.get(sentence_id)
            if cached is not None:
                self._record_cache.move_to_end(sentence_id)
                return cached
            connection = self._open_connection()
            try:
                if self._schema_version >= 2:
                    row = connection.execute(
                        """
                        SELECT sentences.original, sources.source_path,
                               sentences.line_number
                        FROM sentences
                        JOIN sources ON sources.source_id = sentences.source_id
                        WHERE sentences.sentence_id = ?
                        """,
                        (sentence_id,),
                    ).fetchone()
                    if row is not None:
                        row = (row[0], normalize_text(str(row[0])), row[1], row[2])
                else:
                    row = connection.execute(
                        """
                        SELECT sentences.original, sentences.normalized,
                               sources.source_path, sentences.line_number
                        FROM sentences
                        JOIN sources ON sources.source_id = sentences.source_id
                        WHERE sentences.sentence_id = ?
                        """,
                        (sentence_id,),
                    ).fetchone()
            finally:
                connection.close()
            if row is None:
                raise IndexError(f"Unknown sentence ID: {sentence_id}")
            record = (str(row[0]), str(row[1]), str(row[2]), int(row[3]))
            self._record_cache[sentence_id] = record
            if len(self._record_cache) > self._CACHE_SIZE:
                self._record_cache.popitem(last=False)
            return record

    def __getitem__(self, item: int | slice) -> SentenceRecord | list[SentenceRecord]:
        if isinstance(item, slice):
            return [self[index] for index in range(*item.indices(self._length))]
        sentence_id = int(item)
        if sentence_id < 0:
            sentence_id += self._length
        if sentence_id < 0 or sentence_id >= self._length:
            raise IndexError("sentence index out of range")
        original, normalized, source_path, line_number = self._raw_record(sentence_id)
        with self._lock:
            usage_count = self._usage_counts.get(sentence_id, 0)
        return SentenceRecord(
            original_text=original,
            normalized_text=normalized,
            source_path=source_path,
            line_number=line_number,
            usage_count=usage_count,
        )

    def __iter__(self) -> Iterator[SentenceRecord]:
        # A dedicated read-only connection lets a long admin scan coexist with
        # normal point lookups without holding the store lock for minutes.
        connection = self._open_connection()
        try:
            normalized_column = (
                "''" if self._schema_version >= 2 else "sentences.normalized"
            )
            cursor = connection.execute(
                f"""
                SELECT sentences.sentence_id, sentences.original,
                       {normalized_column}, sources.source_path,
                       sentences.line_number
                FROM sentences
                JOIN sources ON sources.source_id = sentences.source_id
                ORDER BY sentences.sentence_id
                """
            )
            for row in cursor:
                sentence_id = int(row[0])
                with self._lock:
                    usage_count = self._usage_counts.get(sentence_id, 0)
                yield SentenceRecord(
                    original_text=str(row[1]),
                    normalized_text=(
                        normalize_text(str(row[1]))
                        if self._schema_version >= 2
                        else str(row[2])
                    ),
                    source_path=str(row[3]),
                    line_number=int(row[4]),
                    usage_count=usage_count,
                )
        finally:
            connection.close()

    @property
    def has_usage_counts(self) -> bool:
        with self._lock:
            return bool(self._usage_counts)

    def usage_counts(self) -> dict[int, int]:
        with self._lock:
            return dict(self._usage_counts)

    def replace_usage_counts(self, usage_counts: dict[int, int]) -> None:
        with self._lock:
            self._usage_counts = dict(usage_counts)

    def increment_usage(self, sentence_id: int) -> int:
        if sentence_id < 0 or sentence_id >= self._length:
            raise IndexError(f"Unknown sentence ID: {sentence_id}")
        with self._lock:
            count = self._usage_counts.get(sentence_id, 0) + 1
            self._usage_counts[sentence_id] = count
            return count

    def reset_usage_counts(self) -> None:
        with self._lock:
            self._usage_counts.clear()

    def corpus_statistics(self) -> dict[str, object]:
        """Compute dashboard aggregates in SQLite instead of Python."""

        connection = self._open_connection()
        try:
            if self._schema_version >= 2:
                metadata = {
                    str(row[0]): int(row[1])
                    for row in connection.execute("SELECT key, value FROM metadata")
                }
                totals = (
                    metadata["sentence_count"],
                    metadata["searchable_sentences"],
                    metadata["original_characters"],
                    metadata["normalized_characters"],
                    metadata["longest_original_length"],
                )
                source_query = """
                    SELECT sources.source_path,
                           source_statistics.sentence_count,
                           source_statistics.searchable_count,
                           source_statistics.original_characters
                    FROM source_statistics
                    JOIN sources
                      ON sources.source_id = source_statistics.source_id
                    ORDER BY source_statistics.sentence_count DESC,
                             sources.source_path
                """
            else:
                totals = connection.execute(
                    """
                    SELECT COUNT(*), SUM(normalized <> ''),
                           COALESCE(SUM(original_length), 0),
                           COALESCE(SUM(LENGTH(normalized)), 0),
                           COALESCE(MAX(original_length), 0)
                    FROM sentences
                    """
                ).fetchone()
                source_query = """
                    SELECT sources.source_path, COUNT(*),
                           SUM(sentences.normalized <> ''),
                           SUM(sentences.original_length)
                    FROM sentences
                    JOIN sources ON sources.source_id = sentences.source_id
                    GROUP BY sentences.source_id
                    ORDER BY COUNT(*) DESC, sources.source_path
                """
            sources = [
                {
                    "source_path": str(row[0]),
                    "sentences": int(row[1]),
                    "searchable": int(row[2]),
                    "original_characters": int(row[3]),
                }
                for row in connection.execute(source_query)
            ]
        finally:
            connection.close()
        total = int(totals[0])
        searchable = int(totals[1] or 0)
        original_characters = int(totals[2])
        return {
            "total_sentences": total,
            "searchable_sentences": searchable,
            "normalized_empty_sentences": total - searchable,
            "source_files": len(sources),
            "original_characters": original_characters,
            "normalized_characters": int(totals[3]),
            "average_original_length": round(original_characters / total, 2)
            if total
            else 0.0,
            "longest_original_length": int(totals[4]),
            "sources": sources,
        }

    def close(self) -> None:
        with self._lock:
            self._record_cache.clear()
