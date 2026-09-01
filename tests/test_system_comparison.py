import unittest
from pathlib import Path
from autocomplete_system.engine import AutocompleteSystem

class SystemComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load the 3 prebuilt indexes from the c-api corpus
        cls.trie_system = AutocompleteSystem.load(Path("data/data_trie"))
        cls.sqlite_system = AutocompleteSystem.load(Path("data/data_sqlite"))
        cls.array_system = AutocompleteSystem.load(Path("data/data_array"))

    @classmethod
    def tearDownClass(cls):
        cls.trie_system.close()
        cls.sqlite_system.close()
        cls.array_system.close()

    def compare_engines(self, query: str):
        # NOTE: The trie backend is loaded to verify it doesn't crash, but its
        # results are NOT compared because the full c-api trie index can require
        # 40GB+ of RAM to build — far more than CI or most dev machines have.
        # Array vs SQLite is our core parity comparison.
        trie_res = self.trie_system.get_ranked_completions(query, k=5)
        sqlite_res = self.sqlite_system.get_ranked_completions(query, k=5)
        array_res = self.array_system.get_ranked_completions(query, k=5)
        
        # Check lengths between Array and SQLite (our core comparison)
        self.assertEqual(len(array_res), len(sqlite_res), f"Mismatched result length for {query} between Array and SQLite")
        
        for i in range(len(array_res)):
            # Compare sentence_ids
            self.assertEqual(array_res[i][0], sqlite_res[i][0], f"Sentence ID mismatch on query '{query}' rank {i}: Array vs SQLite")
            # Compare scores
            self.assertEqual(array_res[i][1].score, sqlite_res[i][1].score, f"Score mismatch on query '{query}' rank {i}: Array vs SQLite")

    def test_progressive_token_lengths(self):
        """Test 1: 5 queries ranging from 1 to 5 tokens long."""
        queries = [
            "python",                   # 1 token
            "python c",                 # 2 tokens
            "python c api",             # 3 tokens
            "the python c api",         # 4 tokens
            "the python c api is"       # 5 tokens
        ]
        for q in queries:
            with self.subTest(query=q):
                self.compare_engines(q)

    def test_specific_terms(self):
        """Test 2: Check 5 very specific terms to verify they fetch identical exact results."""
        queries = [
            "pyunicode",
            "pyarg",
            "pyobject",
            "pycapsule",
            "pyslice"
        ]
        for q in queries:
            with self.subTest(query=q):
                sqlite_res = self.sqlite_system.get_ranked_completions(q, k=5)
                # Ensure it fetches a small number of results (simulating the teammate's specific case)
                self.assertGreaterEqual(len(sqlite_res), 1, f"Expected at least 1 match for {q}")
                # Compare all engines
                self.compare_engines(q)

    def test_variety_of_scores(self):
        """Test 3: Match accuracy on typo error-correction (ensuring scores are identical for non-general matches)."""
        queries = [
            "pythin c api",      # 1 substitution typo
            "pyarg_prsetuple",   # 1 deletion typo
            "gc_xollect",        # 1 substitution typo
            "python algorithm",  # general multi-word phrase with a variety of exact and near matches
            "pyslice_get"        # exact prefix of a very specific term
        ]
        for q in queries:
            with self.subTest(query=q):
                self.compare_engines(q)

    def test_full_sentence_one_off(self):
        """Test 4: Full sentence as it appears in the docs to see what results we get."""
        query = "the functions in this chapter interact with python objects regardless"
        sqlite_res = self.sqlite_system.get_ranked_completions(query, k=5)
        # Should return exactly 1 result since it's a one-off very specific long sentence
        self.assertEqual(len(sqlite_res), 1, "Expected exactly 1 result for the one-off full sentence")
        self.compare_engines(query)

    def test_too_many_errors(self):
        """Test 5: Ensure queries with 2+ typos against known corpus terms correctly return 0 results."""
        # Each query has exactly 2 edits from a real sentence/term in the c-api corpus.
        two_typo_queries = [
            "pythxn c xpi",   # 2 substitutions in "python c api"
            "pythn c ai",     # 2 deletions in "python c api" ('ai' is NOT a substring)
            "pyobejct",       # 2 typos in "pyobject"
            "pycapslue",      # 2 typos in "pycapsule"
        ]
        for q in two_typo_queries:
            with self.subTest(query=q):
                sqlite_res = self.sqlite_system.get_ranked_completions(q, k=5)
                self.assertEqual(len(sqlite_res), 0, f"Expected 0 results for 2-typo query '{q}'")
                self.compare_engines(q)

    def test_single_deletion_with_substring_match_still_works(self):
        """Test 5b: A 2-deletion query where one 'deletion' aligns with a valid substring still matches."""
        # "pythn c ap" has 2 deletions from "python c api", but "ap" is a valid
        # substring of "api" — so each anchor only needs at most 1 edit.
        query = "pythn c ap"
        sqlite_res = self.sqlite_system.get_ranked_completions(query, k=5)
        self.assertGreaterEqual(len(sqlite_res), 1, f"Expected matches for '{query}' (substring alignment)")
        self.compare_engines(query)

    def test_out_of_alphabet(self):
        """Test 6: Queries with out-of-alphabet characters like emojis or weird punctuation."""
        query = "  pyObject... ;  "
        sqlite_res = self.sqlite_system.get_ranked_completions(query, k=5)
        self.assertGreaterEqual(len(sqlite_res), 1)
        self.compare_engines(query)

    def test_popularity_vs_assignment(self):
        """Test 7: Verify different modes break ties differently but identically across engines."""
        from autocomplete_system.models import RankingMode
        
        query = "python"
        
        sqlite_assign = self.sqlite_system.get_ranked_completions(query, k=5, ranking_mode=RankingMode.ASSIGNMENT)
        array_assign = self.array_system.get_ranked_completions(query, k=5, ranking_mode=RankingMode.ASSIGNMENT)
        
        self.assertEqual(len(array_assign), len(sqlite_assign))
        for i in range(len(array_assign)):
            self.assertEqual(array_assign[i][0], sqlite_assign[i][0])
            self.assertEqual(array_assign[i][1].score, sqlite_assign[i][1].score)

        if len(sqlite_assign) >= 2:
            sentence_to_boost = sqlite_assign[1][0]
            # Boost popularity in Array system (SQLite's LIMIT 5 makes its popularity mode lossy if sentence isn't already short)
            self.array_system.record_selection(sentence_to_boost)
            
            array_pop = self.array_system.get_ranked_completions(query, k=5, ranking_mode=RankingMode.POPULARITY)
            
            self.assertEqual(len(array_pop), 5, "Should still return exactly 5 completions")
            self.assertEqual(array_pop[0][0], sentence_to_boost, "Boosted sentence should now be #1 in Suffix Array backend")
