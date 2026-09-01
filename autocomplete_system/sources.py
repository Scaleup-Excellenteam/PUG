"""Deterministic UTF-8 line readers for files, directories, and ZIP archives."""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .logging_config import log_event


LOGGER = logging.getLogger("autocomplete.sources")


@dataclass(frozen=True, slots=True)
class SourceLine:
    original_text: str
    source_path: str
    line_number: int


def _iter_decoded_lines(stream: io.TextIOBase, source_path: str) -> Iterator[SourceLine]:
    for line_number, line in enumerate(stream, start=1):
        original_text = line.rstrip("\r\n")
        if original_text.strip():
            yield SourceLine(original_text, source_path, line_number)


def _iter_text_file(path: Path, display_path: str) -> Iterator[SourceLine]:
    count = 0
    log_event(
        LOGGER,
        "source_file_started",
        filesystem_path=str(path),
        source_path=display_path,
    )
    with path.open("r", encoding="utf-8") as source_file:
        for source_line in _iter_decoded_lines(source_file, display_path):
            count += 1
            yield source_line
    log_event(
        LOGGER,
        "source_file_completed",
        filesystem_path=str(path),
        source_path=display_path,
        nonblank_line_count=count,
    )


def _iter_zip(path: Path) -> Iterator[SourceLine]:
    with zipfile.ZipFile(path) as archive:
        entries = sorted(
            (
                entry
                for entry in archive.infolist()
                if not entry.is_dir() and entry.filename.lower().endswith(".txt")
            ),
            key=lambda entry: entry.filename,
        )
        log_event(
            LOGGER,
            "zip_source_opened",
            archive_path=str(path),
            text_entry_count=len(entries),
        )
        for entry in entries:
            count = 0
            log_event(
                LOGGER,
                "zip_entry_started",
                archive_path=str(path),
                source_path=entry.filename,
                compressed_bytes=entry.compress_size,
                uncompressed_bytes=entry.file_size,
            )
            with archive.open(entry, "r") as binary_stream:
                with io.TextIOWrapper(binary_stream, encoding="utf-8") as text_stream:
                    for source_line in _iter_decoded_lines(text_stream, entry.filename):
                        count += 1
                        yield source_line
            log_event(
                LOGGER,
                "zip_entry_completed",
                archive_path=str(path),
                source_path=entry.filename,
                nonblank_line_count=count,
            )


def iter_source_lines(sources: Sequence[Path]) -> Iterator[SourceLine]:
    """Yield every nonblank line from the configured sources deterministically."""

    for source in sorted((Path(item) for item in sources), key=lambda path: str(path)):
        log_event(
            LOGGER,
            "input_source_started",
            source=str(source),
            source_type=(
                "directory"
                if source.is_dir()
                else source.suffix.lower().removeprefix(".") or "unknown"
            ),
        )
        if not source.exists():
            raise FileNotFoundError(f"Input source does not exist: {source}")

        if source.is_dir():
            files = sorted(
                path
                for path in source.rglob("*")
                if path.is_file() and path.suffix.lower() == ".txt"
            )
            for path in files:
                yield from _iter_text_file(path, path.relative_to(source).as_posix())
        elif source.suffix.lower() == ".txt":
            yield from _iter_text_file(source, source.name)
        elif source.suffix.lower() == ".zip":
            yield from _iter_zip(source)
        else:
            raise ValueError(f"Unsupported input source: {source}")
