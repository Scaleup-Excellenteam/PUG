"""Persistent web analytics and read-only administrative system summaries."""

from __future__ import annotations

import csv
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    ALPHA,
    ANALYTICS_EVENTS_FILENAME,
    DEFAULT_INPUT_SOURCES,
    MAX_NODE_CACHE_SIZE,
)
from .engine import AutocompleteSystem
from .logging_config import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_MAX_LOG_BYTES,
    SYSTEM_LOG_FILENAME,
)


def utc_now() -> str:
    """Return a stable UTC timestamp suitable for persisted events."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _percentile(sorted_values: list[float], percentage: float) -> float:
    if not sorted_values:
        return 0.0
    index = int(round((len(sorted_values) - 1) * percentage))
    return round(sorted_values[index], 3)


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
        searches_by_hour: Counter[str] = Counter()
        recent = deque(maxlen=100)
        voice_searches = 0
        typed_searches = 0
        no_result_searches = 0

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
                timestamp = str(event.get("timestamp", ""))
                if len(timestamp) >= 13:
                    searches_by_hour[timestamp[:13] + ":00"] += 1

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
        latencies.sort()
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
            },
            "selections": type_counts["selection"],
            "errors": type_counts["error"],
            "performance_ms": {
                "average": round(sum(latencies) / len(latencies), 3)
                if latencies
                else 0.0,
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "maximum": round(latencies[-1], 3) if latencies else 0.0,
            },
            "searches_by_hour": [
                {"hour": hour, "count": count}
                for hour, count in sorted(searches_by_hour.items())[-24:]
            ],
            "top_queries": top_queries[:50],
            "top_selections": top_selections[:50],
            "recent_events": list(reversed(recent)),
        }


class RebuildManager:
    """Launch one non-destructive replacement-index build at a time."""

    def __init__(
        self,
        project_directory: Path,
        source_path: Path,
        rebuild_parent: Path,
    ) -> None:
        self.project_directory = Path(project_directory).resolve()
        self.source_path = Path(source_path).resolve()
        self.rebuild_parent = Path(rebuild_parent).resolve()
        self._process: subprocess.Popen[str] | None = None
        self._log_file: io.TextIOWrapper | None = None
        self._target_directory: Path | None = None
        self._log_path: Path | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._return_code: int | None = None
        self._lock = threading.RLock()

    def start(self) -> dict[str, object]:
        with self._lock:
            self._refresh()
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A replacement-index build is already running.")
            if not self.source_path.exists():
                raise FileNotFoundError(f"Source input not found: {self.source_path}")

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
                "--source",
                str(self.source_path),
                "--data-dir",
                str(self._target_directory),
            ]
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
            self._finished_at = None
            self._return_code = None
            return self.status()

    def _refresh(self) -> None:
        if self._process is None:
            return
        return_code = self._process.poll()
        if return_code is not None and self._return_code is None:
            self._return_code = return_code
            self._finished_at = utc_now()
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None

    def status(self) -> dict[str, object]:
        with self._lock:
            self._refresh()
            if self._process is None:
                state = "idle"
            elif self._return_code is None:
                state = "running"
            elif self._return_code == 0:
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
            return {
                "state": state,
                "pid": self._process.pid if self._process is not None else None,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "return_code": self._return_code,
                "source_path": str(self.source_path),
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
    ) -> None:
        self.system = system
        self.analytics = analytics
        self.project_directory = Path(project_directory).resolve()
        self.started_monotonic = started_monotonic or time.monotonic()
        self.started_at = utc_now()
        source = self.project_directory / DEFAULT_INPUT_SOURCES[0]
        rebuild_parent = self.project_directory / "rebuilds"
        self.rebuild_manager = rebuild_manager or RebuildManager(
            self.project_directory,
            source,
            rebuild_parent,
        )
        self._static_corpus: dict[str, object] | None = None
        self._usage_by_id: dict[int, int] | None = None
        self._usage_total = 0
        self._static_lock = threading.Lock()
        self._usage_lock = threading.Lock()

    def _corpus_static(self) -> dict[str, object]:
        with self._static_lock:
            if self._static_corpus is not None:
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
        locations = []
        if data_directory is not None:
            locations.append(("data", data_directory))
        locations.append(("logs", self.project_directory / "logs"))
        seen: set[Path] = set()
        for label, directory in locations:
            if not directory.exists():
                continue
            for path in sorted(item for item in directory.iterdir() if item.is_file()):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                stat = path.stat()
                files.append(
                    {
                        "name": f"{label}/{path.name}",
                        "bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(timespec="seconds"),
                    }
                )
        return files

    def dashboard(self) -> dict[str, object]:
        corpus = dict(self._corpus_static())
        corpus["popularity"] = self._popularity()
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
                "alpha": ALPHA,
                "max_node_cache_size": MAX_NODE_CACHE_SIZE,
                "data_directory": str(self.system.data_directory)
                if self.system.data_directory
                else None,
                "analytics_file": str(self.analytics.path),
                "system_log_file": str(
                    self.project_directory / "logs" / SYSTEM_LOG_FILENAME
                ),
                "system_log_max_bytes": DEFAULT_MAX_LOG_BYTES,
                "system_log_backup_count": DEFAULT_LOG_BACKUP_COUNT,
            },
            "corpus": corpus,
            "storage": self._storage(),
            "analytics": self.analytics.summary(),
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

    def start_rebuild(self) -> dict[str, object]:
        return self.rebuild_manager.start()
