"""Physical keyboard layout remapping supporting dynamic, language-agnostic multi-layouts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

DEFAULT_LAYOUTS_DIRECTORY = Path(__file__).parent / "layouts"


BIDI_CONTROL_CHARS = {
    "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff", "\u200b",
}


class KeyboardLayoutMapper:
    """Remaps text typed on incorrect physical keyboard layouts by dynamically loading layout maps."""

    def __init__(
        self,
        char_map: Mapping[str, str] | None = None,
        name: str = "Empty Layout Mapper",
        loaded_layouts: Sequence[str] | None = None,
    ) -> None:
        self.char_map: dict[str, str] = dict(char_map) if char_map is not None else {}
        self.name = name
        self._source_keys = set(self.char_map.keys())
        self.loaded_layouts: list[str] = (
            list(loaded_layouts)
            if loaded_layouts
            else ([name] if name and name != "Empty Layout Mapper" else [])
        )

    @classmethod
    def load_all_available(cls, layouts_directory: Path | str | None = None) -> KeyboardLayoutMapper:
        """Scan layouts directory and load all available JSON keymaps into a unified multi-layout mapper."""
        target_dir = Path(layouts_directory) if layouts_directory is not None else DEFAULT_LAYOUTS_DIRECTORY
        combined_map: dict[str, str] = {}
        names: list[str] = []

        if target_dir.is_dir():
            for json_file in sorted(target_dir.glob("*.json")):
                try:
                    content = json.loads(json_file.read_text(encoding="utf-8"))
                    mapping = content.get("mapping", {})
                    name = content.get("name", json_file.stem)
                    combined_map.update(mapping)
                    names.append(name)
                except Exception:
                    pass

        if not combined_map:
            return cls({}, name="Empty Layout Mapper", loaded_layouts=[])

        return cls(
            combined_map,
            name=", ".join(names),
            loaded_layouts=names,
        )

    @classmethod
    def load_default(cls) -> KeyboardLayoutMapper:
        """Load default mapper by dynamically loading all layout files from the layouts/ directory."""
        return cls.load_all_available()

    @classmethod
    def from_file(cls, path: Path | str) -> KeyboardLayoutMapper:
        """Load mapping dictionary from a local JSON configuration file."""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Keyboard layout file not found: {file_path}")

        content = json.loads(file_path.read_text(encoding="utf-8"))
        mapping = content.get("mapping", {})
        name = content.get("name", file_path.stem)
        return cls(mapping, name=name, loaded_layouts=[name])

    def add_layout_from_file(self, path: Path | str) -> None:
        """Dynamically add or merge another layout from a JSON file."""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Keyboard layout file not found: {file_path}")

        content = json.loads(file_path.read_text(encoding="utf-8"))
        mapping = content.get("mapping", {})
        name = content.get("name", file_path.stem)
        self.char_map.update(mapping)
        self._source_keys.update(mapping.keys())
        self.loaded_layouts.append(name)
        self.name = ", ".join(self.loaded_layouts)

    def remap_char(self, char: str) -> str:
        """Remap a single character according to active layout table."""
        return self.char_map.get(char, char)

    def remap_text(self, text: str) -> str:
        """Remap all matching characters across a full query string, stripping any invisible bidi marks."""
        cleaned = "".join(ch for ch in text if ch not in BIDI_CONTROL_CHARS)
        return "".join(self.char_map.get(ch, ch) for ch in cleaned)

    def is_candidate(self, text: str) -> bool:
        """Return True if text contains any characters belonging to source layout."""
        return any(ch in self._source_keys for ch in text)
