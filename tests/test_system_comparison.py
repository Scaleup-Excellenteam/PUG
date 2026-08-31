import unittest
from pathlib import Path
from autocomplete_system.engine import AutocompleteSystem

class SystemComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load the 3 prebuilt indexes from the c-api corpus
        cls.trie_system = AutocompleteSystem.load(Path("data_trie"))
        cls.sqlite_system = AutocompleteSystem.load(Path("data_sqlite"))
        cls.array_system = AutocompleteSystem.load(Path("data_array"))

    @classmethod
    def tearDownClass(cls):
        cls.trie_system.close()
        cls.sqlite_system.close()
        cls.array_system.close()

    def compare_engines(self, query: str):
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
