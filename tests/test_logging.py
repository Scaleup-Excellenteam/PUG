from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index
from autocomplete_system.logging_config import (
    SYSTEM_LOG_FILENAME,
    configure_system_logging,
    log_event,
    shutdown_system_logging,
)


class SystemLoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        shutdown_system_logging()

    def test_events_are_valid_utf8_json_and_flush_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_directory = Path(temporary_directory)
            path = configure_system_logging(
                log_directory,
                force=True,
                install_exception_hooks=False,
            )
            log_event(
                logging.getLogger("autocomplete.test"),
                "test_event",
                query="שלום demo",
                results=[{"sentence": "A complete result.", "score": 8}],
            )

            lines = path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[-1])
            self.assertEqual(event["event"], "test_event")
            self.assertEqual(event["details"]["query"], "שלום demo")
            self.assertEqual(
                event["details"]["results"][0]["sentence"],
                "A complete result.",
            )
            shutdown_system_logging()

    def test_log_rotates_by_size_and_keeps_configured_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_directory = Path(temporary_directory)
            configure_system_logging(
                log_directory,
                max_bytes=700,
                backup_count=2,
                force=True,
                install_exception_hooks=False,
            )
            logger = logging.getLogger("autocomplete.rotation")
            for number in range(30):
                log_event(logger, "rotation_event", sequence=number, payload="x" * 180)

            self.assertTrue((log_directory / SYSTEM_LOG_FILENAME).is_file())
            self.assertTrue((log_directory / f"{SYSTEM_LOG_FILENAME}.1").is_file())
            self.assertTrue((log_directory / f"{SYSTEM_LOG_FILENAME}.2").is_file())
            self.assertFalse((log_directory / f"{SYSTEM_LOG_FILENAME}.3").exists())
            shutdown_system_logging()

    def test_search_log_contains_full_query_and_full_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            log_directory = root / "logs"
            path = configure_system_logging(
                log_directory,
                force=True,
                install_exception_hooks=False,
            )
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "example.txt").write_text(
                "The complete demo sentence!\n",
                encoding="utf-8",
            )
            index, master = build_index(corpus)
            system = AutocompleteSystem(index, master)
            try:
                system.get_ranked_completions("demo")
            finally:
                system.close()

            events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            search = next(event for event in events if event["event"] == "search_completed")
            self.assertEqual(search["details"]["query"], "demo")
            self.assertEqual(
                search["details"]["results"][0]["completed_sentence"],
                "The complete demo sentence!",
            )
            shutdown_system_logging()


if __name__ == "__main__":
    unittest.main()
