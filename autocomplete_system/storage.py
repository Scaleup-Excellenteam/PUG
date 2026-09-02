"""Persistence for offline indexes and online popularity statistics."""

from __future__ import annotations

import json
import logging
import pickle
import time
from pathlib import Path
from typing import Any, TypeAlias

from .constants import (
    INDEX_FILENAME,
    INDEX_VERSION,
    MASTER_ARRAY_FILENAME,
    RANKING_SETTINGS_FILENAME,
    USAGE_STATS_FILENAME,
)
from .models import RankingMode, SentenceRecord
from .logging_config import log_event
from .sqlite_index import SQLiteSubstringIndex
from .sqlite_store import SQLiteSentenceStore
from .trie import CompressedSuffixTrie
from .suffix_array import SuffixArrayIndex

SearchIndex: TypeAlias = CompressedSuffixTrie | SQLiteSubstringIndex | SuffixArrayIndex
MasterArray: TypeAlias = list[SentenceRecord] | SQLiteSentenceStore
LOGGER = logging.getLogger("autocomplete.storage")


def _atomic_pickle_dump(path: Path, value: Any) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as temporary_file:
        pickle.dump(value, temporary_file, protocol=pickle.HIGHEST_PROTOCOL)
    temporary_path.replace(path)


def _atomic_write_text(path: Path, data: str) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(data, encoding="utf-8")
    temporary_path.replace(path)


def save_index(
    data_directory: Path,
    index: SearchIndex,
    master_array: MasterArray,
) -> None:
    """Persist index metadata, sentence storage, and current usage counts."""

    started = time.perf_counter()
    log_event(
        LOGGER,
        "index_save_started",
        data_directory=str(data_directory),
        backend=type(index).__name__,
        sentence_count=len(master_array),
    )
    data_directory.mkdir(parents=True, exist_ok=True)
    if isinstance(index, SQLiteSubstringIndex):
        index.attach(data_directory)
        database_path = data_directory / index.database_filename
        if not database_path.is_file():
            raise FileNotFoundError(f"SQLite search database not found: {database_path}")

    _atomic_pickle_dump(
        data_directory / INDEX_FILENAME,
        {"version": INDEX_VERSION, "index": index},
    )
    master_path = data_directory / MASTER_ARRAY_FILENAME
    if isinstance(index, SQLiteSubstringIndex) and index.compact_schema:
        # Sentence rows are already stored once in SQLite.  Keep only a tiny
        # compatibility manifest at the historical filename instead of a
        # multi-gigabyte duplicate of the corpus.
        _atomic_pickle_dump(
            master_path,
            {"storage": "sqlite", "sentence_count": len(master_array)},
        )
    else:
        _atomic_pickle_dump(master_path, master_array)
    save_usage_stats(data_directory, master_array)
    log_event(
        LOGGER,
        "index_save_completed",
        data_directory=str(data_directory),
        backend=type(index).__name__,
        sentence_count=len(master_array),
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )


def load_index(data_directory: Path) -> tuple[SearchIndex, MasterArray]:
    """Load and validate the serialized search index and master sentence array."""

    started = time.perf_counter()
    log_event(LOGGER, "index_load_started", data_directory=str(data_directory))
    index_path = data_directory / INDEX_FILENAME
    master_path = data_directory / MASTER_ARRAY_FILENAME
    if not index_path.is_file():
        raise FileNotFoundError(
            "Autocomplete index not found. Run build_index.py before starting the CLI."
        )

    with index_path.open("rb") as index_file:
        envelope: Any = pickle.load(index_file)
    if (
        not isinstance(envelope, dict)
        or envelope.get("version") != INDEX_VERSION
        or not isinstance(
            envelope.get("index"), (CompressedSuffixTrie, SQLiteSubstringIndex, SuffixArrayIndex)
        )
    ):
        raise ValueError(
            "The serialized autocomplete index is invalid or has an unsupported version."
        )
    index = envelope["index"]
    if isinstance(index, SQLiteSubstringIndex):
        database_path = data_directory / index.database_filename
        if not database_path.is_file():
            raise FileNotFoundError(f"SQLite search database not found: {database_path}")
        index.attach(data_directory)
        if index.compact_schema:
            master_array: MasterArray = SQLiteSentenceStore(database_path)
        else:
            if not master_path.is_file():
                raise FileNotFoundError(
                    "Autocomplete index not found. Run build_index.py before starting the CLI."
                )
            with master_path.open("rb") as master_file:
                master_array = pickle.load(master_file)
    else:
        if not master_path.is_file():
            raise FileNotFoundError(
                "Autocomplete index not found. Run build_index.py before starting the CLI."
            )
        with master_path.open("rb") as master_file:
            master_array = pickle.load(master_file)

    if not isinstance(master_array, (list, SQLiteSentenceStore)) or (
        isinstance(master_array, list)
        and not all(isinstance(record, SentenceRecord) for record in master_array)
    ):
        raise ValueError("The serialized master array is invalid.")

    load_usage_stats(data_directory, master_array)
    log_event(
        LOGGER,
        "index_load_completed",
        data_directory=str(data_directory),
        backend=type(index).__name__,
        sentence_count=len(master_array),
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    return index, master_array


def load_usage_stats(
    data_directory: Path,
    master_array: MasterArray,
) -> None:
    """Reset and apply persisted non-negative usage counts."""

    if isinstance(master_array, SQLiteSentenceStore):
        master_array.reset_usage_counts()
    else:
        for record in master_array:
            record.usage_count = 0

    stats_path = data_directory / USAGE_STATS_FILENAME
    if not stats_path.exists():
        return

    raw_stats: Any = json.loads(stats_path.read_text(encoding="utf-8"))
    if not isinstance(raw_stats, dict):
        raise ValueError("usage_stats.json must contain a JSON object.")

    validated_stats: dict[int, int] = {}
    for raw_sentence_id, raw_count in raw_stats.items():
        try:
            sentence_id = int(raw_sentence_id)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Invalid sentence ID in usage stats: {raw_sentence_id}"
            ) from error
        if (
            sentence_id < 0
            or sentence_id >= len(master_array)
            or not isinstance(raw_count, int)
            or isinstance(raw_count, bool)
            or raw_count < 0
        ):
            raise ValueError(f"Invalid usage-stat entry for sentence ID {raw_sentence_id}.")
        validated_stats[sentence_id] = raw_count
    if isinstance(master_array, SQLiteSentenceStore):
        master_array.replace_usage_counts(validated_stats)
    else:
        for sentence_id, count in validated_stats.items():
            master_array[sentence_id].usage_count = count
    log_event(
        LOGGER,
        "usage_stats_loaded",
        data_directory=str(data_directory),
        nonzero_entries=len(raw_stats),
    )


def save_usage_stats(
    data_directory: Path,
    master_array: MasterArray,
) -> None:
    """Persist nonzero usage counts; omitted IDs implicitly have count zero."""

    data_directory.mkdir(parents=True, exist_ok=True)
    if isinstance(master_array, SQLiteSentenceStore):
        stats = {
            str(sentence_id): count
            for sentence_id, count in master_array.usage_counts().items()
        }
    else:
        stats = {
            str(sentence_id): record.usage_count
            for sentence_id, record in enumerate(master_array)
            if record.usage_count
        }
    serialized = json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
    _atomic_write_text(data_directory / USAGE_STATS_FILENAME, serialized)
    log_event(
        LOGGER,
        "usage_stats_persisted",
        data_directory=str(data_directory),
        nonzero_entries=len(stats),
    )


def load_ranking_mode_setting(
    data_directory: Path,
    default: RankingMode,
) -> RankingMode:
    """Load the web ranking preference, or return ``default`` if none exists."""

    settings_path = data_directory / RANKING_SETTINGS_FILENAME
    if not settings_path.exists():
        log_event(
            LOGGER,
            "ranking_mode_setting_defaulted",
            data_directory=str(data_directory),
            ranking_mode=default.value,
        )
        return default
    raw_settings: Any = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(raw_settings, dict):
        raise ValueError("ranking_settings.json must contain a JSON object.")
    raw_mode = raw_settings.get("ranking_mode")
    try:
        ranking_mode = RankingMode(raw_mode)
    except (TypeError, ValueError) as error:
        raise ValueError("ranking_settings.json contains an invalid ranking_mode.") from error
    log_event(
        LOGGER,
        "ranking_mode_setting_loaded",
        data_directory=str(data_directory),
        ranking_mode=ranking_mode.value,
    )
    return ranking_mode


def save_ranking_mode_setting(
    data_directory: Path,
    ranking_mode: RankingMode,
) -> None:
    """Atomically persist the web ranking preference."""

    data_directory.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        {"ranking_mode": ranking_mode.value},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    _atomic_write_text(data_directory / RANKING_SETTINGS_FILENAME, serialized)
    log_event(
        LOGGER,
        "ranking_mode_setting_persisted",
        data_directory=str(data_directory),
        ranking_mode=ranking_mode.value,
    )
