"""Proof of concept for keyboard-distance heuristics.
we can more or less assume that some of the most common mistakes are mistypes on a keyboard, ie wprld instead of world. 
so we should account for that.
This module is currently detached from the strict scoring rules defined in the project,
but serves as a foundation for implementing QWERTY-adjacent penalty softening if approved.
"""

from __future__ import annotations

# Simplified QWERTY adjacency map
QWERTY_NEIGHBORS = {
    'a': {'q', 'w', 's', 'z'},
    'b': {'v', 'g', 'h', 'n'},
    'c': {'x', 'd', 'f', 'v'},
    'd': {'s', 'e', 'r', 'f', 'x', 'c'},
    'e': {'w', '3', '4', 'r', 's', 'd'},
    'f': {'d', 'r', 't', 'g', 'c', 'v'},
    'g': {'f', 't', 'y', 'h', 'v', 'b'},
    'h': {'g', 'y', 'u', 'j', 'b', 'n'},
    'i': {'u', '8', '9', 'o', 'j', 'k'},
    'j': {'h', 'u', 'i', 'k', 'n', 'm'},
    'k': {'j', 'i', 'o', 'l', 'm'},
    'l': {'k', 'o', 'p'},
    'm': {'n', 'j', 'k'},
    'n': {'b', 'h', 'j', 'm'},
    'o': {'i', '9', '0', 'p', 'k', 'l'},
    'p': {'o', '0', 'l'},
    'q': {'1', '2', 'w', 'a'},
    'r': {'e', '4', '5', 't', 'd', 'f'},
    's': {'a', 'w', 'e', 'd', 'z', 'x'},
    't': {'r', '5', '6', 'y', 'f', 'g'},
    'u': {'y', '7', '8', 'i', 'h', 'j'},
    'v': {'c', 'f', 'g', 'b'},
    'w': {'q', '2', '3', 'e', 'a', 's'},
    'x': {'z', 's', 'd', 'c'},
    'y': {'t', '6', '7', 'u', 'g', 'h'},
    'z': {'a', 's', 'x'}
}

def get_qwerty_neighbors(character: str) -> set[str]:
    """Return the set of physically adjacent keys for a given character."""
    return QWERTY_NEIGHBORS.get(character.lower(), set())

def calculate_qwerty_substitution_penalty(position: int) -> int:
    """Proof of concept: a softer penalty for QWERTY-adjacent typos.
    
    Currently detached from the main scoring loop to preserve alphabetical tie-breakers.
    """
    # e.g., max(1, 4 - position) instead of max(1, 6 - position)
    return max(1, 4 - position)
