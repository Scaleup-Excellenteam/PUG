"""Input adaptation pipeline coordinating translation, keyboard remapping, and alphabet guard."""

from __future__ import annotations

import logging
from pathlib import Path

from autocomplete_system.logging_config import log_event
from .keyboard_layout import KeyboardLayoutMapper
from .models import AdaptationMode, QueryAdaptationResult, SigmaPolicy, TokenAdaptation
from .sigma_guard import SigmaGuard
from .token_translator import TokenTranslator

LOGGER = logging.getLogger("autocomplete.translation")


class InputAdaptationPipeline:
    """Coordinates query adaptation before running search."""

    def __init__(
        self,
        enable_keymap: bool = True,
        enable_translate: bool = False,
        sigma_policy: SigmaPolicy = SigmaPolicy.WARN,
        mode: AdaptationMode | None = None,
        translator: TokenTranslator | None = None,
        keymapper: KeyboardLayoutMapper | None = None,
        guard: SigmaGuard | None = None,
    ) -> None:
        if mode is not None:
            self.enable_keymap = (mode is AdaptationMode.KEYBOARD_REMAP)
            self.enable_translate = (mode is AdaptationMode.TRANSLATE)
        else:
            self.enable_keymap = enable_keymap
            self.enable_translate = enable_translate

        self.sigma_policy = sigma_policy
        self.translator = translator if translator is not None else TokenTranslator()
        self.keymapper = keymapper if keymapper is not None else KeyboardLayoutMapper.load_default()
        self.guard = guard if guard is not None else SigmaGuard(default_policy=sigma_policy)

    @property
    def mode(self) -> AdaptationMode:
        """Return backward-compatible single mode enum."""
        if self.enable_translate:
            return AdaptationMode.TRANSLATE
        if self.enable_keymap:
            return AdaptationMode.KEYBOARD_REMAP
        return AdaptationMode.OFF

    def set_mode(self, mode: AdaptationMode | str) -> None:
        """Update adaptation mode (for backward compatibility)."""
        m = AdaptationMode(mode)
        if m is AdaptationMode.OFF:
            self.enable_keymap = False
            self.enable_translate = False
        elif m is AdaptationMode.KEYBOARD_REMAP:
            self.enable_keymap = True
            self.enable_translate = False
        elif m is AdaptationMode.TRANSLATE:
            self.enable_translate = True

    def toggle_keymap(self) -> bool:
        """Toggle keyboard layout remapping on or off."""
        self.enable_keymap = not self.enable_keymap
        return self.enable_keymap

    def toggle_translate(self) -> bool:
        """Toggle token-level Google translation on or off."""
        self.enable_translate = not self.enable_translate
        return self.enable_translate

    def set_sigma_policy(self, policy: SigmaPolicy | str) -> None:
        """Update active Sigma guard policy."""
        self.sigma_policy = SigmaPolicy(policy)
        self.guard.policy = self.sigma_policy

    def load_keymap_file(self, path: Path | str) -> None:
        """Load a custom keyboard layout configuration from file."""
        self.keymapper = KeyboardLayoutMapper.from_file(path)

    def process(self, query: str) -> QueryAdaptationResult:
        """Process a query through keyboard remapping, translation, and Sigma guard."""
        current_query = query
        keymap_applied = False
        translation_applied = False
        tokens: list[TokenAdaptation] = []
        detected_languages: list[str] = []

        # 1. Local Keyboard Remapping (recovers accidental layout switches, e.g. יקךךם -> hello)
        if self.enable_keymap and self.keymapper.is_candidate(current_query):
            remapped = self.keymapper.remap_text(current_query)
            if remapped != current_query:
                tokens.append(
                    TokenAdaptation(
                        original=current_query,
                        adapted=remapped,
                        was_adapted=True,
                        method="keymap",
                    )
                )
                current_query = remapped
                keymap_applied = True
                log_event(
                    LOGGER,
                    "input_remapped",
                    original_query=query,
                    remapped_query=current_query,
                    method="keyboard_layout",
                )

        # 2. Remote Token Translation (bridges foreign/forgotten words)
        if self.enable_translate:
            translated_query, trans_tokens, trans_langs = self.translator.translate_tokens(current_query)
            if translated_query != current_query:
                tokens.extend(trans_tokens)
                detected_languages.extend(trans_langs)
                current_query = translated_query
                translation_applied = True
                log_event(
                    LOGGER,
                    "tokens_translated",
                    original_query=query,
                    translated_query=current_query,
                    detected_languages=sorted(set(detected_languages)),
                )

        # 3. Validate against alphabet Sigma
        validation = self.guard.validate(current_query, policy=self.sigma_policy)
        if validation.violations:
            log_event(
                LOGGER,
                "sigma_violations_detected",
                level=logging.WARNING if validation.is_blocked else logging.INFO,
                query=current_query,
                violations=validation.violations,
                policy=self.sigma_policy.value,
                is_blocked=validation.is_blocked,
            )

        was_adapted = keymap_applied or translation_applied
        return QueryAdaptationResult(
            original_query=query,
            final_query=current_query,
            was_adapted=was_adapted,
            keymap_applied=keymap_applied,
            translation_applied=translation_applied,
            mode=self.mode,
            tokens=tokens,
            detected_languages=sorted(set(detected_languages)),
            sigma_violations=validation.violations,
            is_blocked=validation.is_blocked,
            warning_message=validation.warning,
        )
