"""Text normalization shared by indexing and querying."""

import re
import string
import unicodedata

_WHITESPACE_PATTERN = re.compile(r"\s+")
_ASCII_PUNCTUATION = frozenset(string.punctuation)


def normalize_text(text: str) -> str:
    """Lowercase text, delete punctuation, and collapse whitespace."""

    without_punctuation = "".join(
        character
        for character in text.lower()
        if character not in _ASCII_PUNCTUATION
        and not unicodedata.category(character).startswith("P")
    )
    return _WHITESPACE_PATTERN.sub(" ", without_punctuation).strip()
