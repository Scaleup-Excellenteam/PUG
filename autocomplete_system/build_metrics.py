"""Persistent, machine-readable measurements for an index build."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BUILD_METRICS_FILENAME = "build_metrics.json"
BUILD_METRICS_VERSION = 1


def directory_size(directory: Path) -> int:
    """Return the current size of every regular file below ``directory``."""

    root = Path(directory)
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def read_build_metrics(data_directory: Path) -> dict[str, Any] | None:
    path = Path(data_directory) / BUILD_METRICS_FILENAME
    if not path.is_file():
        return None
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != BUILD_METRICS_VERSION:
        return None
    return payload


def write_build_metrics(data_directory: Path, metrics: dict[str, object]) -> Path:
    """Atomically persist build metrics and include the metadata file in its size."""

    directory = Path(data_directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / BUILD_METRICS_FILENAME
    payload = {
        "version": BUILD_METRICS_VERSION,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **metrics,
    }
    for _ in range(3):
        payload["output_bytes"] = directory_size(directory)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
    return path
