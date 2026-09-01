"""Token-level translation using Google Cloud Translation API to bridge forgotten words."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable
from .models import TokenAdaptation


LOGGER = logging.getLogger("autocomplete.translation")

# Pattern splits into words (\w+), whitespace (\s+), and punctuation ([^\w\s])
TOKEN_SPLIT_REGEX = re.compile(r"(\w+|[^\w\s]+|\s+)", re.UNICODE)


def is_foreign_token(token: str) -> bool:
    """Return True if token contains non-ASCII alphabetic characters needing translation."""
    return any(ord(ch) > 127 and ch.isalpha() for ch in token)


class TokenTranslator:
    """Translates individual non-English words using Google Cloud Translation API."""

    def __init__(
        self,
        api_key: str | None = None,
        custom_translate_fn: Callable[[list[str], str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_TRANSLATE_API_KEY")
        self._cache: dict[str, tuple[str, str]] = {}  # token -> (translated_text, detected_lang)
        self._custom_translate_fn = custom_translate_fn
        self._client: Any = None
        self._client_initialized = False

    def is_service_available(self) -> bool:
        """Check if Google Translation client or custom translator is available."""
        if self._custom_translate_fn is not None:
            return True
        if self.api_key or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            return True
        try:
            from google.cloud import translate_v2  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_client(self) -> Any:
        """Lazily initialize and return the Google Cloud Translation client."""
        if self._client_initialized:
            return self._client
        self._client_initialized = True
        if self._custom_translate_fn is not None:
            return None
        try:
            from google.cloud import translate_v2 as translate
            if self.api_key:
                self._client = translate.Client(target_language="en", credentials=None)
            else:
                self._client = translate.Client(target_language="en")
        except Exception as error:
            LOGGER.warning(
                "Google Cloud Translation client could not be initialized: %s",
                error,
            )
            self._client = None
        return self._client

    def _call_api(self, words: list[str], target_language: str = "en") -> list[dict[str, Any]]:
        """Perform batch translation for a list of words."""
        if not words:
            return []

        if self._custom_translate_fn is not None:
            return self._custom_translate_fn(words, target_language)

        client = self._get_client()
        if client is None:
            LOGGER.info(
                "Google Translation service unavailable; bypassing translation for tokens: %s",
                words,
            )
            return [
                {
                    "translatedText": word,
                    "detectedSourceLanguage": "und",
                }
                for word in words
            ]

        try:
            results = client.translate(words, target_language=target_language)
            if isinstance(results, dict):
                results = [results]
            return results
        except Exception as error:
            LOGGER.warning("Google Translation API request failed: %s", error)
            return [
                {
                    "translatedText": word,
                    "detectedSourceLanguage": "und",
                }
                for word in words
            ]

    def translate_tokens(
        self,
        query: str,
        target_language: str = "en",
    ) -> tuple[str, list[TokenAdaptation], list[str]]:
        """Translate only foreign tokens in a query, leaving English tokens and formatting intact."""
        raw_tokens = TOKEN_SPLIT_REGEX.findall(query)
        if not raw_tokens:
            return query, [], []

        # Identify unique foreign tokens that need translation
        uncached_words: list[str] = []
        for token in raw_tokens:
            if is_foreign_token(token) and token not in self._cache:
                if token not in uncached_words:
                    uncached_words.append(token)

        # Batch-translate uncached tokens
        if uncached_words:
            api_results = self._call_api(uncached_words, target_language=target_language)
            for word, result in zip(uncached_words, api_results):
                translated = result.get("translatedText", word).strip()
                detected = result.get("detectedSourceLanguage", "unknown")
                self._cache[word] = (translated, detected)

        # Reconstruct query token by token
        adapted_tokens: list[TokenAdaptation] = []
        reconstructed_parts: list[str] = []
        detected_languages: set[str] = set()

        for token in raw_tokens:
            if is_foreign_token(token):
                translated_text, detected_lang = self._cache.get(token, (token, "unknown"))
                was_adapted = (translated_text.lower() != token.lower())
                if was_adapted and detected_lang not in {"und", "unknown"}:
                    detected_languages.add(detected_lang)
                adapted_tokens.append(
                    TokenAdaptation(
                        original=token,
                        adapted=translated_text,
                        was_adapted=was_adapted,
                        method="translate",
                        detected_language=detected_lang if was_adapted else None,
                    )
                )
                reconstructed_parts.append(translated_text)
            else:
                adapted_tokens.append(
                    TokenAdaptation(
                        original=token,
                        adapted=token,
                        was_adapted=False,
                        method="identity",
                    )
                )
                reconstructed_parts.append(token)

        final_query = "".join(reconstructed_parts)
        return final_query, adapted_tokens, sorted(detected_languages)
