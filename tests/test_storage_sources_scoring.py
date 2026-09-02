from __future__ import annotations

import json
import pickle
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import autocomplete
from autocomplete_system.constants import (
    INDEX_FILENAME,
    INDEX_VERSION,
    MASTER_ARRAY_FILENAME,
    SQLITE_INDEX_FILENAME,
)
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index, build_sqlite_index, discover_text_files
from autocomplete_system.models import RankingMode, SentenceRecord
from autocomplete_system.scoring import (
    generate_scored_variants,
    indel_penalty,
    substitution_penalty,
)
from autocomplete_system.sources import iter_source_lines
from autocomplete_system.sqlite_index import SQLiteSubstringIndex
from autocomplete_system.storage import (
    load_index,
    load_ranking_mode_setting,
    load_usage_stats,
    save_index,
    save_ranking_mode_setting,
    save_usage_stats,
)


class ScoringContractTests(unittest.TestCase):
    def test_all_penalty_positions_match_the_specification(self) -> None:
        self.assertEqual(
            [substitution_penalty(position) for position in range(1, 8)],
            [5, 4, 3, 2, 1, 1, 1],
        )
        self.assertEqual(
            [indel_penalty(position) for position in range(1, 8)],
            [10, 8, 6, 4, 2, 2, 2],
        )

    def test_variant_generation_keeps_exact_and_each_single_edit_score(self) -> None:
        variants = generate_scored_variants("ab", "abc")

        self.assertEqual(variants["ab"], 4)
        self.assertEqual(variants["cb"], -3)  # substitution at position 1
        self.assertEqual(variants["b"], -8)  # deletion at position 1
        self.assertEqual(variants["abc"], -2)  # insertion at position 3
        self.assertNotIn("", variants)


class SourceAndIndexerValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_directory_and_direct_file_inputs_are_deterministic(self) -> None:
        corpus = self.root / "corpus"
        nested = corpus / "nested"
        nested.mkdir(parents=True)
        (corpus / "b.TXT").write_text("Second\n", encoding="utf-8")
        (corpus / "a.txt").write_text("First\n\nThird\n", encoding="utf-8")
        (nested / "c.txt").write_text("Nested\n", encoding="utf-8")
        (corpus / "ignored.csv").write_text("Ignored\n", encoding="utf-8")

        discovered = discover_text_files(corpus)
        self.assertEqual(
            [path.relative_to(corpus).as_posix() for path in discovered],
            ["a.txt", "b.TXT", "nested/c.txt"],
        )
        lines = list(iter_source_lines((corpus,)))
        self.assertEqual(
            [(item.original_text, item.source_path, item.line_number) for item in lines],
            [
                ("First", "a.txt", 1),
                ("Third", "a.txt", 3),
                ("Second", "b.TXT", 1),
                ("Nested", "nested/c.txt", 1),
            ],
        )
        direct = list(iter_source_lines((corpus / "a.txt",)))
        self.assertEqual([item.source_path for item in direct], ["a.txt", "a.txt"])

    def test_zip_entries_are_sorted_and_only_text_is_read(self) -> None:
        archive_path = self.root / "source.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("z.txt", "Last\n")
            archive.writestr("folder/", "")
            archive.writestr("a.TXT", "First\n")
            archive.writestr("ignored.md", "No\n")
            archive.writestr("__MACOSX/._a.txt", b"\xff\xfe")
            archive.writestr("folder/._z.txt", b"\xff\xfe")

        lines = list(iter_source_lines((archive_path,)))
        self.assertEqual(
            [(item.original_text, item.source_path) for item in lines],
            [("First", "a.TXT"), ("Last", "z.txt")],
        )

    def test_directory_reads_text_files_and_zip_archives_recursively(self) -> None:
        corpus = self.root / "corpus"
        nested = corpus / "nested"
        nested.mkdir(parents=True)
        (corpus / "plain.txt").write_text("Plain\n", encoding="utf-8")
        with zipfile.ZipFile(nested / "extra.zip", "w") as archive:
            archive.writestr("inside.txt", "Archived\n")
            archive.writestr("ignored.csv", "Not indexed\n")

        lines = list(iter_source_lines((corpus,)))

        self.assertEqual(
            [(item.original_text, item.source_path) for item in lines],
            [
                ("Archived", "nested/extra.zip!/inside.txt"),
                ("Plain", "plain.txt"),
            ],
        )

    def test_missing_and_unsupported_sources_are_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            list(iter_source_lines((self.root / "missing",)))
        unsupported = self.root / "data.csv"
        unsupported.write_text("value", encoding="utf-8")
        with self.assertRaises(ValueError):
            list(iter_source_lines((unsupported,)))
        with self.assertRaises(FileNotFoundError):
            discover_text_files(self.root / "missing-directory")

    def test_build_index_accepts_a_single_file_and_a_sequence(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("One\n", encoding="utf-8")
        second.write_text("Two\n", encoding="utf-8")

        _, single_master = build_index(first)
        _, combined_master = build_index((second, first))
        self.assertEqual([record.original_text for record in single_master], ["One"])
        self.assertEqual(
            [record.original_text for record in combined_master],
            ["One", "Two"],
        )

    def test_failed_sqlite_build_removes_its_temporary_database(self) -> None:
        corpus = self.root / "failure-corpus"
        corpus.mkdir()
        (corpus / "sample.txt").write_text("demo\n", encoding="utf-8")
        data_directory = self.root / "failed-sqlite"
        with (
            patch(
                "autocomplete_system.indexer._create_sqlite_schema",
                side_effect=RuntimeError("schema failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "schema failure"),
        ):
            build_sqlite_index(corpus, data_directory)
        self.assertFalse(
            (data_directory / f"{SQLITE_INDEX_FILENAME}.tmp").exists()
        )


class StorageAndPublicApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        (self.corpus / "sample.txt").write_text(
            "Hello, world!\nA useful demo\n",
            encoding="utf-8",
        )
        self.index, self.master = build_index(self.corpus)

    def tearDown(self) -> None:
        if autocomplete._system is not None:
            autocomplete._system.close()
            autocomplete._system = None
        self.temporary_directory.cleanup()

    def test_usage_stats_reset_missing_values_and_round_trip_sparse_counts(self) -> None:
        data_directory = self.root / "data"
        self.master[0].usage_count = 4
        self.master[1].usage_count = 0
        save_usage_stats(data_directory, self.master)
        self.assertEqual(
            json.loads((data_directory / "usage_stats.json").read_text("utf-8")),
            {"0": 4},
        )

        self.master[0].usage_count = 99
        self.master[1].usage_count = 99
        load_usage_stats(data_directory, self.master)
        self.assertEqual([record.usage_count for record in self.master], [4, 0])

        (data_directory / "usage_stats.json").unlink()
        load_usage_stats(data_directory, self.master)
        self.assertEqual([record.usage_count for record in self.master], [0, 0])

    def test_invalid_usage_stats_are_rejected(self) -> None:
        cases = (
            ([], "object"),
            ({"not-an-id": 1}, "Invalid sentence ID"),
            ({"9": 1}, "Invalid usage-stat entry"),
            ({"0": True}, "Invalid usage-stat entry"),
            ({"0": -1}, "Invalid usage-stat entry"),
        )
        for number, (payload, message) in enumerate(cases):
            with self.subTest(payload=payload):
                data_directory = self.root / f"invalid-usage-{number}"
                data_directory.mkdir()
                (data_directory / "usage_stats.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, message):
                    load_usage_stats(data_directory, self.master)

    def test_ranking_setting_default_round_trip_and_validation(self) -> None:
        data_directory = self.root / "settings"
        self.assertEqual(
            load_ranking_mode_setting(data_directory, RankingMode.ASSIGNMENT),
            RankingMode.ASSIGNMENT,
        )
        save_ranking_mode_setting(data_directory, RankingMode.POPULARITY)
        self.assertEqual(
            load_ranking_mode_setting(data_directory, RankingMode.ASSIGNMENT),
            RankingMode.POPULARITY,
        )

        for payload in ([], {"ranking_mode": "unknown"}, {}):
            (data_directory / "ranking_settings.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                load_ranking_mode_setting(data_directory, RankingMode.ASSIGNMENT)

    def test_load_index_rejects_missing_and_invalid_serialized_data(self) -> None:
        missing = self.root / "missing"
        with self.assertRaises(FileNotFoundError):
            load_index(missing)

        invalid_index = self.root / "invalid-index"
        invalid_index.mkdir()
        (invalid_index / INDEX_FILENAME).write_bytes(
            pickle.dumps({"version": INDEX_VERSION + 1, "index": self.index})
        )
        (invalid_index / MASTER_ARRAY_FILENAME).write_bytes(pickle.dumps(self.master))
        with self.assertRaisesRegex(ValueError, "unsupported version"):
            load_index(invalid_index)

        invalid_master = self.root / "invalid-master"
        invalid_master.mkdir()
        (invalid_master / INDEX_FILENAME).write_bytes(
            pickle.dumps({"version": INDEX_VERSION, "index": self.index})
        )
        (invalid_master / MASTER_ARRAY_FILENAME).write_bytes(pickle.dumps(["bad"]))
        with self.assertRaisesRegex(ValueError, "master array"):
            load_index(invalid_master)

    def test_sqlite_persistence_requires_its_database_file(self) -> None:
        detached = SQLiteSubstringIndex("missing.sqlite3", (), {}, {})
        with self.assertRaises(FileNotFoundError):
            save_index(self.root / "detached", detached, self.master)

        data_directory = self.root / "sqlite"
        sqlite_index, sqlite_master = build_sqlite_index(self.corpus, data_directory)
        save_index(data_directory, sqlite_index, sqlite_master)
        sqlite_index.close()
        (data_directory / SQLITE_INDEX_FILENAME).unlink()
        with self.assertRaises(FileNotFoundError):
            load_index(data_directory)

    def test_engine_validates_mutations_and_resets_all_counts(self) -> None:
        system = AutocompleteSystem(self.index, self.master)
        self.assertIs(system.trie, self.index)
        with self.assertRaises(IndexError):
            system.record_selection(-1)
        with self.assertRaises(IndexError):
            system.record_selection(len(self.master))
        with self.assertRaises(ValueError):
            system.save_usage_stats()

        system.record_selection(0)
        system.record_selection(1)
        system.reset_usage_counts()
        self.assertEqual([record.usage_count for record in self.master], [0, 0])

    def test_required_module_level_api_initializes_and_searches(self) -> None:
        data_directory = self.root / "public-api"
        save_index(data_directory, self.index, self.master)

        autocomplete.initialize(data_directory, RankingMode.ASSIGNMENT)
        results = autocomplete.get_best_k_completions("world")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].completed_sentence, "Hello, world!")
        self.assertIsInstance(results[0].score, int)

    def test_public_api_closes_previous_system_and_supports_lazy_initialization(self) -> None:
        data_directory = self.root / "public-api-reload"
        save_index(data_directory, self.index, self.master)
        previous = Mock()
        autocomplete._system = previous
        autocomplete.initialize(data_directory, RankingMode.ASSIGNMENT)
        previous.close.assert_called_once_with()

        loaded = autocomplete._system
        assert loaded is not None
        loaded.close()
        autocomplete._system = None
        fake_system = Mock()
        fake_system.get_best_k_completions.return_value = ["sentinel"]

        def lazy_initialize() -> None:
            autocomplete._system = fake_system

        with patch.object(autocomplete, "initialize", side_effect=lazy_initialize) as initialize:
            self.assertEqual(autocomplete.get_best_k_completions("demo"), ["sentinel"])
        initialize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
