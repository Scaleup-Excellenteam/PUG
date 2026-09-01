"""Translation and input adaptation module.

Provides token-level translation for bridging forgotten words,
keyboard layout remapping for accidental cross-layout typing,
and a Sigma alphabet guard to validate queries.
"""

from __future__ import annotations

from .keyboard_layout import KeyboardLayoutMapper
from .models import (
    AdaptationMode,
    QueryAdaptationResult,
    SigmaPolicy,
    TokenAdaptation,
)
from .pipeline import InputAdaptationPipeline
from .sigma_guard import SigmaGuard, SigmaValidationResult
from .token_translator import TokenTranslator

__all__ = [
    "AdaptationMode",
    "InputAdaptationPipeline",
    "KeyboardLayoutMapper",
    "QueryAdaptationResult",
    "SigmaGuard",
    "SigmaPolicy",
    "SigmaValidationResult",
    "TokenAdaptation",
    "TokenTranslator",
]
