"""Keyboard layout remapping for handling accidental cross-layout typing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


DEFAULT_LAYOUT_FILE = Path(__file__).resolve().parent / "layouts" / "hebrew_qwerty.json"

DEFAULT_HEBREW_QWERTY_MAP: dict[str, str] = {
    "/": "q",
    "'": "w",
    "ק": "e",
    "ר": "r",
    "א": "t",
    "ט": "y",
    "ו": "u",
    "ן": "i",
    "ם": "o",
    "פ": "p",
    "ש": "a",
    "ד": "s",
    "ג": "d",
    "כ": "f",
    "ע": "g",
    "י": "h",
    "ח": "j",
    "ל": "k",
    "ך": "l",
    "ף": ";",
    ",": "'",
    "ז": "z",
    "ס": "x",
    "ב": "c",
    "ה": "v",
    "נ": "b",
    "מ": "n",
    "צ": "m",
    "ת": ",",
    "ץ": ".",
    ".": "/",
}


class KeyboardLayoutMapper:
    """Remaps characters typed with an incorrect physical keyboard layout."""

    def __init__(
        self,
        mapping: Mapping[str, str] | None = None,
        name: str = "hebrew_qwerty",
    ) -> None:
        self.name = name
        self.mapping = dict(mapping) if mapping is not None else dict(DEFAULT_HEBREW_QWERTY_MAP)

    @classmethod
    def from_file(cls, path: Path | str) -> KeyboardLayoutMapper:
        """Load a keyboard layout mapping from a local JSON file."""
        config_path = Path(path).resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"Keyboard layout file not found: {config_path}")

        data = json.loads(config_path.read_text(encoding="utf-8"))
        mapping = data.get("mapping", {})
        name = data.get("name", config_path.stem)
        return cls(mapping=mapping, name=name)

    @classmethod
    def load_default(cls) -> KeyboardLayoutMapper:
        """Load default layout from disk or fallback to built-in dictionary."""
        if DEFAULT_LAYOUT_FILE.is_file():
            try:
                return cls.from_file(DEFAULT_LAYOUT_FILE)
            except Exception:
                pass
        return cls(DEFAULT_HEBREW_QWERTY_MAP, name="hebrew_qwerty")

    def remap_char(self, char: str) -> str:
        """Remap a single character if found in layout, else return as-is."""
        return self.mapping.get(char, char)

    def remap_token(self, token: str) -> str:
        """Remap each character in a token string."""
        return "".join(self.remap_char(ch) for ch in token)

    def remap_text(self, text: str) -> str:
        """Remap each character in the entire text string."""
        return "".join(self.remap_char(ch) for ch in text)

    def is_candidate(self, text: str) -> bool:
        """Return True if text contains characters present in this layout's keys."""
        return any(ch in self.mapping for ch in text)
