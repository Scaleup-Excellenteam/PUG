"""Persistent web analytics and read-only administrative system summaries."""

from __future__ import annotations

import csv
import ctypes
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .build_metrics import read_build_metrics
from .constants import (
    ALPHA,
    ANALYTICS_EVENTS_FILENAME,
    DEFAULT_INPUT_SOURCES,
    MAX_NODE_CACHE_SIZE,
    RANKING_SETTINGS_FILENAME,
)
from .engine import AutocompleteSystem
from .sqlite_store import SQLiteSentenceStore
from .source_manifest import (
    build_source_manifest,
    discover_source_files,
    manifests_match,
    read_source_manifest,
)
from .logging_config import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_MAX_LOG_BYTES,
    SYSTEM_LOG_FILENAME,
    log_event,
)
from .models import RankingMode
from .storage import save_ranking_mode_setting


LOGGER = logging.getLogger("autocomplete.analytics")


def utc_now() -> str:
    """Return a stable UTC timestamp suitable for persisted events."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _percentile(sorted_values: list[float], percentage: float) -> float:
    if not sorted_values:
        return 0.0
    index = int(round((len(sorted_values) - 1) * percentage))
    return round(sorted_values[index], 3)


def _latency_summary(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    recent = values[-100:]
    return {
        "sample_count": len(ordered),
        "minimum": round(ordered[0], 3) if ordered else 0.0,
        "average": round(sum(ordered) / len(ordered), 3) if ordered else 0.0,
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "maximum": round(ordered[-1], 3) if ordered else 0.0,
        "total": round(sum(ordered), 3),
        "recent_100_average": round(sum(recent) / len(recent), 3)
        if recent
        else 0.0,
    }


def _process_memory_bytes() -> int | None:
    """Return this process's resident working set without optional packages."""

    if os.name != "nt":
        try:
            import resource

            resident = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            return resident if sys.platform == "darwin" else resident * 1024
        except (ImportError, OSError, ValueError):
            return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    try:
        get_process = ctypes.windll.kernel32.GetCurrentProcess
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process.restype = ctypes.c_void_p
        get_memory.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        )
        get_memory.restype = ctypes.c_int
        if not get_memory(get_process(), ctypes.byref(counters), counters.cb):
            return None
    except (AttributeError, OSError):
        return None
    return int(counters.WorkingSetSize)


class AnalyticsStore:
    """Append-only JSON Lines event storage with on-demand aggregation."""

    def __init__(self, data_directory: Path) -> None:
        self.data_directory = Path(data_directory)
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self.path = self.data_directory / ANALYTICS_EVENTS_FILENAME
        self.path.touch(exist_ok=True)
        self.session_id = uuid.uuid4().hex
        self._lock = threading.RLock()

    def record(self, event_type: str, **details: object) -> dict[str, object]:
        event: dict[str, object] = {
            "timestamp": utc_now(),
            "event_type": event_type,
            "session_id": self.session_id,
            **details,
        }
        serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as event_file:
                event_file.write(serialized + "\n")
        return event

    def read_events(self) -> tuple[list[dict[str, Any]], int]:
        events: list[dict[str, Any]] = []
        malformed_lines = 0
        with self._lock:
            with self.path.open("r", encoding="utf-8") as event_file:
                for line in event_file:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue
                    if isinstance(event, dict):
                        events.append(event)
                    else:
                        malformed_lines += 1
        return events, malformed_lines

    def clear(self) -> None:
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with self._lock:
            temporary_path.write_text("", encoding="utf-8")
            temporary_path.replace(self.path)

    def export_json(self) -> bytes:
        events, malformed_lines = self.read_events()
        return json.dumps(
            {"exported_at": utc_now(), "malformed_lines": malformed_lines, "events": events},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    def export_csv(self) -> bytes:
        events, _ = self.read_events()
        fields: list[str] = sorted({key for event in events for key in event})
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for event in events:
            row = {
                key: (
                    json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    if isinstance(value, (list, dict))
                    else value
                )
                for key, value in event.items()
            }
            writer.writerow(row)
        return output.getvalue().encode("utf-8-sig")

    def summary(self) -> dict[str, object]:
        events, malformed_lines = self.read_events()
        type_counts: Counter[str] = Counter()
        query_stats: dict[str, dict[str, float | int | str]] = {}
        selection_stats: dict[int, dict[str, object]] = {}
        latencies: list[float] = []
        session_latencies: list[float] = []
        searches_by_hour: Counter[str] = Counter()
        recent = deque(maxlen=100)
        voice_searches = 0
        typed_searches = 0
        no_result_searches = 0
        searches_last_hour = 0
        searches_last_24_hours = 0
        now = datetime.now(timezone.utc)

        for event in events:
            event_type = str(event.get("event_type", "unknown"))
            type_counts[event_type] += 1
            recent.append(event)
            if event_type == "search":
                input_method = str(event.get("input_method", "typed"))
                if input_method == "voice":
                    voice_searches += 1
                else:
                    typed_searches += 1
                result_count = int(event.get("result_count", 0))
                if result_count == 0:
                    no_result_searches += 1
                latency = float(event.get("duration_ms", 0.0))
                latencies.append(latency)
                if event.get("session_id") == self.session_id:
                    session_latencies.append(latency)
                timestamp = str(event.get("timestamp", ""))
                if len(timestamp) >= 13:
                    searches_by_hour[timestamp[:13] + ":00"] += 1
                try:
                    event_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if event_time.tzinfo is None:
                        event_time = event_time.replace(tzinfo=timezone.utc)
                    age_seconds = (now - event_time.astimezone(timezone.utc)).total_seconds()
                    searches_last_hour += int(0 <= age_seconds <= 3600)
                    searches_last_24_hours += int(0 <= age_seconds <= 86400)
                except ValueError:
                    pass

                normalized_query = str(event.get("normalized_query", ""))
                query = str(event.get("query", ""))
                item = query_stats.setdefault(
                    normalized_query,
                    {
                        "query": query,
                        "normalized_query": normalized_query,
                        "count": 0,
                        "voice_count": 0,
                        "no_result_count": 0,
                        "total_duration_ms": 0.0,
                    },
                )
                item["count"] = int(item["count"]) + 1
                item["voice_count"] = int(item["voice_count"]) + (
                    1 if input_method == "voice" else 0
                )
                item["no_result_count"] = int(item["no_result_count"]) + (
                    1 if result_count == 0 else 0
                )
                item["total_duration_ms"] = float(item["total_duration_ms"]) + latency
            elif event_type == "selection":
                sentence_id = int(event.get("sentence_id", -1))
                item = selection_stats.setdefault(
                    sentence_id,
                    {
                        "sentence_id": sentence_id,
                        "completed_sentence": str(event.get("completed_sentence", "")),
                        "source_text": str(event.get("source_text", "")),
                        "offset": int(event.get("offset", 0)),
                        "count": 0,
                    },
                )
                item["count"] = int(item["count"]) + 1

        top_queries: list[dict[str, object]] = []
        for item in query_stats.values():
            count = int(item["count"])
            total_duration_ms = float(item["total_duration_ms"])
            top_queries.append(
                {
                    "query": item["query"],
                    "normalized_query": item["normalized_query"],
                    "count": count,
                    "voice_count": item["voice_count"],
                    "no_result_count": item["no_result_count"],
                    "average_duration_ms": round(total_duration_ms / count, 3),
                }
            )
        top_queries.sort(
            key=lambda item: (-int(item["count"]), str(item["normalized_query"]))
        )
        top_selections = sorted(
            selection_stats.values(),
            key=lambda item: (-int(item["count"]), str(item["completed_sentence"])),
        )
        total_searches = type_counts["search"]
        return {
            "event_count": len(events),
            "malformed_lines": malformed_lines,
            "first_event_at": events[0].get("timestamp") if events else None,
            "last_event_at": events[-1].get("timestamp") if events else None,
            "event_types": dict(sorted(type_counts.items())),
            "searches": {
                "total": total_searches,
                "typed": typed_searches,
                "voice": voice_searches,
                "no_results": no_result_searches,
                "success_rate_percent": round(
                    100.0 * (total_searches - no_result_searches) / total_searches, 2
                )
                if total_searches
                else 0.0,
                "unique_normalized_queries": len(query_stats),
                "last_hour": searches_last_hour,
                "last_24_hours": searches_last_24_hours,
            },
            "selections": type_counts["selection"],
            "errors": type_counts["error"],
            "performance_ms": _latency_summary(latencies),
            "session_performance_ms": _latency_summary(session_latencies),
            "searches_by_hour": [
                {"hour": hour, "count": count}
                for hour, count in sorted(searches_by_hour.items())[-24:]
            ],
            "top_queries": top_queries[:50],
            "top_selections": top_selections[:50],
            "recent_events": list(reversed(recent)),
        }


class RebuildManager:
    """Build one replacement index at a time and optionally activate it."""

    def __init__(
        self,
        project_directory: Path,
        source_path: Path | Iterable[Path],
        rebuild_parent: Path,
        activation_callback: Callable[[Path], Path | None] | None = None,
        active_data_directory: Path | None = None,
    ) -> None:
        self.project_directory = Path(project_directory).resolve()
        if isinstance(source_path, Path):
            raw_sources = (source_path,)
        else:
            raw_sources = tuple(source_path)
        if not raw_sources:
            raise ValueError("At least one index source is required.")
        self.source_paths = tuple(Path(item).resolve() for item in raw_sources)
        self.source_path = self.source_paths[0]
        self.rebuild_parent = Path(rebuild_parent).resolve()
        self.activation_callback = activation_callback
        self.active_data_directory = (
            Path(active_data_directory).resolve()
            if active_data_directory is not None
            else None
        )
        self._process: subprocess.Popen[str] | None = None
        self._log_file: io.TextIOWrapper | None = None
        self._target_directory: Path | None = None
        self._log_path: Path | None = None
        self._started_at: str | None = None
        self._started_monotonic: float | None = None
        self._finished_at: str | None = None
        self._finished_monotonic: float | None = None
        self._return_code: int | None = None
        self._activated_at: str | None = None
        self._backup_directory: Path | None = None
        self._activation_error: str | None = None
        self._unchanged_at: str | None = None
        self._lock = threading.RLock()

    def start(self) -> dict[str, object]:
        with self._lock:
            self._refresh()
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A replacement-index build is already running.")
            # ``source_path`` remains the backward-compatible mutable alias for
            # the first configured source.
            self.source_paths = (Path(self.source_path).resolve(),) + self.source_paths[1:]
            for source in self.source_paths:
                if not source.exists():
                    raise FileNotFoundError(f"Source input not found: {source}")

            if self.active_data_directory is not None:
                previous_manifest = read_source_manifest(self.active_data_directory)
                current_manifest = build_source_manifest(
                    self.source_paths,
                    previous=previous_manifest,
                )
                if manifests_match(previous_manifest, current_manifest):
                    self._process = None
                    self._target_directory = None
                    self._log_path = None
                    self._started_at = utc_now()
                    self._started_monotonic = time.monotonic()
                    self._finished_at = self._started_at
                    self._finished_monotonic = self._started_monotonic
                    self._return_code = 0
                    self._activated_at = None
                    self._backup_directory = None
                    self._activation_error = None
                    self._unchanged_at = self._started_at
                    return self.status()

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            self.rebuild_parent.mkdir(parents=True, exist_ok=True)
            self._target_directory = self.rebuild_parent / f"data-rebuild-{stamp}"
            self._log_path = self.rebuild_parent / f"rebuild-{stamp}.log"
            self._log_file = self._log_path.open("w", encoding="utf-8")
            command = [
                sys.executable,
                str(self.project_directory / "build_index.py"),
                "--backend",
                "sqlite",
                "--data-dir",
                str(self._target_directory),
            ]
            for source in self.source_paths:
                command.extend(("--source", str(source)))
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._process = subprocess.Popen(
                command,
                cwd=self.project_directory,
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creation_flags,
            )
            self._started_at = utc_now()
            self._started_monotonic = time.monotonic()
            self._finished_at = None
            self._finished_monotonic = None
            self._return_code = None
            self._activated_at = None
            self._backup_directory = None
            self._activation_error = None
            self._unchanged_at = None
            return self.status()

    def _refresh(self) -> None:
        if self._process is None:
            return
        return_code = self._process.poll()
        if return_code is not None and self._return_code is None:
            self._return_code = return_code
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None
            if return_code == 0 and self.activation_callback is not None:
                assert self._target_directory is not None
                try:
                    self._backup_directory = self.activation_callback(
                        self._target_directory
                    )
                    self._activated_at = utc_now()
                except Exception as error:
                    self._activation_error = f"{type(error).__name__}: {error}"
            self._finished_at = utc_now()
            self._finished_monotonic = time.monotonic()

    def status(self) -> dict[str, object]:
        with self._lock:
            self._refresh()
            if self._unchanged_at is not None:
                state = "unchanged"
            elif self._process is None:
                state = "idle"
            elif self._return_code is None:
                state = "running"
            elif self._return_code == 0 and self._activation_error is None:
                state = "completed"
            else:
                state = "failed"

            log_tail: list[str] = []
            if self._log_path is not None and self._log_path.exists():
                try:
                    log_tail = self._log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()[-12:]
                except OSError:
                    log_tail = []
            elapsed_seconds = (
                round(
                    (self._finished_monotonic or time.monotonic())
                    - self._started_monotonic,
                    3,
                )
                if self._started_monotonic is not None
                else None
            )
            progress_sentences = None
            progress_elapsed_seconds = None
            for line in reversed(log_tail):
                match = re.search(
                    r"Read ([\d,]+) sentences \(([\d.]+)s elapsed\)", line
                )
                if match:
                    progress_sentences = int(match.group(1).replace(",", ""))
                    progress_elapsed_seconds = float(match.group(2))
                    break
            return {
                "state": state,
                "pid": self._process.pid if self._process is not None else None,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "elapsed_seconds": elapsed_seconds,
                "progress_sentences": progress_sentences,
                "progress_sentences_per_second": round(
                    progress_sentences / progress_elapsed_seconds, 2
                )
                if progress_sentences and progress_elapsed_seconds
                else None,
                "return_code": self._return_code,
                "activated": self._activated_at is not None,
                "activated_at": self._activated_at,
                "backup_directory": str(self._backup_directory)
                if self._backup_directory
                else None,
                "activation_error": self._activation_error,
                "source_path": str(self.source_path),
                "source_paths": [str(path) for path in self.source_paths],
                "target_directory": str(self._target_directory)
                if self._target_directory
                else None,
                "log_path": str(self._log_path) if self._log_path else None,
                "log_tail": log_tail,
            }


class AdminService:
    """Aggregate all locally available system and corpus information."""

    def __init__(
        self,
        system: AutocompleteSystem,
        analytics: AnalyticsStore,
        project_directory: Path,
        started_monotonic: float | None = None,
        rebuild_manager: RebuildManager | None = None,
        source_paths: Iterable[Path] | None = None,
        backup_retention: int = 0,
    ) -> None:
        self.system = system
        self.analytics = analytics
        self.project_directory = Path(project_directory).resolve()
        self.started_monotonic = started_monotonic or time.monotonic()
        self.started_at = utc_now()
        if backup_retention < 0:
            raise ValueError("backup_retention cannot be negative.")
        self.backup_retention = backup_retention
        configured_sources = tuple(source_paths or DEFAULT_INPUT_SOURCES)
        resolved_sources = tuple(
            path.resolve()
            if (path := Path(source)).is_absolute()
            else (self.project_directory / path).resolve()
            for source in configured_sources
        )
        rebuild_parent = self.project_directory / "rebuilds"
        configured_data = self.system.data_directory or Path("data")
        active_data_directory = Path(configured_data)
        if not active_data_directory.is_absolute():
            active_data_directory = self.project_directory / active_data_directory
        self.rebuild_manager = rebuild_manager or RebuildManager(
            self.project_directory,
            resolved_sources,
            rebuild_parent,
            activation_callback=self._activate_rebuild,
            active_data_directory=active_data_directory,
        )
        self._static_corpus: dict[str, object] | None = None
        self._usage_by_id: dict[int, int] | None = None
        self._usage_total = 0
        self._static_lock = threading.Lock()
        self._usage_lock = threading.Lock()

    def _prune_index_backups(self, rebuild_parent: Path) -> list[Path]:
        backups = sorted(
            (
                path.resolve()
                for path in rebuild_parent.glob("data-backup-*")
                if path.is_dir()
            ),
            key=lambda path: path.name,
            reverse=True,
        )
        removed = []
        for path in backups[self.backup_retention :]:
            if path.parent != rebuild_parent or not path.name.startswith("data-backup-"):
                raise ValueError(f"Unsafe backup cleanup path: {path}")
            shutil.rmtree(path)
            removed.append(path)
        return removed

    def _activate_rebuild(self, target_directory: Path) -> Path | None:
        """Promote a completed replacement index and reload it without restart."""

        target_directory = Path(target_directory).resolve()
        configured_data = self.system.data_directory or Path("data")
        active_directory = Path(configured_data)
        if not active_directory.is_absolute():
            active_directory = self.project_directory / active_directory
        active_directory = active_directory.resolve()
        rebuild_parent = (self.project_directory / "rebuilds").resolve()
        for path in (active_directory, target_directory, rebuild_parent):
            if not path.is_relative_to(self.project_directory):
                raise ValueError(f"Index activation path is outside the project: {path}")
        if not active_directory.is_dir():
            raise FileNotFoundError(
                f"Active index directory not found: {active_directory}"
            )
        if not target_directory.is_dir():
            raise FileNotFoundError(
                f"Replacement index directory not found: {target_directory}"
            )

        suffix = target_directory.name.removeprefix("data-rebuild-")
        backup_directory = rebuild_parent / f"data-backup-{suffix}"
        if backup_directory.exists():
            raise FileExistsError(
                f"Index backup directory already exists: {backup_directory}"
            )

        for filename in (ANALYTICS_EVENTS_FILENAME, RANKING_SETTINGS_FILENAME):
            source_file = active_directory / filename
            if source_file.is_file():
                shutil.copy2(source_file, target_directory / filename)

        self.system.unload_index()
        active_moved = False
        replacement_moved = False
        try:
            active_directory.rename(backup_directory)
            active_moved = True
            target_directory.rename(active_directory)
            replacement_moved = True
            self.system.reload_index(active_directory)
        except Exception:
            self.system.unload_index()
            if replacement_moved and active_directory.exists():
                active_directory.rename(target_directory)
            if active_moved and backup_directory.exists():
                backup_directory.rename(active_directory)
            if active_directory.is_dir():
                self.system.reload_index(active_directory)
            raise

        with self._static_lock:
            self._static_corpus = None
        with self._usage_lock:
            self._usage_by_id = None
            self._usage_total = 0
        self.analytics.record(
            "index_activation",
            active_directory=str(active_directory),
            backup_directory=str(backup_directory),
            sentence_count=len(self.system.master_array),
            backend=type(self.system.index).__name__,
        )
        removed_backups = self._prune_index_backups(rebuild_parent)
        log_event(
            LOGGER,
            "index_backups_pruned",
            retained=self.backup_retention,
            removed=[str(path) for path in removed_backups],
        )
        return backup_directory if backup_directory.exists() else None

    def _corpus_static(self) -> dict[str, object]:
        with self._static_lock:
            if self._static_corpus is not None:
                return self._static_corpus
            if isinstance(self.system.master_array, SQLiteSentenceStore):
                self._static_corpus = self.system.master_array.corpus_statistics()
                return self._static_corpus
            source_stats: dict[str, dict[str, int]] = defaultdict(
                lambda: {"sentences": 0, "searchable": 0, "original_characters": 0}
            )
            original_characters = 0
            normalized_characters = 0
            searchable_sentences = 0
            longest_length = 0
            for record in self.system.master_array:
                original_length = len(record.original_text)
                normalized_length = len(record.normalized_text)
                original_characters += original_length
                normalized_characters += normalized_length
                searchable_sentences += int(bool(record.normalized_text))
                longest_length = max(longest_length, original_length)
                source = source_stats[record.source_path]
                source["sentences"] += 1
                source["searchable"] += int(bool(record.normalized_text))
                source["original_characters"] += original_length
            total = len(self.system.master_array)
            sources = [
                {"source_path": source_path, **values}
                for source_path, values in source_stats.items()
            ]
            sources.sort(key=lambda item: (-int(item["sentences"]), str(item["source_path"])))
            self._static_corpus = {
                "total_sentences": total,
                "searchable_sentences": searchable_sentences,
                "normalized_empty_sentences": total - searchable_sentences,
                "source_files": len(sources),
                "original_characters": original_characters,
                "normalized_characters": normalized_characters,
                "average_original_length": round(original_characters / total, 2)
                if total
                else 0.0,
                "longest_original_length": longest_length,
                "sources": sources,
            }
            return self._static_corpus

    def _popularity(self) -> dict[str, object]:
        with self._usage_lock:
            if self._usage_by_id is None:
                if isinstance(self.system.master_array, SQLiteSentenceStore):
                    self._usage_by_id = self.system.master_array.usage_counts()
                else:
                    self._usage_by_id = {
                        sentence_id: record.usage_count
                        for sentence_id, record in enumerate(self.system.master_array)
                        if record.usage_count
                    }
                self._usage_total = sum(self._usage_by_id.values())
            usage_snapshot = dict(self._usage_by_id)
            total_usage = self._usage_total
        selected: list[dict[str, object]] = []
        for sentence_id, usage_count in usage_snapshot.items():
            record = self.system.master_array[sentence_id]
            selected.append(
                {
                    "sentence_id": sentence_id,
                    "completed_sentence": record.original_text,
                    "source_text": record.source_path,
                    "offset": record.line_number,
                    "usage_count": usage_count,
                }
            )
        selected.sort(
            key=lambda item: (-int(item["usage_count"]), str(item["completed_sentence"]))
        )
        return {
            "total_usage": total_usage,
            "sentences_with_usage": len(selected),
            "top_sentences": selected[:50],
        }

    def note_selection(self, sentence_id: int) -> None:
        """Update the sparse popularity snapshot after one web selection."""

        with self._usage_lock:
            if self._usage_by_id is None:
                return
            self._usage_by_id[sentence_id] = self.system.master_array[
                sentence_id
            ].usage_count
            self._usage_total += 1

    def _storage(self) -> list[dict[str, object]]:
        data_directory = self.system.data_directory
        files: list[dict[str, object]] = []
        locations: list[tuple[str, str, Path, bool]] = []
        if data_directory is not None:
            locations.append(("index", "data", Path(data_directory), False))
        locations.append(("logs", "logs", self.project_directory / "logs", False))
        locations.append(("packages", "dist", self.project_directory / "dist", False))
        locations.append(("rebuilds", "rebuilds", self.project_directory / "rebuilds", True))
        inactive = self.project_directory / "data_compact"
        if inactive.exists():
            locations.append(("inactive", "data_compact", inactive, True))
        seen: set[Path] = set()
        for category, label, directory, recursive in locations:
            if not directory.exists():
                continue
            candidates = directory.rglob("*") if recursive else directory.iterdir()
            for path in sorted(item for item in candidates if item.is_file()):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                stat = path.stat()
                relative = path.relative_to(directory).as_posix()
                files.append(
                    {
                        "name": f"{label}/{relative}",
                        "category": category,
                        "bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(timespec="seconds"),
                    }
                )

        try:
            source_files = discover_source_files(self.rebuild_manager.source_paths)
        except (FileNotFoundError, ValueError, OSError):
            source_files = []
        for key, path in source_files:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            stat = path.stat()
            files.append(
                {
                    "name": f"source/{key}",
                    "category": "sources",
                    "bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(timespec="seconds"),
                }
            )
        return files

    def _technical_metrics(
        self, storage: list[dict[str, object]], corpus: dict[str, object]
    ) -> dict[str, object]:
        category_bytes: Counter[str] = Counter()
        category_files: Counter[str] = Counter()
        for item in storage:
            category = str(item.get("category", "other"))
            category_bytes[category] += int(item.get("bytes", 0))
            category_files[category] += 1

        active_directory = Path(self.system.data_directory or self.project_directory / "data")
        manifest = read_source_manifest(active_directory)
        build = read_build_metrics(active_directory)
        source_indexed_bytes = int((manifest or {}).get("total_bytes", 0))
        source_current_bytes = int(category_bytes.get("sources", 0))
        try:
            current_source_snapshot = []
            for key, path in discover_source_files(self.rebuild_manager.source_paths):
                stat = path.stat()
                current_source_snapshot.append(
                    {"key": key, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
                )
        except (FileNotFoundError, ValueError, OSError):
            current_source_snapshot = []
        indexed_source_snapshot = [
            {
                "key": item.get("key"),
                "size": item.get("size"),
                "mtime_ns": item.get("mtime_ns"),
            }
            for item in (manifest or {}).get("files", [])
            if isinstance(item, dict)
        ]
        source_changes_pending = current_source_snapshot != indexed_source_snapshot
        package_bytes = int(category_bytes.get("packages", 0))
        disk = shutil.disk_usage(active_directory if active_directory.exists() else self.project_directory)
        sentence_count = int(corpus.get("total_sentences", 0))
        index_bytes = int(category_bytes.get("index", 0))

        if build is None:
            build = {
                "version": None,
                "completed_at": None,
                "backend": type(self.system.index).__name__,
                "source_file_count": len((manifest or {}).get("files", [])),
                "source_bytes": source_indexed_bytes,
                "sentence_count": sentence_count,
                "duration_seconds": None,
                "sentences_per_second": None,
                "input_mib_per_second": None,
                "output_bytes": index_bytes,
            }

        package_file = max(
            (item for item in storage if item.get("category") == "packages"),
            key=lambda item: int(item.get("bytes", 0)),
            default=None,
        )
        transfer_bytes = int(package_file.get("bytes", 0)) if package_file else 0
        upload_estimates = [
            {
                "megabits_per_second": speed,
                "seconds": round(transfer_bytes * 8 / (speed * 1_000_000), 1),
            }
            for speed in (10, 50, 100)
        ] if transfer_bytes else []

        return {
            "runtime": {
                "index_load_ms": self.system.index_load_duration_ms,
                "process_memory_bytes": _process_memory_bytes(),
            },
            "storage": {
                "tracked_total_bytes": sum(category_bytes.values()),
                "categories": {
                    category: {
                        "bytes": category_bytes[category],
                        "files": category_files[category],
                    }
                    for category in sorted(category_bytes)
                },
                "active_index_bytes": index_bytes,
                "bytes_per_sentence": round(index_bytes / sentence_count, 2)
                if sentence_count
                else 0.0,
                "source_current_bytes": source_current_bytes,
                "source_indexed_bytes": source_indexed_bytes,
                "source_changes_pending": source_changes_pending,
                "disk_total_bytes": disk.total,
                "disk_used_bytes": disk.used,
                "disk_free_bytes": disk.free,
            },
            "last_build": build,
            "upload": {
                "package_name": package_file.get("name") if package_file else None,
                "package_bytes": transfer_bytes,
                "estimates": upload_estimates,
                "note": "Calculated transfer time only; connection overhead is not measured.",
            },
        }

    def dashboard(self) -> dict[str, object]:
        corpus = dict(self._corpus_static())
        corpus["popularity"] = self._popularity()
        storage = self._storage()
        analytics_summary = self.analytics.summary()
        technical = self._technical_metrics(storage, corpus)
        return {
            "generated_at": utc_now(),
            "server": {
                "status": "online",
                "started_at": self.started_at,
                "uptime_seconds": round(time.monotonic() - self.started_monotonic, 3),
                "process_id": os.getpid(),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
            },
            "configuration": {
                "index_backend": type(self.system.index).__name__,
                "ranking_mode": self.system.ranking_mode.value,
                "popularity_enabled": self.system.ranking_mode is RankingMode.POPULARITY,
                "alpha": ALPHA,
                "max_node_cache_size": MAX_NODE_CACHE_SIZE,
                "data_directory": str(self.system.data_directory)
                if self.system.data_directory
                else None,
                "index_sources": [
                    str(path) for path in self.rebuild_manager.source_paths
                ],
                "index_backup_retention": self.backup_retention,
                "analytics_file": str(self.analytics.path),
                "system_log_file": str(
                    self.project_directory / "logs" / SYSTEM_LOG_FILENAME
                ),
                "system_log_max_bytes": DEFAULT_MAX_LOG_BYTES,
                "system_log_backup_count": DEFAULT_LOG_BACKUP_COUNT,
            },
            "corpus": corpus,
            "storage": storage,
            "technical": technical,
            "analytics": analytics_summary,
            "rebuild": self.rebuild_manager.status(),
        }

    def sentences_page(self, offset: int, limit: int) -> dict[str, object]:
        total = len(self.system.master_array)
        start = min(max(offset, 0), total)
        end = min(start + min(max(limit, 1), 100), total)
        records = []
        for sentence_id in range(start, end):
            record = self.system.master_array[sentence_id]
            records.append(
                {
                    "sentence_id": sentence_id,
                    "original_text": record.original_text,
                    "normalized_text": record.normalized_text,
                    "source_path": record.source_path,
                    "line_number": record.line_number,
                    "usage_count": record.usage_count,
                }
            )
        return {"offset": start, "limit": limit, "total": total, "records": records}

    def _allowed_log_names(self) -> tuple[str, ...]:
        return (SYSTEM_LOG_FILENAME,) + tuple(
            f"{SYSTEM_LOG_FILENAME}.{number}"
            for number in range(1, DEFAULT_LOG_BACKUP_COUNT + 1)
        )

    def resolve_log_file(self, filename: str) -> Path:
        """Resolve only the active log or one configured rotation backup."""

        if filename not in self._allowed_log_names():
            raise ValueError("Unknown operational log file.")
        path = (self.project_directory / "logs" / filename).resolve()
        expected_parent = (self.project_directory / "logs").resolve()
        if path.parent != expected_parent or not path.is_file():
            raise FileNotFoundError(f"Operational log file not found: {filename}")
        return path

    def log_files(self) -> list[dict[str, object]]:
        files = []
        for filename in self._allowed_log_names():
            try:
                path = self.resolve_log_file(filename)
            except FileNotFoundError:
                continue
            stat = path.stat()
            files.append(
                {
                    "filename": filename,
                    "active": filename == SYSTEM_LOG_FILENAME,
                    "bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(timespec="milliseconds"),
                }
            )
        return files

    def read_system_logs(
        self,
        filename: str,
        limit: int,
        level: str = "",
        component: str = "",
        search: str = "",
    ) -> dict[str, object]:
        """Return filtered recent structured records, newest first."""

        path = self.resolve_log_file(filename)
        raw_lines = path.read_bytes().splitlines()[-5000:]
        normalized_level = level.upper().strip()
        normalized_component = component.casefold().strip()
        normalized_search = search.casefold().strip()
        records: list[dict[str, Any]] = []
        malformed_lines = 0
        for raw_line in reversed(raw_lines):
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                malformed_lines += 1
                continue
            if not isinstance(event, dict):
                malformed_lines += 1
                continue
            if normalized_level and str(event.get("level", "")).upper() != normalized_level:
                continue
            if normalized_component and normalized_component not in str(
                event.get("logger", "")
            ).casefold():
                continue
            if normalized_search and normalized_search not in json.dumps(
                event, ensure_ascii=False, default=str
            ).casefold():
                continue
            records.append(event)
            if len(records) >= limit:
                break
        stat = path.stat()
        return {
            "filename": filename,
            "bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat(timespec="milliseconds"),
            "scanned_lines": len(raw_lines),
            "malformed_lines": malformed_lines,
            "record_count": len(records),
            "records": records,
            "filters": {
                "level": normalized_level,
                "component": component,
                "search": search,
                "limit": limit,
            },
        }

    def reset_analytics(self) -> None:
        self.analytics.clear()

    def reset_popularity(self) -> None:
        self.system.reset_usage_counts()
        with self._usage_lock:
            self._usage_by_id = {}
            self._usage_total = 0
        if self.system.data_directory is not None:
            self.system.save_usage_stats()

    def set_popularity_enabled(self, enabled: bool) -> RankingMode:
        """Enable or disable popularity weighting without deleting usage counts."""

        ranking_mode = (
            RankingMode.POPULARITY if enabled else RankingMode.ASSIGNMENT
        )
        self.system.ranking_mode = ranking_mode
        if self.system.data_directory is not None:
            save_ranking_mode_setting(self.system.data_directory, ranking_mode)
        return ranking_mode

    def start_rebuild(self) -> dict[str, object]:
        return self.rebuild_manager.start()
