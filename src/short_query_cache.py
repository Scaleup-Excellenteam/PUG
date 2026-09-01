import gzip
import pickle
import itertools
from typing import Callable, Dict, Any, Iterator

DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyz "

def generate_combinations(alphabet: str, max_length: int) -> Iterator[str]:
    """
    Yields all possible string combinations of lengths 1 to max_length
    using the provided alphabet.
    """
    for length in range(1, max_length + 1):
        for combo in itertools.product(alphabet, repeat=length):
            yield "".join(combo)

def build_cache(search_callback: Callable[[str], Any], 
                max_length: int = 4, 
                alphabet: str = DEFAULT_ALPHABET) -> Dict[str, Any]:
    """
    Builds the short query cache by iterating through all combinations up to max_length.
    It calls search_callback(query). If the callback returns a non-empty result,
    it stores it in the cache to prune 'junk' queries.
    
    Args:
        search_callback: A function that takes a query string and returns a list of results.
        max_length: Maximum length of queries to generate.
        alphabet: String containing all allowed characters.
        
    Returns:
        A dictionary mapping the query string to its search results.
    """
    cache = {}
    for query in generate_combinations(alphabet, max_length):
        results = search_callback(query)
        if results:  # Only store if there are matches (pruning junk)
            cache[query] = results
    return cache

def save_cache(cache: Dict[str, Any], filepath: str) -> None:
    """
    Saves the cache to a file using pickle and gzip compression for efficiency.
    """
    with gzip.open(filepath, 'wb') as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)

def load_cache(filepath: str) -> Dict[str, Any]:
    """
    Loads the cache from a gzipped pickle file.
    """
    with gzip.open(filepath, 'rb') as f:
        return pickle.load(f)
