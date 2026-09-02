from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import autocomplete_system
from autocomplete_system.analytics import AdminService, AnalyticsStore, RebuildManager
from autocomplete_system import constants
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index, build_sqlite_index
from autocomplete_system.logging_config import (
    JsonLineFormatter,
    configure_system_logging,
    shutdown_system_logging,
)
from autocomplete_system.models import RankingMode
from autocomplete_system.sources import SourceLine
from autocomplete_system.sqlite_index import SQLiteSubstringIndex
from autocomplete_system.trie import CompressedSuffixTrie


class AnalyticsAndAdministrationEdgeTests(unittest.TestCase):
    def test_rebuild_status_survives_an_unreadable_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = RebuildManager(root, root / "source.txt", root / "rebuilds")
            log_path = root / "rebuild.log"
            log_path.write_text("partial output\n", encoding="utf-8")
            manager._log_path = log_path

            with patch.object(Path, "read_text", side_effect=OSError("access denied")):
                status = manager.status()

            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["log_tail"], [])

    def test_storage_deduplicates_the_same_resolved_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            shared_directory = root / "logs"
            shared_directory.mkdir()
            (shared_directory / "system.jsonl").write_text("{}\n", encoding="utf-8")
            index, master = build_index(root)
            system = AutocompleteSystem(index, master, data_directory=shared_directory)
            try:
                service = AdminService(system, AnalyticsStore(root / "analytics"), root)
                stored = service._storage()
            finally:
                system.close()

            matching = [item for item in stored if item["name"].endswith("system.jsonl")]
            self.assertEqual(len(matching), 1)

class ImportContractTests(unittest.TestCase):
    def test_public_exports_and_assignment_constants(self) -> None:
        self.assertEqual(
            autocomplete_system.__all__,
            ["AutoCompleteData", "AutocompleteSystem", "RankingMode"],
        )
        self.assertEqual(constants.MAX_NODE_CACHE_SIZE, 20)
        self.assertEqual(constants.ALPHA, 5)
        self.assertEqual(constants.DEFAULT_INPUT_SOURCES, (Path("Archive"),))
        self.assertEqual(constants.SQLITE_VARIANT_BATCH_SIZE, 100)

    def test_log_reader_exercises_component_search_and_limit_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "sample.txt").write_text("demo\n", encoding="utf-8")
            logs = root / "logs"
            logs.mkdir()
            events = [
                {"level": "INFO", "logger": "autocomplete.engine", "message": "first"},
                {"level": "ERROR", "logger": "autocomplete.web", "message": "second"},
            ]
            (logs / "system.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            index, master = build_index(corpus)
            system = AutocompleteSystem(index, master)
            try:
                service = AdminService(system, AnalyticsStore(root / "analytics"), root)
                self.assertEqual(
                    service.read_system_logs("system.jsonl", 10, component="missing")[
                        "record_count"
                    ],
                    0,
                )
                self.assertEqual(
                    service.read_system_logs("system.jsonl", 10, search="absent")[
                        "record_count"
                    ],
                    0,
                )
                limited = service.read_system_logs("system.jsonl", 1)
                self.assertEqual(limited["record_count"], 1)
            finally:
                system.close()


class IndexConstructionEdgeTests(unittest.TestCase):
    def test_large_sqlite_build_flushes_batches_reports_progress_and_removes_stale_temp(
        self,
    ) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.executemany_calls = 0
                self.commit_calls = 0
                self.closed = False

            def execute(self, *_args: object) -> FakeConnection:
                return self

            @staticmethod
            def fetchone() -> tuple[int]:
                return (100_000,)

            def executescript(self, *_args: object) -> FakeConnection:
                return self

            def executemany(self, _sql: str, rows: list[tuple[object, ...]]) -> None:
                self.executemany_calls += 1
                self.assert_batch_size(rows)

            @staticmethod
            def assert_batch_size(rows: list[tuple[object, ...]]) -> None:
                if len(rows) != 10_000:
                    raise AssertionError(f"unexpected batch size: {len(rows)}")

            def commit(self) -> None:
                self.commit_calls += 1

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("placeholder\n", encoding="utf-8")
            data_directory = root / "data"
            data_directory.mkdir()
            temporary_database = data_directory / "sentences.sqlite3.tmp"
            temporary_database.write_bytes(b"stale")
            connection = FakeConnection()
            progress = Mock()

            def source_lines(_sources: object):
                for line_number in range(1, 100_001):
                    yield SourceLine("!!!", "source.txt", line_number)

            def connect(path: Path) -> FakeConnection:
                Path(path).touch()
                return connection

            with (
                patch("autocomplete_system.indexer.iter_source_lines", source_lines),
                patch("autocomplete_system.indexer.sqlite3.connect", side_effect=connect),
                patch("autocomplete_system.indexer.log_event") as log_event,
            ):
                index, master = build_sqlite_index(source, data_directory, progress)

            self.assertEqual(len(master), 100_000)
            self.assertEqual(connection.executemany_calls, 20)
            self.assertEqual(connection.commit_calls, 11)
            self.assertTrue(connection.closed)
            self.assertFalse(temporary_database.exists())
            self.assertTrue((data_directory / "sentences.sqlite3").is_file())
            progress.assert_called_once_with(100_000)
            self.assertTrue(
                any(
                    call.args[1] == "sqlite_index_build_progress"
                    for call in log_event.call_args_list
                )
            )
            index.close()


class SQLiteSearchEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = SQLiteSubstringIndex(
            "sentences.sqlite3",
            ("a", "b", "c", "d", "e", "f", "g"),
            {"a": [7], "b": [8]},
            {"a": [9], "b": [10]},
        )

    def test_empty_long_query_and_unanchored_wildcard_need_no_database(self) -> None:
        self.assertEqual(
            self.index._query_long_variants([], RankingMode.ASSIGNMENT), []
        )
        self.assertEqual(
            self.index._query_single_wildcard("a", "b", RankingMode.POPULARITY),
            [],
        )

    def test_assignment_wildcard_uses_assignment_table(self) -> None:
        connection = Mock()
        connection.execute.return_value = [(42,)]
        with patch.object(SQLiteSubstringIndex, "_connect", return_value=connection):
            result = self.index._query_single_wildcard(
                "abc", "def", RankingMode.ASSIGNMENT
            )

        self.assertEqual(result, [42])
        self.assertIn("assignment_fts", connection.execute.call_args.args[0])

    def test_short_exact_query_uses_the_in_memory_cache(self) -> None:
        result = self.index.candidate_text_scores("a", RankingMode.ASSIGNMENT)
        self.assertEqual(result[7], 2)

    def test_pattern_search_uses_short_deleted_variants(self) -> None:
        result = self.index._candidate_scores_from_patterns(
            "ab",
            RankingMode.POPULARITY,
            {},
            {"a": [1], "b": [2]},
        )
        self.assertIn(1, result)
        self.assertIn(2, result)

    def test_assignment_pattern_search_stops_after_five_final_candidates(self) -> None:
        candidates = [1, 2, 3, 4, 5]
        with (
            patch.object(SQLiteSubstringIndex, "_query_long_variants", return_value=[]),
            patch.object(
                SQLiteSubstringIndex,
                "_query_single_wildcard",
                return_value=candidates,
            ),
        ):
            result = self.index._candidate_scores_from_patterns(
                "abcdefg", RankingMode.ASSIGNMENT, {}, {}
            )

        self.assertEqual(set(result), set(candidates))

    def test_short_variant_search_stops_after_five_final_candidates(self) -> None:
        def long_variants(
            _index: SQLiteSubstringIndex,
            variants: list[str],
            _mode: RankingMode,
        ) -> list[int]:
            return [] if variants == ["abc"] else [1, 2, 3, 4, 5]

        with patch.object(SQLiteSubstringIndex, "_query_long_variants", long_variants):
            result = self.index.candidate_text_scores("abc", RankingMode.ASSIGNMENT)

        self.assertEqual(set(result), {1, 2, 3, 4, 5})


class TrieAndLoggingEdgeTests(unittest.TestCase):
    def tearDown(self) -> None:
        shutdown_system_logging()

    def test_empty_suffix_is_ignored_and_repeated_state_is_pruned(self) -> None:
        trie = CompressedSuffixTrie()
        key = lambda sentence_id: (sentence_id,)
        trie.insert_suffix("", 0, key, key)
        self.assertEqual(trie.root.children, {})

        trie.insert_sentence("aaaaa", 0, key, key)
        self.assertIn(
            0,
            trie.candidate_text_scores("aaaaaa", RankingMode.ASSIGNMENT),
        )

    def test_formatter_accepts_preformatted_exception_text(self) -> None:
        record = logging.LogRecord(
            "autocomplete.test",
            logging.ERROR,
            __file__,
            1,
            "failure",
            (),
            None,
        )
        record.exc_text = "already formatted"
        payload = json.loads(JsonLineFormatter().format(record))
        self.assertEqual(payload["exception"], "already formatted")

    def test_thread_exception_hook_serializes_a_real_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = configure_system_logging(
                Path(temporary_directory),
                force=True,
                install_exception_hooks=True,
            )
            try:
                raise LookupError("thread traceback")
            except LookupError:
                exception_type, exception, traceback = sys.exc_info()
                assert exception_type is not None and exception is not None
                args = SimpleNamespace(
                    thread=None,
                    exc_type=exception_type,
                    exc_value=exception,
                    exc_traceback=traceback,
                )
                __import__("threading").excepthook(args)

            events = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            event = next(
                item for item in events if item["event"] == "uncaught_thread_exception"
            )
            self.assertIn("LookupError: thread traceback", event["exception"])
            shutdown_system_logging()


if __name__ == "__main__":
    unittest.main()
