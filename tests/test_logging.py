from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index
from autocomplete_system.logging_config import (
    SYSTEM_LOG_FILENAME,
    configure_system_logging,
    log_event,
    shutdown_system_logging,
)


class SystemLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_exception_hook = sys.excepthook
        self.original_thread_exception_hook = threading.excepthook

    def tearDown(self) -> None:
        shutdown_system_logging()
        sys.excepthook = self.original_exception_hook
        threading.excepthook = self.original_thread_exception_hook

    def test_invalid_rotation_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            with self.assertRaises(ValueError):
                configure_system_logging(directory, max_bytes=0)
            with self.assertRaises(ValueError):
                configure_system_logging(directory, backup_count=-1)

    def test_repeated_configuration_reuses_the_existing_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = configure_system_logging(
                directory,
                force=True,
                install_exception_hooks=False,
            )
            second = configure_system_logging(
                directory / "ignored",
                install_exception_hooks=False,
            )
            self.assertEqual(first, second)
            owned_handlers = [
                handler
                for handler in logging.getLogger("autocomplete").handlers
                if getattr(handler, "_autocomplete_system_handler", False)
            ]
            self.assertEqual(len(owned_handlers), 1)
            shutdown_system_logging()

    def test_exception_information_and_uncaught_hooks_are_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = configure_system_logging(
                Path(temporary_directory),
                force=True,
                install_exception_hooks=True,
            )
            try:
                raise RuntimeError("logged failure")
            except RuntimeError:
                log_event(
                    logging.getLogger("autocomplete.test"),
                    "handled_failure",
                    exc_info=True,
                )

            try:
                raise ValueError("uncaught failure")
            except ValueError:
                exception_type, exception, traceback = sys.exc_info()
                assert exception_type is not None and exception is not None
                sys.excepthook(exception_type, exception, traceback)

            thread_args = SimpleNamespace(
                thread=threading.current_thread(),
                exc_type=LookupError,
                exc_value=LookupError("thread failure"),
                exc_traceback=None,
            )
            threading.excepthook(thread_args)

            events = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            handled = next(item for item in events if item["event"] == "handled_failure")
            self.assertIn("RuntimeError: logged failure", handled["exception"])
            self.assertTrue(any(item["event"] == "uncaught_exception" for item in events))
            self.assertTrue(
                any(item["event"] == "uncaught_thread_exception" for item in events)
            )
            shutdown_system_logging()

    def test_keyboard_interrupt_is_delegated_to_the_original_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(sys, "__excepthook__") as original_hook:
                configure_system_logging(
                    Path(temporary_directory),
                    force=True,
                    install_exception_hooks=True,
                )
                interrupt = KeyboardInterrupt()
                sys.excepthook(KeyboardInterrupt, interrupt, None)
                original_hook.assert_called_once_with(KeyboardInterrupt, interrupt, None)
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
