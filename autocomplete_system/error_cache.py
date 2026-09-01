"""Asynchronous Aho-Corasick Error Cache.

This module implements a dynamic typo cache that uses an Aho-Corasick Automaton 
to scan user queries in O(L) time. It supports asynchronous background rebuilds 
to prevent thread-locking during read operations.
"""

from __future__ import annotations

import threading
from collections import deque

class ErrorCache:
    def __init__(self, max_size: int = 10000):
        self.max_size = max_size
        self.cache: dict[str, str] = {}  # Maps mistake -> correction
        self.queue: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        
        # DFA State (swapped atomically after background rebuilds)
        self._goto: dict[int, dict[str, int]] = {0: {}}
        self._fail: dict[int, int] = {}
        self._output: dict[int, list[tuple[str, str]]] = {}

    def submit_correction(self, mistake: str, correction: str) -> None:
        """Submit a novel mistake->correction pair to the async queue."""
        with self.lock:
            # Prevent queue from growing infinitely if background cycle is stalled
            if len(self.queue) < self.max_size:
                self.queue.append((mistake, correction))

    def rebuild_cycle(self) -> None:
        """Background task to consume the queue and rebuild the DFA.
        
        This should be called periodically by a background worker/thread.
        """
        with self.lock:
            if not self.queue:
                return
            
            # 1. Update our LRU/dictionary
            for mistake, correction in self.queue:
                self.cache[mistake] = correction
            self.queue.clear()
            
            # Simple LRU eviction if we exceed max_size
            if len(self.cache) > self.max_size:
                # Remove oldest elements (dict insertion order is preserved in Python 3.7+)
                excess = len(self.cache) - self.max_size
                for k in list(self.cache.keys())[:excess]:
                    del self.cache[k]
                    
            # 2. Rebuild the DFA on a local copy to prevent blocking reads
            new_goto = {0: {}}
            new_output = {}
            state_count = 1
            
            for mistake, correction in self.cache.items():
                curr = 0
                for char in mistake:
                    if char not in new_goto.get(curr, {}):
                        new_goto.setdefault(curr, {})[char] = state_count
                        new_goto[state_count] = {}
                        state_count += 1
                    curr = new_goto[curr][char]
                new_output.setdefault(curr, []).append((mistake, correction))
            
            new_fail = {}
            q = deque()
            for char, next_state in new_goto.get(0, {}).items():
                new_fail[next_state] = 0
                q.append(next_state)
            
            while q:
                curr = q.popleft()
                for char, next_state in new_goto.get(curr, {}).items():
                    q.append(next_state)
                    fail_state = new_fail.get(curr, 0)
                    while fail_state != 0 and char not in new_goto.get(fail_state, {}):
                        fail_state = new_fail.get(fail_state, 0)
                    
                    new_fail[next_state] = new_goto.get(fail_state, {}).get(char, 0)
                    if new_output.get(new_fail[next_state]):
                        new_output.setdefault(next_state, []).extend(new_output[new_fail[next_state]])
            
            # 3. Atomic swap (Python pointer swaps are GIL-protected atomic operations)
            self._goto = new_goto
            self._fail = new_fail
            self._output = new_output

    def scan_and_replace(self, query: str) -> str:
        """Scan the query in O(L) time and replace the longest found mistake."""
        if not self._goto.get(0):
            return query
            
        curr = 0
        best_replacement = None
        
        for i, char in enumerate(query):
            while curr != 0 and char not in self._goto.get(curr, {}):
                curr = self._fail.get(curr, 0)
            curr = self._goto.get(curr, {}).get(char, 0)
            
            if curr in self._output:
                for mistake, correction in self._output[curr]:
                    # Keep track of the longest replacement found
                    if not best_replacement or len(mistake) > len(best_replacement[1]):
                        start_idx = i - len(mistake) + 1
                        best_replacement = (start_idx, mistake, correction)
        
        # Apply the replacement
        if best_replacement:
            start_idx, mistake, correction = best_replacement
            end_idx = start_idx + len(mistake)
            return query[:start_idx] + correction + query[end_idx:]
            
        return query
