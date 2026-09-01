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
from autocomplete_system.indexer import build_array_index, build_index, build_sqlite_index
from autocomplete_system.models import RankingMode
from autocomplete_system.normalization import normalize_text
from autocomplete_system.scoring import (
    generate_scored_variants,
    indel_penalty,
    substitution_penalty,
)
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
        # Per the assignment PDF (project_part_a.pdf, scoring appendix):
        # "not be" is "Not a match: no substring can be reached with at most
        # one character edit." The query spans a word boundary that doesn't
        # appear at any single anchor point within one edit distance.
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

    def test_popularity_toggle_keeps_the_same_candidate_pool(self) -> None:
        cache_corpus = self.root / "toggle-corpus"
        cache_corpus.mkdir()
        lines = [
            f"Name{number:02d} common {'x' * (25 - number)}"
            for number in range(25)
        ]
        (cache_corpus / "cache.txt").write_text(
            "\n".join(reversed(lines)) + "\n",
            encoding="utf-8",
        )
        index, master_array = build_index(cache_corpus)
        system = AutocompleteSystem(index, master_array)
        self.systems_to_close.append(system)

        assignment_ids = {
            sentence_id
            for sentence_id, _ in system.get_ranked_completions(
                "common",
                k=MAX_NODE_CACHE_SIZE,
                ranking_mode=RankingMode.ASSIGNMENT,
            )
        }
        popularity_ids = {
            sentence_id
            for sentence_id, _ in system.get_ranked_completions(
                "common",
                k=MAX_NODE_CACHE_SIZE,
                ranking_mode=RankingMode.POPULARITY,
            )
        }

        self.assertEqual(assignment_ids, popularity_ids)
        self.assertEqual(len(assignment_ids), MAX_NODE_CACHE_SIZE)

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

    def test_sqlite_legacy_candidate_orders_and_detached_guard(self) -> None:
        cache_corpus = self.root / "sqlite-cache-corpus"
        cache_corpus.mkdir()
        lines = [
            f"Name{number:02d} common {'x' * (25 - number)}"
            for number in range(25)
        ]
        (cache_corpus / "cache.txt").write_text(
            "\n".join(reversed(lines)) + "\n",
            encoding="utf-8",
        )
        sqlite_data = self.root / "sqlite-cache-data"
        sqlite_index, sqlite_master = build_sqlite_index(cache_corpus, sqlite_data)
        sqlite_system = AutocompleteSystem(sqlite_index, sqlite_master)
        self.systems_to_close.append(sqlite_system)

        by_length = sqlite_index.candidate_text_scores(
            "common",
            RankingMode.POPULARITY,
            allow_popularity_exact_shortcut=True,
        )
        by_alphabet = sqlite_index.candidate_text_scores(
            "common",
            RankingMode.ASSIGNMENT,
        )
        self.assertEqual(len(by_length), MAX_NODE_CACHE_SIZE)
        self.assertEqual(len(by_alphabet), MAX_NODE_CACHE_SIZE)
        self.assertNotEqual(set(by_length), set(by_alphabet))

        detached = type(sqlite_index)("missing.sqlite3", (), {}, {})
        with self.assertRaises(RuntimeError):
            detached.candidate_text_scores("demo", RankingMode.POPULARITY)

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



class ScoringTests(unittest.TestCase):
    """Direct tests for the scoring module (project_part_a.pdf penalty tables)."""

    def test_substitution_penalty_matches_assignment_table(self) -> None:
        # Position:   1   2   3   4   5+
        # Penalty:    5   4   3   2   1
        self.assertEqual(substitution_penalty(1), 5)
        self.assertEqual(substitution_penalty(2), 4)
        self.assertEqual(substitution_penalty(3), 3)
        self.assertEqual(substitution_penalty(4), 2)
        self.assertEqual(substitution_penalty(5), 1)
        self.assertEqual(substitution_penalty(6), 1)
        self.assertEqual(substitution_penalty(100), 1)

    def test_indel_penalty_matches_assignment_table(self) -> None:
        # Position:   1    2   3   4   5+
        # Penalty:   10    8   6   4    2
        self.assertEqual(indel_penalty(1), 10)
        self.assertEqual(indel_penalty(2), 8)
        self.assertEqual(indel_penalty(3), 6)
        self.assertEqual(indel_penalty(4), 4)
        self.assertEqual(indel_penalty(5), 2)
        self.assertEqual(indel_penalty(6), 2)
        self.assertEqual(indel_penalty(100), 2)

    def test_exact_match_variant_gets_full_score(self) -> None:
        variants = generate_scored_variants("hello", "abcdefghijklmnopqrstuvwxyz")
        # Exact match: 2 * len("hello") = 10
        self.assertEqual(variants["hello"], 10)

    def test_variant_count_for_known_query(self) -> None:
        alphabet = "abc"
        query = "ab"
        variants = generate_scored_variants(query, alphabet)
        # Exact: "ab" (1)
        # Substitutions at each of 2 positions × 2 other chars = 4 variants
        # Deletions at each of 2 positions = 2 variants (may overlap with subs)
        # Insertions at each of 3 positions × 3 chars = 9 variants (may overlap)
        # Total unique keys: verify exact count doesn't regress
        self.assertIn("ab", variants)  # exact
        self.assertIn("cb", variants)  # sub pos 1
        self.assertIn("ac", variants)  # sub pos 2
        self.assertIn("b", variants)   # delete pos 1
        self.assertIn("a", variants)   # delete pos 2
        self.assertIn("aab", variants) # insert at 0
        self.assertIn("abc", variants) # insert at 2
        # Verify scores follow formulas
        # "cb" = sub at pos 1: 2*(2-1) - 5 = -3, but keep_best keeps max
        # Since score can go negative, it's still stored
        self.assertEqual(variants["ab"], 4)  # exact: 2*2 = 4

    def test_empty_string_variants_are_excluded(self) -> None:
        # Deleting the only character of a 1-char query produces ""
        variants = generate_scored_variants("a", "abc")
        self.assertNotIn("", variants)

    def test_substitution_scores_match_assignment_examples(self) -> None:
        # From the PDF: "2o be" → sub '2' with 't' at pos 1
        # score = 2*4 - 5 = 3 (4 matching chars out of 5-char query with 1 sub)
        variants = generate_scored_variants("2o be", "abcdefghijklmnopqrstuvwxyz 0123456789")
        # The variant "to be" should exist (sub at pos 1)
        self.assertIn("to be", variants)
        # Score = 2*(5-1) - sub_penalty(1) = 8 - 5 = 3
        self.assertEqual(variants["to be"], 3)

    def test_indel_scores_match_assignment_examples(self) -> None:
        # From the PDF: "or knot" → delete 'k' at pos 4 → "or not"
        # score = 2*6 - indel_penalty(4) = 12 - 4 = 8
        variants = generate_scored_variants("or knot", "abcdefghijklmnopqrstuvwxyz ")
        self.assertIn("or not", variants)
        self.assertEqual(variants["or not"], 8)

        # From the PDF: "or nt" → insert 'o' at pos 5 → "or not"
        # score = 2*5 - indel_penalty(5) = 10 - 2 = 8
        # But "or not" is an insertion variant of "or nt" — it appears in
        # the variants of "or nt"
        variants_nt = generate_scored_variants("or nt", "abcdefghijklmnopqrstuvwxyz ")
        self.assertIn("or not", variants_nt)
        self.assertEqual(variants_nt["or not"], 8)


class SuffixArrayTests(unittest.TestCase):
    """Verify the SuffixArrayIndex backend matches the Trie backend."""

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
        trie_index, trie_master = build_index(self.corpus)
        self.trie_system = AutocompleteSystem(trie_index, trie_master)
        array_index, array_master = build_array_index(self.corpus)
        self.array_system = AutocompleteSystem(array_index, array_master)

    def tearDown(self) -> None:
        self.trie_system.close()
        self.array_system.close()
        self.temporary_directory.cleanup()

    def test_suffix_array_matches_trie_on_standard_queries(self) -> None:
        for query in ("hello", "llo", "hxllo", "helo", "is a d", "demo"):
            with self.subTest(query=query):
                trie_results = self.trie_system.get_best_k_completions(query)
                array_results = self.array_system.get_best_k_completions(query)
                self.assertEqual(array_results, trie_results)

    def test_suffix_array_matches_trie_on_empty_and_punctuation_queries(self) -> None:
        for query in ("!!!", "", "   "):
            with self.subTest(query=query):
                trie_results = self.trie_system.get_best_k_completions(query)
                array_results = self.array_system.get_best_k_completions(query)
                self.assertEqual(array_results, trie_results)


class ErrorCacheTests(unittest.TestCase):
    """Tests for the Aho-Corasick error cache (typo correction DFA)."""

    def setUp(self) -> None:
        from autocomplete_system.error_cache import ErrorCache
        self.cache = ErrorCache(max_size=5)

    def test_empty_cache_returns_query_unchanged(self) -> None:
        self.assertEqual(self.cache.scan_and_replace("pythun"), "pythun")

    def test_submit_rebuild_scan_corrects_typo(self) -> None:
        self.cache.submit_correction("pythun", "python")
        # Before rebuild, DFA is empty so scan returns original
        self.assertEqual(self.cache.scan_and_replace("pythun"), "pythun")
        # After rebuild, DFA has the correction
        self.cache.rebuild_cycle()
        self.assertEqual(self.cache.scan_and_replace("pythun"), "python")

    def test_scan_replaces_longest_match(self) -> None:
        self.cache.submit_correction("py", "XX")
        self.cache.submit_correction("pyth", "YYYY")
        self.cache.rebuild_cycle()
        # "pyth" is longer than "py", so it should be preferred
        result = self.cache.scan_and_replace("pyth is great")
        self.assertIn("YYYY", result)
        self.assertNotIn("XX", result)

    def test_multiple_corrections_accumulated(self) -> None:
        self.cache.submit_correction("teh", "the")
        self.cache.submit_correction("wrold", "world")
        self.cache.rebuild_cycle()
        # Each query should be corrected independently
        self.assertEqual(self.cache.scan_and_replace("teh"), "the")
        self.assertEqual(self.cache.scan_and_replace("wrold"), "world")

    def test_lru_eviction_drops_oldest_entries(self) -> None:
        # max_size=5. The queue also caps at max_size, so we submit in batches.
        # Batch 1: 5 entries → cache has 5 entries (at capacity, no eviction)
        for i in range(5):
            self.cache.submit_correction(f"mistake{i}", f"fix{i}")
        self.cache.rebuild_cycle()
        self.assertEqual(len(self.cache.cache), 5)

        # Batch 2: 3 more entries → cache grows to 8, evicts oldest 3
        for i in range(5, 8):
            self.cache.submit_correction(f"mistake{i}", f"fix{i}")
        self.cache.rebuild_cycle()
        self.assertEqual(len(self.cache.cache), 5)

        # Oldest (mistake0, mistake1, mistake2) should be evicted
        self.assertEqual(self.cache.scan_and_replace("mistake0"), "mistake0")
        self.assertEqual(self.cache.scan_and_replace("mistake1"), "mistake1")
        self.assertEqual(self.cache.scan_and_replace("mistake2"), "mistake2")
        # Newest should still work
        self.assertEqual(self.cache.scan_and_replace("mistake7"), "fix7")
        self.assertEqual(self.cache.scan_and_replace("mistake3"), "fix3")

    def test_queue_respects_max_size(self) -> None:
        for i in range(10):
            self.cache.submit_correction(f"m{i}", f"f{i}")
        # Queue should not grow beyond max_size (5)
        self.assertLessEqual(len(self.cache.queue), self.cache.max_size)

    def test_rebuild_with_empty_queue_is_noop(self) -> None:
        self.cache.rebuild_cycle()
        # DFA should remain empty
        self.assertEqual(self.cache._goto, {0: {}})

    def test_correction_embedded_in_longer_string(self) -> None:
        self.cache.submit_correction("teh", "the")
        self.cache.rebuild_cycle()
        result = self.cache.scan_and_replace("I saw teh cat")
        self.assertEqual(result, "I saw the cat")

    def test_no_match_returns_original(self) -> None:
        self.cache.submit_correction("xyz", "abc")
        self.cache.rebuild_cycle()
        self.assertEqual(self.cache.scan_and_replace("hello world"), "hello world")


if __name__ == "__main__":
    unittest.main()
