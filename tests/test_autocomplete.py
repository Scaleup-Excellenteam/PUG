from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autocomplete_system.constants import MAX_NODE_CACHE_SIZE
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index, build_sqlite_index
from autocomplete_system.models import RankingMode
from autocomplete_system.normalization import normalize_text
from autocomplete_system.storage import load_index, save_index
from main import READY_PROMPT, SUGGESTIONS_HEADER, run_cli


class AutocompleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.corpus = self.root / "Archive"
        nested = self.corpus / "nested"
        nested.mkdir(parents=True)
        (self.corpus / "first.txt").write_text(
            "Hello, world!\nThis is a demo.\n\n",
            encoding="utf-8",
        )
        (nested / "second.txt").write_text(
            "A demo is useful.\nHello, world!\n",
            encoding="utf-8",
        )
        index, master_array = build_index(self.corpus)
        self.system = AutocompleteSystem(index, master_array)
        self.systems_to_close = [self.system]

    def tearDown(self) -> None:
        for system in self.systems_to_close:
            system.close()
        self.temporary_directory.cleanup()

    def test_normalization_deletes_ascii_and_unicode_punctuation(self) -> None:
        self.assertEqual(normalize_text("  Don't—stop!  now "), "dontstop now")

    def test_search_starts_at_every_character(self) -> None:
        result = self.system.get_best_k_completions("llo")[0]
        self.assertEqual(result.completed_sentence, "Hello, world!")
        self.assertEqual(result.score, 6)

    def test_spaces_are_counted_in_exact_score(self) -> None:
        result = self.system.get_best_k_completions("is a d")[0]
        self.assertEqual(result.completed_sentence, "This is a demo.")
        self.assertEqual(result.score, 12)

    def test_substitution_has_position_penalty(self) -> None:
        result = self.system.get_best_k_completions("hxllo")[0]
        self.assertEqual(result.completed_sentence, "Hello, world!")
        self.assertEqual(result.score, 4)

    def test_missing_character_uses_indel_penalty(self) -> None:
        result = self.system.get_best_k_completions("helo")[0]
        self.assertEqual(result.completed_sentence, "Hello, world!")
        self.assertEqual(result.score, 4)

    def test_document_scoring_examples(self) -> None:
        corpus = self.root / "scoring"
        corpus.mkdir()
        sentence = "To be or not to be, that is the question."
        (corpus / "example.txt").write_text(sentence + "\n", encoding="utf-8")
        index, master = build_index(corpus)
        system = AutocompleteSystem(index, master)
        self.systems_to_close.append(system)

        expected_scores = {
            "To be": 10,
            "or Not": 12,
            "be, that": 14,
            "2o be": 3,
            "to pe": 6,
            "or knot": 8,
            "or nt": 8,
        }
        for query, expected_score in expected_scores.items():
            with self.subTest(query=query):
                result = system.get_best_k_completions(query)[0]
                self.assertEqual(result.completed_sentence, sentence)
                self.assertEqual(result.score, expected_score)
        self.assertEqual(system.get_best_k_completions("not be"), [])

    def test_duplicate_lines_have_unique_results_and_offsets(self) -> None:
        results = self.system.get_best_k_completions("hello")
        matches = [item for item in results if item.completed_sentence == "Hello, world!"]
        self.assertEqual(len(matches), 2)
        self.assertEqual({item.source_text for item in matches}, {"first.txt", "nested/second.txt"})
        self.assertEqual({item.offset for item in matches}, {1, 2})

    def test_popularity_mode_changes_score_but_assignment_mode_does_not(self) -> None:
        sentence_id, assignment_before = self.system.get_ranked_completions("a demo")[0]
        self.system.record_selection(sentence_id)
        assignment_after = self.system.get_best_k_completions("a demo")[0]
        self.assertEqual(assignment_after.score, assignment_before.score)

        popularity = self.system.get_ranked_completions(
            "a demo", ranking_mode=RankingMode.POPULARITY
        )[0][1]
        self.assertEqual(popularity.score, assignment_before.score + 5)

    def test_pickle_and_usage_json_round_trip_for_trie(self) -> None:
        data_directory = self.root / "data"
        sentence_id = self.system.get_ranked_completions("this")[0][0]
        self.system.record_selection(sentence_id)
        save_index(data_directory, self.system.index, self.system.master_array)

        _, loaded_master = load_index(data_directory)
        self.assertEqual(loaded_master[sentence_id].usage_count, 1)

    def test_empty_normalized_query_has_no_results(self) -> None:
        self.assertEqual(self.system.get_best_k_completions("!!!"), [])

    def test_nonempty_punctuation_line_remains_in_master_array(self) -> None:
        punctuation_file = self.corpus / "punctuation.txt"
        punctuation_file.write_text("!!!\n", encoding="utf-8")
        _, master_array = build_index(self.corpus)
        punctuation_records = [
            record for record in master_array if record.original_text == "!!!"
        ]
        self.assertEqual(len(punctuation_records), 1)
        self.assertEqual(punctuation_records[0].normalized_text, "")

    def test_node_caches_keep_exactly_twenty_candidates_by_both_orders(self) -> None:
        cache_corpus = self.root / "cache-corpus"
        cache_corpus.mkdir()
        lines = [f"Name{number:02d} common {'x' * (25 - number)}" for number in range(25)]
        (cache_corpus / "cache.txt").write_text(
            "\n".join(reversed(lines)) + "\n",
            encoding="utf-8",
        )
        index, master_array = build_index(cache_corpus)

        by_length = index.candidate_text_scores("common", RankingMode.POPULARITY)
        by_alphabet = index.candidate_text_scores("common", RankingMode.ASSIGNMENT)
        self.assertEqual(len(by_length), MAX_NODE_CACHE_SIZE)
        self.assertEqual(len(by_alphabet), MAX_NODE_CACHE_SIZE)

        cached_length_texts = {master_array[item].original_text for item in by_length}
        expected_length = set(
            sorted(lines, key=lambda text: (len(text), text))[:MAX_NODE_CACHE_SIZE]
        )
        self.assertEqual(cached_length_texts, expected_length)

        cached_alphabet_texts = {master_array[item].original_text for item in by_alphabet}
        expected_alphabet = set(sorted(lines)[:MAX_NODE_CACHE_SIZE])
        self.assertEqual(cached_alphabet_texts, expected_alphabet)

    def test_zip_input_preserves_entry_path_and_line_offset(self) -> None:
        archive_path = self.root / "Archive.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("folder/example.txt", "First\n\nSecond\n")
            archive.writestr("ignored.csv", "Not indexed\n")

        _, master = build_index(archive_path)
        self.assertEqual(
            [(item.original_text, item.source_path, item.line_number) for item in master],
            [
                ("First", "folder/example.txt", 1),
                ("Second", "folder/example.txt", 3),
            ],
        )

    def test_sqlite_backend_matches_trie_backend(self) -> None:
        sqlite_data = self.root / "sqlite-data"
        sqlite_index, sqlite_master = build_sqlite_index(self.corpus, sqlite_data)
        sqlite_system = AutocompleteSystem(sqlite_index, sqlite_master)
        self.systems_to_close.append(sqlite_system)

        for query in ("hello", "llo", "hxllo", "helo", "is a d", "demo"):
            with self.subTest(query=query):
                trie_results = self.system.get_best_k_completions(query)
                sqlite_results = sqlite_system.get_best_k_completions(query)
                self.assertEqual(sqlite_results, trie_results)

    def test_long_one_edit_anchor_search_matches_trie(self) -> None:
        corpus = self.root / "long-edit-corpus"
        corpus.mkdir()
        (corpus / "long.txt").write_text(
            "prefix abcdefghij suffix\n",
            encoding="utf-8",
        )
        trie_index, trie_master = build_index(corpus)
        trie_system = AutocompleteSystem(trie_index, trie_master)
        self.systems_to_close.append(trie_system)

        sqlite_data = self.root / "long-edit-data"
        sqlite_index, sqlite_master = build_sqlite_index(corpus, sqlite_data)
        sqlite_system = AutocompleteSystem(sqlite_index, sqlite_master)
        self.systems_to_close.append(sqlite_system)

        expected_scores = {
            "abcdxfghij": 17,
            "abcdfghij": 16,
            "abcdexfghij": 18,
        }
        for query, expected_score in expected_scores.items():
            with self.subTest(query=query):
                trie_results = trie_system.get_best_k_completions(query)
                sqlite_results = sqlite_system.get_best_k_completions(query)
                self.assertEqual(sqlite_results, trie_results)
                self.assertEqual(sqlite_results[0].score, expected_score)

    def test_sqlite_pickle_round_trip(self) -> None:
        data_directory = self.root / "sqlite-persistence"
        index, master = build_sqlite_index(self.corpus, data_directory)
        save_index(data_directory, index, master)
        index.close()

        loaded = AutocompleteSystem.load(data_directory)
        self.systems_to_close.append(loaded)
        self.assertEqual(
            loaded.get_best_k_completions("hello")[0].completed_sentence,
            "Hello, world!",
        )

    def test_cli_accumulates_fragments_hash_selects_and_saves(self) -> None:
        data_directory = self.root / "cli-data"
        save_index(data_directory, self.system.index, self.system.master_array)
        loaded_system = AutocompleteSystem.load(data_directory)
        self.systems_to_close.append(loaded_system)
        output = io.StringIO()

        with patch("builtins.input", side_effect=["hel", "lo", "#", EOFError]):
            with redirect_stdout(output):
                run_cli(loaded_system)

        rendered = output.getvalue()
        self.assertIn(READY_PROMPT, rendered)
        self.assertIn(SUGGESTIONS_HEADER, rendered)
        self.assertIn("1. Hello, world! (first.txt:1, score=10)", rendered)
        stats = json.loads(
            (data_directory / "usage_stats.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stats["0"], 1)


if __name__ == "__main__":
    unittest.main()
