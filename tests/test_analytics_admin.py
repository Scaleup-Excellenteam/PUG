from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autocomplete_system.analytics import AdminService, AnalyticsStore, RebuildManager
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index
from autocomplete_system.models import RankingMode
from autocomplete_system.storage import save_index
from autocomplete_system.trie import CompressedSuffixTrie


class AnalyticsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = AnalyticsStore(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_summary_exports_and_malformed_lines_cover_complete_analytics_flow(self) -> None:
        self.store.record(
            "search",
            timestamp="2026-01-01T10:05:00+00:00",
            query="Demo",
            normalized_query="demo",
            input_method="typed",
            result_count=0,
            duration_ms=10.0,
            results=[],
        )
        self.store.record(
            "search",
            timestamp="2026-01-01T10:45:00+00:00",
            query="demo",
            normalized_query="demo",
            input_method="voice",
            result_count=1,
            duration_ms=20.0,
            results=[{"sentence_id": 4, "score": 8}],
        )
        self.store.record(
            "selection",
            sentence_id=4,
            completed_sentence="demo",
            source_text="sample.txt",
            offset=7,
        )
        with self.store.path.open("a", encoding="utf-8") as event_file:
            event_file.write("not-json\n[]\n")

        events, malformed = self.store.read_events()
        self.assertEqual(len(events), 3)
        self.assertEqual(malformed, 2)
        summary = self.store.summary()
        self.assertEqual(summary["event_count"], 3)
        self.assertEqual(summary["malformed_lines"], 2)
        self.assertEqual(summary["searches"]["total"], 2)
        self.assertEqual(summary["searches"]["typed"], 1)
        self.assertEqual(summary["searches"]["voice"], 1)
        self.assertEqual(summary["searches"]["no_results"], 1)
        self.assertEqual(summary["searches"]["success_rate_percent"], 50.0)
        self.assertEqual(summary["performance_ms"]["average"], 15.0)
        self.assertEqual(summary["performance_ms"]["sample_count"], 2)
        self.assertEqual(summary["performance_ms"]["minimum"], 10.0)
        self.assertEqual(summary["performance_ms"]["p50"], 10.0)
        self.assertEqual(summary["performance_ms"]["p95"], 20.0)
        self.assertEqual(summary["performance_ms"]["p99"], 20.0)
        self.assertEqual(summary["performance_ms"]["maximum"], 20.0)
        self.assertEqual(summary["performance_ms"]["total"], 30.0)
        self.assertEqual(summary["performance_ms"]["recent_100_average"], 15.0)
        self.assertEqual(summary["session_performance_ms"]["sample_count"], 2)
        self.assertEqual(summary["session_performance_ms"]["average"], 15.0)
        self.assertEqual(summary["searches_by_hour"][0]["count"], 2)
        self.assertEqual(summary["top_queries"][0]["count"], 2)
        self.assertEqual(summary["top_selections"][0]["sentence_id"], 4)

        exported_json = json.loads(self.store.export_json().decode("utf-8"))
        self.assertEqual(exported_json["malformed_lines"], 2)
        self.assertEqual(len(exported_json["events"]), 3)

        csv_text = self.store.export_csv().decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual(len(rows), 3)
        self.assertIn('"sentence_id":4', rows[1]["results"])

    def test_empty_summary_and_atomic_clear(self) -> None:
        empty = self.store.summary()
        self.assertEqual(empty["event_count"], 0)
        self.assertEqual(empty["searches"]["success_rate_percent"], 0.0)
        self.assertEqual(empty["performance_ms"]["average"], 0.0)
        self.assertEqual(empty["performance_ms"]["p95"], 0.0)

        self.store.record("page_view", page="admin")
        self.store.clear()
        self.assertEqual(self.store.read_events(), ([], 0))


class AdminServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        corpus = self.root / "corpus"
        corpus.mkdir()
        (corpus / "sample.txt").write_text(
            "Alpha demo\n!!!\nBeta demo\n",
            encoding="utf-8",
        )
        index, master = build_index(corpus)
        self.system = AutocompleteSystem(
            index,
            master,
            data_directory=self.root / "data",
            ranking_mode=RankingMode.POPULARITY,
        )
        self.analytics = AnalyticsStore(self.root / "analytics")
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "system.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:00+00:00",
                            "level": "INFO",
                            "logger": "autocomplete.engine",
                            "event": "search_completed",
                            "message": "search completed",
                            "details": {"query": "demo"},
                        }
                    ),
                    "malformed",
                    "[]",
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:01+00:00",
                            "level": "ERROR",
                            "logger": "autocomplete.web",
                            "event": "web_search_failed",
                            "message": "search failed",
                            "details": {"query": "broken"},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (logs / "system.jsonl.1").write_text("{}\n", encoding="utf-8")
        self.service = AdminService(self.system, self.analytics, self.root)

    def tearDown(self) -> None:
        self.system.close()
        self.temporary_directory.cleanup()

    def test_dashboard_corpus_cache_popularity_storage_and_pagination(self) -> None:
        first_dashboard = self.service.dashboard()
        corpus = first_dashboard["corpus"]
        self.assertEqual(corpus["total_sentences"], 3)
        self.assertEqual(corpus["searchable_sentences"], 2)
        self.assertEqual(corpus["normalized_empty_sentences"], 1)
        self.assertEqual(corpus["source_files"], 1)
        self.assertEqual(first_dashboard["configuration"]["alpha"], 5)
        self.assertTrue(first_dashboard["configuration"]["popularity_enabled"])
        self.assertEqual(first_dashboard["rebuild"]["state"], "idle")
        self.assertIn("technical", first_dashboard)
        self.assertIn("runtime", first_dashboard["technical"])
        self.assertIn("storage", first_dashboard["technical"])
        self.assertEqual(
            first_dashboard["technical"]["last_build"]["sentence_count"], 3
        )

        self.system.record_selection(0)
        self.service.note_selection(0)
        second_dashboard = self.service.dashboard()
        self.assertEqual(second_dashboard["corpus"]["popularity"]["total_usage"], 1)
        self.assertTrue(
            any(item["name"] == "logs/system.jsonl" for item in second_dashboard["storage"])
        )

        page = self.service.sentences_page(offset=100, limit=500)
        self.assertEqual(page["records"], [])
        first_page = self.service.sentences_page(offset=-10, limit=1)
        self.assertEqual(first_page["offset"], 0)
        self.assertEqual(first_page["records"][0]["line_number"], 1)

        self.service.reset_popularity()
        self.assertEqual(self.service.dashboard()["corpus"]["popularity"]["total_usage"], 0)
        self.assertTrue((self.root / "data" / "usage_stats.json").is_file())

    def test_log_listing_resolution_and_all_filters(self) -> None:
        files = self.service.log_files()
        self.assertEqual([item["filename"] for item in files], ["system.jsonl", "system.jsonl.1"])
        filtered = self.service.read_system_logs(
            "system.jsonl",
            limit=10,
            level="error",
            component="web",
            search="broken",
        )
        self.assertEqual(filtered["record_count"], 1)
        self.assertEqual(filtered["malformed_lines"], 2)
        self.assertEqual(filtered["records"][0]["event"], "web_search_failed")

        with self.assertRaises(ValueError):
            self.service.resolve_log_file("../README.md")
        with self.assertRaises(FileNotFoundError):
            self.service.resolve_log_file("system.jsonl.5")

    def test_reset_analytics_and_start_rebuild_are_delegated(self) -> None:
        self.analytics.record("search", result_count=0)
        self.service.reset_analytics()
        self.assertEqual(self.analytics.read_events(), ([], 0))

        fake_manager = unittest.mock.Mock()
        fake_manager.status.return_value = {"state": "idle"}
        fake_manager.start.return_value = {"state": "running", "pid": 123}
        service = AdminService(
            self.system,
            self.analytics,
            self.root,
            rebuild_manager=fake_manager,
        )
        self.assertEqual(service.start_rebuild()["pid"], 123)
        fake_manager.start.assert_called_once_with()

    def test_empty_master_array_dashboard_has_safe_averages(self) -> None:
        empty_system = AutocompleteSystem(CompressedSuffixTrie(), [])
        service = AdminService(empty_system, self.analytics, self.root)
        dashboard = service.dashboard()
        self.assertEqual(dashboard["corpus"]["total_sentences"], 0)
        self.assertEqual(dashboard["corpus"]["average_original_length"], 0.0)

    def test_completed_rebuild_is_activated_and_old_index_is_pruned_by_default(self) -> None:
        active = self.root / "data"
        save_index(active, self.system.index, self.system.master_array)
        self.system.data_directory = active
        active_analytics = AnalyticsStore(active)
        service = AdminService(self.system, active_analytics, self.root)
        service._static_corpus = {"stale": True}

        replacement_corpus = self.root / "replacement-corpus"
        replacement_corpus.mkdir()
        (replacement_corpus / "new.txt").write_text(
            "Newly indexed sentence\n",
            encoding="utf-8",
        )
        replacement_index, replacement_master = build_index(replacement_corpus)
        replacement = self.root / "rebuilds" / "data-rebuild-test"
        save_index(replacement, replacement_index, replacement_master)

        backup = service._activate_rebuild(replacement)

        self.assertEqual(
            self.system.get_best_k_completions("newly")[0].completed_sentence,
            "Newly indexed sentence",
        )
        self.assertEqual(self.system.data_directory, active)
        self.assertIsNone(backup)
        self.assertEqual(list((self.root / "rebuilds").glob("data-backup-*")), [])
        self.assertTrue((active / "index.pkl").is_file())
        self.assertFalse(replacement.exists())
        self.assertIsNone(service._static_corpus)
        events, _ = active_analytics.read_events()
        self.assertEqual(events[-1]["event_type"], "index_activation")


class RebuildManagerValidationTests(unittest.TestCase):
    def test_missing_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = RebuildManager(root, root / "missing.zip", root / "rebuilds")
            with self.assertRaises(FileNotFoundError):
                manager.start()

    def test_second_concurrent_build_is_rejected(self) -> None:
        class RunningProcess:
            pid = 777

            @staticmethod
            def poll() -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("demo\n", encoding="utf-8")
            manager = RebuildManager(root, source, root / "rebuilds")
            with patch(
                "autocomplete_system.analytics.subprocess.Popen",
                return_value=RunningProcess(),
            ):
                manager.start()
                self.assertEqual(manager.status()["state"], "running")
                with self.assertRaises(RuntimeError):
                    manager.start()
            if manager._log_file is not None:
                manager._log_file.close()

    def test_nonzero_process_exit_is_reported_as_failed(self) -> None:
        class FailedProcess:
            pid = 778

            @staticmethod
            def poll() -> int:
                return 7

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("demo\n", encoding="utf-8")
            manager = RebuildManager(root, source, root / "rebuilds")
            with patch(
                "autocomplete_system.analytics.subprocess.Popen",
                return_value=FailedProcess(),
            ):
                status = manager.start()
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["return_code"], 7)
            self.assertIsNotNone(status["finished_at"])

    def test_successful_build_runs_activation_callback(self) -> None:
        class CompletedProcess:
            pid = 779

            @staticmethod
            def poll() -> int:
                return 0

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("demo\n", encoding="utf-8")
            backup = root / "rebuilds" / "backup"
            activate = unittest.mock.Mock(return_value=backup)
            manager = RebuildManager(
                root,
                source,
                root / "rebuilds",
                activation_callback=activate,
            )
            with patch(
                "autocomplete_system.analytics.subprocess.Popen",
                return_value=CompletedProcess(),
            ):
                status = manager.start()

            activate.assert_called_once_with(Path(status["target_directory"]))
            self.assertEqual(status["state"], "completed")
            self.assertTrue(status["activated"])
            self.assertEqual(status["backup_directory"], str(backup))

    def test_activation_failure_is_reported_as_failed(self) -> None:
        class CompletedProcess:
            pid = 780

            @staticmethod
            def poll() -> int:
                return 0

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("demo\n", encoding="utf-8")
            manager = RebuildManager(
                root,
                source,
                root / "rebuilds",
                activation_callback=unittest.mock.Mock(
                    side_effect=RuntimeError("activation failed")
                ),
            )
            with patch(
                "autocomplete_system.analytics.subprocess.Popen",
                return_value=CompletedProcess(),
            ):
                status = manager.start()

            self.assertEqual(status["state"], "failed")
            self.assertFalse(status["activated"])
            self.assertIn("activation failed", status["activation_error"])


if __name__ == "__main__":
    unittest.main()
