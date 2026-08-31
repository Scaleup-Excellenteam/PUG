"""Data models used by both the offline and online phases."""

from dataclasses import dataclass
from enum import Enum


class RankingMode(str, Enum):
    """Select assignment-compatible or popularity-weighted ranking."""

    ASSIGNMENT = "assignment"
    POPULARITY = "popularity"


@dataclass(slots=True)
class SentenceRecord:
    """A single source line in the master sentence array."""

    original_text: str
    normalized_text: str
    source_path: str
    line_number: int
    usage_count: int = 0


@dataclass(slots=True)
class AutoCompleteData:
    """One autocomplete result returned by the public API."""

    completed_sentence: str
    source_text: str
    offset: int
    score: int
