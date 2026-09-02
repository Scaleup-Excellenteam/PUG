"""Dynamic input discovery and reproducible corpus fingerprints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any


SOURCE_MANIFEST_FILENAME = "source_manifest.json"
SOURCE_MANIFEST_VERSION = 1
SUPPORTED_SOURCE_SUFFIXES = frozenset({".txt", ".zip"})


def discover_source_files(sources: Sequence[Path]) -> list[tuple[str, Path]]:
    """Return every configured TXT/ZIP source with a stable display key."""

    discovered: list[tuple[str, Path]] = []
    for source_number, raw_source in enumerate(sources):
        source = Path(raw_source).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input source does not exist: {source}")
        prefix = f"{source_number}:{source.name}"
        if source.is_dir():
            for path in sorted(source.rglob("*")):
                if path.is_file() and path.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES:
                    discovered.append(
                        (f"{prefix}/{path.relative_to(source).as_posix()}", path)
                    )
        elif source.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES:
            discovered.append((prefix, source))
        else:
            raise ValueError(f"Unsupported input source: {source}")
    return discovered


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        while chunk := source_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_source_manifest(data_directory: Path) -> dict[str, Any] | None:
    path = Path(data_directory) / SOURCE_MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != SOURCE_MANIFEST_VERSION:
        return None
    if not isinstance(payload.get("files"), list):
        return None
    return payload


def build_source_manifest(
    sources: Sequence[Path],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fingerprint inputs, reusing hashes whose size and mtime are unchanged."""

    previous_by_key = {
        str(item.get("key")): item
        for item in (previous or {}).get("files", [])
        if isinstance(item, dict)
    }
    files = []
    for key, path in discover_source_files(sources):
        stat = path.stat()
        prior = previous_by_key.get(key)
        if (
            prior is not None
            and prior.get("size") == stat.st_size
            and prior.get("mtime_ns") == stat.st_mtime_ns
            and isinstance(prior.get("sha256"), str)
        ):
            digest = str(prior["sha256"])
        else:
            digest = _sha256(path)
        files.append(
            {
                "key": key,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": digest,
            }
        )
    return {
        "version": SOURCE_MANIFEST_VERSION,
        "files": files,
        "total_bytes": sum(int(item["size"]) for item in files),
    }


def manifests_match(left: dict[str, Any] | None, right: dict[str, Any]) -> bool:
    if left is None:
        return False
    return left.get("version") == right.get("version") and left.get("files") == right.get("files")


def write_source_manifest(data_directory: Path, manifest: dict[str, Any]) -> Path:
    path = Path(data_directory) / SOURCE_MANIFEST_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
