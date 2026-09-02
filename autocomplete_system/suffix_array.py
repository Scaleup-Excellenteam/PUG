"""Flat Suffix Array backend for low memory and fast string matching."""

from __future__ import annotations

import array
import string
from collections import defaultdict

from .models import RankingMode
from .scoring import generate_scored_variants

class SuffixArrayIndex:
    """Flat, bit-packed Suffix Array index with an O(1) prefix lookup cache."""

    __slots__ = ("sentences", "suffix_array", "prefix_cache", "alphabet")

    def __init__(self, alphabet: tuple[str, ...]) -> None:
        self.sentences: list[str] = []
        self.suffix_array = array.array('Q')
        self.prefix_cache: dict[str, tuple[int, int]] = {}
        self.alphabet = alphabet

    def __getstate__(self) -> tuple[list[str], array.array, dict[str, tuple[int, int]], tuple[str, ...]]:
        return self.sentences, self.suffix_array, self.prefix_cache, self.alphabet

    def __setstate__(self, state: tuple[list[str], array.array, dict[str, tuple[int, int]], tuple[str, ...]]) -> None:
        self.sentences, self.suffix_array, self.prefix_cache, self.alphabet = state

    def insert_sentence(
        self,
        normalized_sentence: str,
        sentence_id: int,
        length_key,
        alphabetical_key,
    ) -> None:
        """Add all suffixes of the normalized string into the array."""
        if sentence_id >= len(self.sentences):
            self.sentences.extend([""] * (sentence_id - len(self.sentences) + 1))
        self.sentences[sentence_id] = normalized_sentence

        # Append all suffixes to the flat array
        for offset in range(len(normalized_sentence)):
            # Pack sentence_id into high 32 bits, offset into low 32 bits
            val = (sentence_id << 32) | offset
            self.suffix_array.append(val)

    def build(self) -> None:
        """Sort the array alphabetically to create the real Suffix Array, and cache short prefixes."""
        # Convert to list for efficient custom sorting
        temp_list = self.suffix_array.tolist()
        
        # Sort based on the string slice
        temp_list.sort(key=lambda x: self.sentences[x >> 32][x & 0xFFFFFFFF:])
        
        # Repack into array
        self.suffix_array = array.array('Q', temp_list)
        
        # Build prefix cache for instant lookups on the first 1-3 characters
        self.prefix_cache.clear()
        
        current_prefixes = {}
        for i, val in enumerate(self.suffix_array):
            s_id = val >> 32
            offset = val & 0xFFFFFFFF
            text = self.sentences[s_id][offset:offset+3]
            
            # Record start/end boundaries for prefixes up to 3 chars
            for length in range(1, min(4, len(text) + 1)):
                p = text[:length]
                if p not in current_prefixes:
                    current_prefixes[p] = [i, i]
                else:
                    current_prefixes[p][1] = i
                    
        for p, (start, end) in current_prefixes.items():
            self.prefix_cache[p] = (start, end)

    def _binary_search(self, target: str) -> list[int]:
        """Find all sentence IDs containing the exact target as a prefix using Bisect-style binary search."""
        if not target:
            return []
            
        prefix_len = min(3, len(target))
        bounds = self.prefix_cache.get(target[:prefix_len])
        if bounds is None:
            return []
            
        start, end = bounds
        if len(target) <= 3:
            return [(val >> 32) for val in self.suffix_array[start:end+1]]
            
        # Binary search for left boundary
        low, high = start, end
        first_match = -1
        while low <= high:
            mid = (low + high) // 2
            val = self.suffix_array[mid]
            s = self.sentences[val >> 32][val & 0xFFFFFFFF : (val & 0xFFFFFFFF) + len(target)]
            if s < target:
                low = mid + 1
            elif s > target:
                high = mid - 1
            else:
                first_match = mid
                high = mid - 1
                
        if first_match == -1:
            return []
            
        # Binary search for right boundary
        low, high = first_match, end
        last_match = -1
        while low <= high:
            mid = (low + high) // 2
            val = self.suffix_array[mid]
            s = self.sentences[val >> 32][val & 0xFFFFFFFF : (val & 0xFFFFFFFF) + len(target)]
            if s > target:
                high = mid - 1
            else:
                last_match = mid
                low = mid + 1
                
        return [(val >> 32) for val in self.suffix_array[first_match:last_match+1]]

    def candidate_text_scores(
        self,
        query: str,
        ranking_mode: RankingMode,
        allow_popularity_exact_shortcut: bool = False,
        prioritize_qwerty: bool = False,
        soften_qwerty_penalty: bool = False,
    ) -> dict[int, int]:
        
        # 1. Exact matches first
        exact_candidate_ids = self._binary_search(query)
        best_candidates = {
            sentence_id: 2 * len(query) for sentence_id in exact_candidate_ids
        }
        
        # Early exit if we found enough exact matches and mode allows it
        if len(best_candidates) >= 5 and (
            ranking_mode is RankingMode.ASSIGNMENT or allow_popularity_exact_shortcut
        ):
            return best_candidates
            
        # Generate variants
        edit_alphabet = (
            character for character in self.alphabet
            if character == " " or (character.isascii() and character.isalnum())
        )
        variants = generate_scored_variants(
            query, 
            edit_alphabet,
            prioritize_qwerty=prioritize_qwerty,
            soften_qwerty_penalty=soften_qwerty_penalty,
        )
        variants.pop(query, None)
        
        variants_by_score: dict[int, list[str]] = defaultdict(list)
        for variant, score in variants.items():
            variants_by_score[score].append(variant)
            
        # Branch & Bound
        for score in sorted(variants_by_score.keys(), reverse=True):
            for variant in variants_by_score[score]:
                matches = self._binary_search(variant)
                for m in matches:
                    if m not in best_candidates or best_candidates[m] < score:
                        best_candidates[m] = score
                        
            # If we have 5 candidates and we are in assignment mode, stop searching
            if len(best_candidates) >= 5 and ranking_mode is RankingMode.ASSIGNMENT:
                break
                
        return best_candidates
