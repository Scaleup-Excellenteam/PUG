"""Tests for the ZDT filesystem hand-off: pointer read/write and the watcher."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index
from autocomplete_system.snapshot import (
    SnapshotWatcher,
    read_snapshot_pointer,
    write_snapshot_pointer,
)
from autocomplete_system.storage import save_index


def _build_and_save(root: Path, name: str, text: str) -> Path:
    corpus = root / f"{name}-corpus"
    corpus.mkdir()
    (corpus / "lines.txt").write_text(text, encoding="utf-8")
    index, master_array = build_index(corpus)
    data_directory = root / f"{name}-data"
    save_index(data_directory, index, master_array)
    return data_directory


class WriteSnapshotPointerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_write_rejects_a_directory_with_no_built_index(self) -> None:
        pointer_path = self.root / "active_snapshot.json"
        with self.assertRaises(FileNotFoundError):
            write_snapshot_pointer(pointer_path, self.root / "never-built")
        self.assertFalse(pointer_path.exists())

    def test_write_publishes_an_atomic_pointer_file(self) -> None:
        data_directory = _build_and_save(self.root, "alpha", "Alpha one.\nAlpha two.\n")
        pointer_path = self.root / "rebuilds" / "active_snapshot.json"

        record = write_snapshot_pointer(pointer_path, data_directory)

        self.assertTrue(pointer_path.is_file())
        self.assertFalse(pointer_path.with_suffix(".json.tmp").exists())
        on_disk = json.loads(pointer_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["data_directory"], str(data_directory.resolve()))
        self.assertEqual(on_disk["sentence_count"], 2)
        self.assertIn("activated_at", on_disk)
        self.assertEqual(record, on_disk)

    def test_write_overwrites_a_previous_pointer(self) -> None:
        first = _build_and_save(self.root, "alpha", "Alpha only.\n")
        second = _build_and_save(self.root, "beta", "Beta one.\nBeta two.\nBeta three.\n")
        pointer_path = self.root / "active_snapshot.json"

        write_snapshot_pointer(pointer_path, first)
        write_snapshot_pointer(pointer_path, second)

        record = read_snapshot_pointer(pointer_path)
        assert record is not None
        self.assertEqual(record["data_directory"], str(second.resolve()))
        self.assertEqual(record["sentence_count"], 3)


class ReadSnapshotPointerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_missing_pointer_returns_none(self) -> None:
        self.assertIsNone(read_snapshot_pointer(self.root / "missing.json"))

    def test_malformed_pointer_is_tolerated_and_returns_none(self) -> None:
        pointer_path = self.root / "active_snapshot.json"
        pointer_path.write_text("not json", encoding="utf-8")
        self.assertIsNone(read_snapshot_pointer(pointer_path))

    def test_pointer_missing_data_directory_key_returns_none(self) -> None:
        pointer_path = self.root / "active_snapshot.json"
        pointer_path.write_text(json.dumps({"activated_at": "now"}), encoding="utf-8")
        self.assertIsNone(read_snapshot_pointer(pointer_path))


class SnapshotWatcherPollOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.initial_directory = _build_and_save(self.root, "initial", "Initial only.\n")
        self.system = AutocompleteSystem.load(self.initial_directory)
        self.pointer_path = self.root / "active_snapshot.json"

    def tearDown(self) -> None:
        self.system.close()
        self.temporary_directory.cleanup()

    def test_poll_once_without_a_pointer_does_nothing(self) -> None:
        watcher = SnapshotWatcher(self.pointer_path, self.system)
        self.assertFalse(watcher.poll_once())
        self.assertEqual(self.system.data_directory, self.initial_directory)

    def test_poll_once_swaps_in_a_newly_published_snapshot(self) -> None:
        new_directory = _build_and_save(self.root, "second", "Second alpha.\nSecond beta.\n")
        write_snapshot_pointer(self.pointer_path, new_directory)

        activated: list[Path] = []
        watcher = SnapshotWatcher(
            self.pointer_path, self.system, on_activate=activated.append
        )
        swapped = watcher.poll_once()

        self.assertTrue(swapped)
        self.assertEqual(self.system.data_directory, new_directory.resolve())
        self.assertEqual(
            self.system.get_best_k_completions("second")[0].completed_sentence,
            "Second alpha.",
        )
        self.assertEqual(activated, [new_directory.resolve()])

    def test_poll_once_is_a_noop_once_already_active(self) -> None:
        new_directory = _build_and_save(self.root, "second", "Second alpha.\n")
        write_snapshot_pointer(self.pointer_path, new_directory)
        watcher = SnapshotWatcher(self.pointer_path, self.system)

        self.assertTrue(watcher.poll_once())
        self.assertFalse(watcher.poll_once())

    def test_poll_once_ignores_a_pointer_naming_a_directory_that_no_longer_exists(self) -> None:
        pointer_path = self.pointer_path
        vanished = self.root / "vanished-data"
        vanished.mkdir()
        write_snapshot_pointer(pointer_path, self.initial_directory)
        # Simulate the pointer having been hand-edited to a bogus location.
        pointer_path.write_text(
            json.dumps({"data_directory": str(vanished), "activated_at": "now"}),
            encoding="utf-8",
        )
        watcher = SnapshotWatcher(pointer_path, self.system)

        self.assertFalse(watcher.poll_once())
        self.assertEqual(self.system.data_directory, self.initial_directory)


class SnapshotWatcherThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.initial_directory = _build_and_save(self.root, "initial", "Initial only.\n")
        self.system = AutocompleteSystem.load(self.initial_directory)
        self.pointer_path = self.root / "active_snapshot.json"

    def tearDown(self) -> None:
        self.system.close()
        self.temporary_directory.cleanup()

    def test_background_thread_picks_up_a_pointer_change_without_a_restart(self) -> None:
        watcher = SnapshotWatcher(self.pointer_path, self.system, poll_interval_seconds=0.02)
        watcher.start()
        try:
            new_directory = _build_and_save(self.root, "live", "Live update sentence.\n")
            write_snapshot_pointer(self.pointer_path, new_directory)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.system.data_directory == new_directory.resolve():
                    break
                time.sleep(0.02)
        finally:
            watcher.stop()

        self.assertEqual(self.system.data_directory, new_directory.resolve())
        self.assertEqual(
            self.system.get_best_k_completions("live update")[0].completed_sentence,
            "Live update sentence.",
        )

    def test_stop_joins_the_thread_and_start_is_idempotent(self) -> None:
        watcher = SnapshotWatcher(self.pointer_path, self.system, poll_interval_seconds=0.02)
        watcher.start()
        watcher.start()  # second start must not raise or spawn a second thread
        watcher.stop()
        self.assertIsNone(watcher._thread)
        watcher.stop()  # idempotent


if __name__ == "__main__":
    unittest.main()
