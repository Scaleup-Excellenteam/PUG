"""Deterministic UTF-8 line readers for files, directories, and ZIP archives."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path


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
    with path.open("r", encoding="utf-8") as source_file:
        yield from _iter_decoded_lines(source_file, display_path)


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
        for entry in entries:
            with archive.open(entry, "r") as binary_stream:
                with io.TextIOWrapper(binary_stream, encoding="utf-8") as text_stream:
                    yield from _iter_decoded_lines(text_stream, entry.filename)


def iter_source_lines(sources: Sequence[Path]) -> Iterator[SourceLine]:
    """Yield every nonblank line from the configured sources deterministically."""

    for source in sorted((Path(item) for item in sources), key=lambda path: str(path)):
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
