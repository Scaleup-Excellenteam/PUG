"""Shared edit penalties and exact one-edit variant generation."""

from __future__ import annotations

from collections.abc import Iterable


def substitution_penalty(position: int) -> int:
    return max(1, 6 - position)


def indel_penalty(position: int) -> int:
    return 2 * max(1, 6 - position)


def generate_scored_variants(query: str, alphabet: Iterable[str]) -> dict[str, int]:
    """Generate stored substrings reachable from ``query`` with at most one edit."""

    variants = {query: 2 * len(query)}
    characters = tuple(sorted(set(alphabet)))

    def keep_best(variant: str, score: int) -> None:
        if variant:
            variants[variant] = max(score, variants.get(variant, score))

    for index, query_character in enumerate(query):
        position = index + 1
        substitution_score = 2 * (len(query) - 1) - substitution_penalty(position)
        for replacement in characters:
            if replacement != query_character:
                keep_best(
                    query[:index] + replacement + query[index + 1 :],
                    substitution_score,
                )
        keep_best(
            query[:index] + query[index + 1 :],
            2 * (len(query) - 1) - indel_penalty(position),
        )

    for index in range(len(query) + 1):
        insertion_score = 2 * len(query) - indel_penalty(index + 1)
        for inserted_character in characters:
            keep_best(
                query[:index] + inserted_character + query[index:],
                insertion_score,
            )
    return variants
