"""Filesystem hand-off between offline snapshot builds and the online server.

ZDT ("Zero DownTime"): offline builds (``build_index.py``, and the Admin
``RebuildManager``) already write each replacement corpus to its own
versioned directory under ``rebuilds/`` without ever touching the directory
the running server currently serves from. This module adds the other half of
the hand-off:

- ``write_snapshot_pointer`` validates that a built directory actually
  contains a loadable index, then atomically publishes it as "current" by
  rewriting one small pointer file (write-then-rename, never a partial
  write). It can be called from the running server's own Admin action, or
  completely independently -- from a separate CLI invocation
  (``activate_snapshot.py``), on this machine or any other process with
  filesystem access to the same pointer path (for example a shared/remote
  volume), without the server ever being contacted directly.
- ``SnapshotWatcher`` runs in a running server, polling that same pointer
  file in the background. When it changes, the watcher loads the new
  snapshot and calls ``AutocompleteSystem.adopt_snapshot`` to swap it into
  the live, already-running system in place. In-flight requests keep being
  served by the previous snapshot until the swap; there is no restart and no
  window in which the server stops answering requests.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .constants import DEFAULT_SNAPSHOT_POLL_INTERVAL_SECONDS
from .engine import AutocompleteSystem
from .logging_config import log_event
from .storage import load_index

LOGGER = logging.getLogger("autocomplete.snapshot")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_write_text(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` so readers never observe a partial file."""

    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(data, encoding="utf-8")
    temporary_path.replace(path)


def write_snapshot_pointer(pointer_path: Path, data_directory: Path) -> dict[str, object]:
    """Validate ``data_directory`` and atomically publish it as the active snapshot.

    The snapshot is loaded once here purely to validate it (an incomplete or
    corrupt build can therefore never become the active pointer); whatever
    ``load_index`` raises -- ``FileNotFoundError`` for a missing/incomplete
    build, ``ValueError`` for a corrupted or unsupported one -- propagates
    unchanged and the pointer file is left untouched.
    """

    data_directory = Path(data_directory).resolve()
    index, master_array = load_index(data_directory)
    if hasattr(index, "close"):
        index.close()  # this was only a validation load; the watcher loads its own

    pointer_path = Path(pointer_path)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "data_directory": str(data_directory),
        "activated_at": _utc_now(),
        "sentence_count": len(master_array),
    }
    _atomic_write_text(
        pointer_path,
        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
    )
    log_event(
        LOGGER,
        "snapshot_pointer_activated",
        pointer_path=str(pointer_path),
        data_directory=record["data_directory"],
        sentence_count=record["sentence_count"],
    )
    return record


def read_snapshot_pointer(pointer_path: Path) -> dict[str, object] | None:
    """Return the published pointer record, or ``None`` if absent or invalid."""

    pointer_path = Path(pointer_path)
    if not pointer_path.is_file():
        return None
    try:
        record: Any = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("data_directory"), str):
        return None
    return record


class SnapshotWatcher:
    """Poll one pointer file and hot-swap a running system's active snapshot."""

    def __init__(
        self,
        pointer_path: Path,
        system: AutocompleteSystem,
        poll_interval_seconds: float = DEFAULT_SNAPSHOT_POLL_INTERVAL_SECONDS,
        on_activate: Callable[[Path], None] | None = None,
    ) -> None:
        self.pointer_path = Path(pointer_path)
        self.system = system
        self.poll_interval_seconds = poll_interval_seconds
        self.on_activate = on_activate
        self._active_directory = (
            Path(system.data_directory).resolve() if system.data_directory else None
        )
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def poll_once(self) -> bool:
        """Adopt a newly published snapshot, if the pointer changed. Returns True on swap."""

        record = read_snapshot_pointer(self.pointer_path)
        if record is None:
            return False
        target_directory = Path(str(record["data_directory"])).resolve()
        if target_directory == self._active_directory:
            return False
        try:
            index, master_array = load_index(target_directory)
        except (FileNotFoundError, ValueError) as error:
            log_event(
                LOGGER,
                "snapshot_activation_failed",
                level=logging.ERROR,
                pointer_path=str(self.pointer_path),
                data_directory=str(target_directory),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return False

        self.system.adopt_snapshot(index, master_array, target_directory)
        self._active_directory = target_directory
        log_event(
            LOGGER,
            "snapshot_activation_completed",
            pointer_path=str(self.pointer_path),
            data_directory=str(target_directory),
            sentence_count=len(master_array),
        )
        if self.on_activate is not None:
            self.on_activate(target_directory)
        return True

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_interval_seconds):
            try:
                self.poll_once()
            except Exception:
                log_event(
                    LOGGER,
                    "snapshot_watcher_cycle_failed",
                    level=logging.ERROR,
                    exc_info=True,
                )

    def start(self) -> None:
        """Start the background polling thread. Calling this twice is a no-op."""

        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="snapshot-watcher", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = 2.0) -> None:
        """Stop the background polling thread and wait for it to exit."""

        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        self._thread = None
