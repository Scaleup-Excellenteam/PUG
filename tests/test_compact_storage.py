from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_sqlite_index
from autocomplete_system.models import RankingMode
from autocomplete_system.sqlite_store import SQLiteSentenceStore
from autocomplete_system.storage import load_index, save_index
from optimize_data import optimize
from package_project import build_bundle


class CompactSQLiteTests(unittest.TestCase):
    def test_compact_build_uses_contentless_fts_and_tiny_master_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            corpus = root / "corpus"
            data = root / "data"
            corpus.mkdir()
            (corpus / "sample.txt").write_text(
                "Hello, world!\nA useful demo\nAnother hello\n",
                encoding="utf-8",
            )

            index, master = build_sqlite_index(corpus, data)
            self.assertIsInstance(master, SQLiteSentenceStore)
            save_index(data, index, master)
            index.close()
            master.close()

            self.assertLess((data / "sentences.pkl").stat().st_size, 1024)
            connection = sqlite3.connect(data / "sentences.sqlite3")
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            self.assertNotIn("assignment_fts_content", tables)
            self.assertNotIn("assignment_fts_docsize", tables)
            self.assertNotIn("length_fts_content", tables)
            self.assertNotIn("length_fts_docsize", tables)

            loaded = AutocompleteSystem.load(data, RankingMode.ASSIGNMENT)
            try:
                self.assertEqual(
                    loaded.get_best_k_completions("hello")[0].completed_sentence,
                    "Another hello",
                )
                sentence_id = loaded.get_ranked_completions("world")[0][0]
                loaded.record_selection(sentence_id)
                loaded.save_usage_stats()
            finally:
                loaded.close()
            _, reloaded_master = load_index(data)
            try:
                self.assertEqual(reloaded_master[sentence_id].usage_count, 1)
            finally:
                reloaded_master.close()

    def test_optimizer_preserves_legacy_search_results_and_mutable_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            corpus = root / "corpus"
            compact_build = root / "compact-build"
            legacy = root / "legacy"
            output = root / "optimized"
            corpus.mkdir()
            (corpus / "sample.txt").write_text(
                "Hello, world!\nA useful demo\nAnother hello\n",
                encoding="utf-8",
            )
            compact_index, compact_master = build_sqlite_index(corpus, compact_build)

            # Recreate the historical schema from the compact build's records.
            legacy.mkdir()
            connection = sqlite3.connect(legacy / "sentences.sqlite3")
            connection.executescript(
                """
                CREATE TABLE sentences (
                    sentence_id INTEGER PRIMARY KEY,
                    normalized TEXT NOT NULL,
                    original TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    original_length INTEGER NOT NULL
                );
                CREATE VIRTUAL TABLE assignment_fts USING fts5(
                    normalized, sentence_id UNINDEXED, tokenize='trigram'
                );
                CREATE VIRTUAL TABLE length_fts USING fts5(
                    normalized, sentence_id UNINDEXED, tokenize='trigram'
                );
                """
            )
            rows = [
                (
                    sentence_id,
                    record.normalized_text,
                    record.original_text,
                    record.source_path,
                    record.line_number,
                    len(record.original_text),
                )
                for sentence_id, record in enumerate(compact_master)
            ]
            connection.executemany(
                "INSERT INTO sentences VALUES (?, ?, ?, ?, ?, ?)", rows
            )
            connection.execute(
                """
                INSERT INTO assignment_fts(normalized, sentence_id)
                SELECT normalized, sentence_id FROM sentences
                WHERE normalized <> ''
                ORDER BY normalized, original, source_path, line_number, sentence_id
                """
            )
            connection.execute(
                """
                INSERT INTO length_fts(normalized, sentence_id)
                SELECT normalized, sentence_id FROM sentences
                WHERE normalized <> ''
                ORDER BY original_length, original, sentence_id
                """
            )
            connection.commit()
            connection.close()

            compact_index.compact_schema = False
            save_index(legacy, compact_index, list(compact_master))
            compact_index.close()
            compact_master.close()
            (legacy / "ranking_settings.json").write_text(
                json.dumps({"ranking_mode": "popularity"}), encoding="utf-8"
            )
            (legacy / "analytics_events.jsonl").write_text(
                '{"event":"kept"}\n', encoding="utf-8"
            )

            optimize(legacy, output)

            self.assertTrue((legacy / "sentences.pkl").stat().st_size > 100)
            self.assertLess((output / "sentences.pkl").stat().st_size, 1024)
            self.assertEqual(
                (output / "ranking_settings.json").read_text("utf-8"),
                (legacy / "ranking_settings.json").read_text("utf-8"),
            )
            optimized = AutocompleteSystem.load(output)
            legacy_system = AutocompleteSystem.load(legacy)
            try:
                for query in ("hello", "world", "demo", "hxllo"):
                    self.assertEqual(
                        optimized.get_best_k_completions(query),
                        legacy_system.get_best_k_completions(query),
                    )
            finally:
                optimized.close()
                legacy_system.close()


class PackagingTests(unittest.TestCase):
    def test_source_bundle_excludes_generated_state_but_keeps_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "project"
            root.mkdir()
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "Archive").mkdir()
            (root / "Archive" / "source.zip").write_bytes(b"PK sample")
            (root / "data").mkdir()
            (root / "data" / "sentences.pkl").write_bytes(b"large")
            (root / "data_compact").mkdir()
            (root / "data_compact" / "trial.sqlite3").write_bytes(b"duplicate")
            (root / ".git").mkdir()
            (root / ".git" / "objects").write_bytes(b"history")
            output = Path(temporary_directory) / "upload.zip"

            build_bundle(root, output, "source")

            import zipfile

            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
            self.assertIn("project/main.py", names)
            self.assertIn("project/Archive/source.zip", names)
            self.assertNotIn("project/data/sentences.pkl", names)
            self.assertNotIn("project/data_compact/trial.sqlite3", names)
            self.assertNotIn("project/.git/objects", names)


if __name__ == "__main__":
    unittest.main()
