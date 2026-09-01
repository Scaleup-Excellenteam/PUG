"""Data models and enums for translation and input adaptation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AdaptationMode(str, Enum):
    """Input adaptation mode."""

    OFF = "off"
    TRANSLATE = "translate"
    KEYBOARD_REMAP = "keymap"


class SigmaPolicy(str, Enum):
    """Policy for characters outside the system alphabet Sigma."""

    OFF = "off"
    WARN = "warn"
    BLOCK = "block"


@dataclass(slots=True)
class TokenAdaptation:
    """Adaptation result for a single token."""

    original: str
    adapted: str
    was_adapted: bool = False
    method: str = "none"
    detected_language: str | None = None


@dataclass(slots=True)
class QueryAdaptationResult:
    """Full adaptation result for an entire query string."""

    original_query: str
    final_query: str
    was_adapted: bool = False
    keymap_applied: bool = False
    translation_applied: bool = False
    mode: AdaptationMode = AdaptationMode.OFF
    tokens: list[TokenAdaptation] = field(default_factory=list)
    detected_languages: list[str] = field(default_factory=list)
    sigma_violations: list[str] = field(default_factory=list)
    is_blocked: bool = False
    warning_message: str | None = None
