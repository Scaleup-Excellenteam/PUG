"""Required function-level API for the autocomplete system."""

from __future__ import annotations

from pathlib import Path

from autocomplete_system.constants import DEFAULT_DATA_DIRECTORY
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.logging_config import configure_system_logging
from autocomplete_system.models import AutoCompleteData, RankingMode

_system: AutocompleteSystem | None = None


def initialize(
    data_directory: Path = DEFAULT_DATA_DIRECTORY,
    ranking_mode: RankingMode = RankingMode.ASSIGNMENT,
) -> None:
    """Load the serialized index used by the module-level search function."""

    configure_system_logging()
    global _system
    if _system is not None:
        _system.close()
    _system = AutocompleteSystem.load(data_directory, ranking_mode)


def get_best_k_completions(prefix: str) -> list[AutoCompleteData]:
    """Return up to five autocomplete results for ``prefix``."""

    global _system
    if _system is None:
        initialize()
    assert _system is not None
    return _system.get_best_k_completions(prefix)


__all__ = [
    "AutoCompleteData",
    "RankingMode",
    "get_best_k_completions",
    "initialize",
]
