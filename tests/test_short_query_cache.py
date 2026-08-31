import unittest
import tempfile
import os
import sys

# Ensure we can import from src
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.short_query_cache import (
    generate_combinations, 
    build_cache, 
    save_cache, 
    load_cache,
    DEFAULT_ALPHABET
)

class TestShortQueryCache(unittest.TestCase):
    
    def test_generate_combinations(self):
        # Test with a very small alphabet and max_length to verify math
        alphabet = "ab"
        max_length = 2
        
        combinations = list(generate_combinations(alphabet, max_length))
        
        # Length 1: "a", "b"
        # Length 2: "aa", "ab", "ba", "bb"
        # Total = 2 + 4 = 6
        self.assertEqual(len(combinations), 6)
        self.assertIn("a", combinations)
        self.assertIn("bb", combinations)
        
        # Verify the actual default alphabet size logic
        # For L=1, it should yield exactly len(alphabet)
        l1_count = len(list(generate_combinations(DEFAULT_ALPHABET, 1)))
        self.assertEqual(l1_count, 27)

    def test_build_cache_prunes_junk(self):
        # A mock search function that only returns results for specific strings
        def mock_search(query):
            valid_queries = {
                "a": [("doc1", 10)],
                "ab": [("doc2", 15)],
                "hi ": [("doc3", 20)]
            }
            return valid_queries.get(query, []) # Returns empty list for junk
        
        # Build cache using a subset alphabet and L=2 to make it fast
        # Note: "hi " has length 3, so it shouldn't be found if max_length is 2
        cache = build_cache(mock_search, max_length=2, alphabet="abh i")
        
        # It should ONLY contain "a" and "ab" because those return matches and are length <= 2.
        # "hi " is length 3 so it's not generated, and junk like "b", "h", "hh" return empty and are pruned.
        self.assertEqual(len(cache), 2)
        self.assertIn("a", cache)
        self.assertIn("ab", cache)
        self.assertNotIn("b", cache)
        self.assertNotIn("hi ", cache)

    def test_save_and_load_cache(self):
        dummy_cache = {
            "test": [1, 2, 3],
            "hello": [{"id": 1, "score": 100}]
        }
        
        # Create a temporary file to safely test file I/O
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            filepath = tmp.name
            
        try:
            save_cache(dummy_cache, filepath)
            loaded_cache = load_cache(filepath)
            
            self.assertEqual(dummy_cache, loaded_cache)
        finally:
            os.remove(filepath)

if __name__ == '__main__':
    unittest.main()
