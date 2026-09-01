"""Input adaptation pipeline coordinating translation, keyboard remapping, and alphabet guard."""

from __future__ import annotations

from pathlib import Path
from .keyboard_layout import KeyboardLayoutMapper
from .models import AdaptationMode, QueryAdaptationResult, SigmaPolicy, TokenAdaptation
from .sigma_guard import SigmaGuard
from .token_translator import TokenTranslator


class InputAdaptationPipeline:
    """Coordinates query adaptation before running search."""

    def __init__(
        self,
        mode: AdaptationMode = AdaptationMode.OFF,
        sigma_policy: SigmaPolicy = SigmaPolicy.WARN,
        translator: TokenTranslator | None = None,
        keymapper: KeyboardLayoutMapper | None = None,
        guard: SigmaGuard | None = None,
    ) -> None:
        self.mode = mode
        self.sigma_policy = sigma_policy
        self.translator = translator if translator is not None else TokenTranslator()
        self.keymapper = keymapper if keymapper is not None else KeyboardLayoutMapper.load_default()
        self.guard = guard if guard is not None else SigmaGuard(default_policy=sigma_policy)

    def set_mode(self, mode: AdaptationMode | str) -> None:
        """Update active adaptation mode."""
        self.mode = AdaptationMode(mode)

    def set_sigma_policy(self, policy: SigmaPolicy | str) -> None:
        """Update active Sigma guard policy."""
        self.sigma_policy = SigmaPolicy(policy)
        self.guard.policy = self.sigma_policy

    def load_keymap_file(self, path: Path | str) -> None:
        """Load a custom keyboard layout configuration from file."""
        self.keymapper = KeyboardLayoutMapper.from_file(path)

    def process(self, query: str) -> QueryAdaptationResult:
        """Process a query through the active adaptation mode and Sigma guard."""
        current_query = query
        was_adapted = False
        tokens: list[TokenAdaptation] = []
        detected_languages: list[str] = []

        # 1. Apply adaptation (if enabled)
        if self.mode is AdaptationMode.TRANSLATE:
            current_query, tokens, detected_languages = self.translator.translate_tokens(query)
            was_adapted = (current_query != query)

        elif self.mode is AdaptationMode.KEYBOARD_REMAP:
            if self.keymapper.is_candidate(query):
                current_query = self.keymapper.remap_text(query)
                was_adapted = (current_query != query)
                tokens = [
                    TokenAdaptation(
                        original=query,
                        adapted=current_query,
                        was_adapted=was_adapted,
                        method="keymap",
                    )
                ]

        # 2. Validate against alphabet Sigma
        validation = self.guard.validate(current_query, policy=self.sigma_policy)

        return QueryAdaptationResult(
            original_query=query,
            final_query=current_query,
            was_adapted=was_adapted,
            mode=self.mode,
            tokens=tokens,
            detected_languages=detected_languages,
            sigma_violations=validation.violations,
            is_blocked=validation.is_blocked,
            warning_message=validation.warning,
        )
