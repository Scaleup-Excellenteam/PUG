"""Disk-backed substring index for corpora too large for Python Trie nodes."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

from .constants import MAX_NODE_CACHE_SIZE, SQLITE_VARIANT_BATCH_SIZE
from .models import RankingMode
from .scoring import generate_scored_variants, indel_penalty, substitution_penalty


class SQLiteSubstringIndex:
    """FTS5 trigram index with exact one-edit variant scoring."""

    __slots__ = (
        "database_filename",
        "alphabet",
        "short_assignment_cache",
        "short_length_cache",
        "_database_path",
        "_connection",
    )

    def __init__(
        self,
        database_filename: str,
        alphabet: tuple[str, ...],
        short_assignment_cache: dict[str, list[int]],
        short_length_cache: dict[str, list[int]],
    ) -> None:
        self.database_filename = database_filename
        self.alphabet = alphabet
        self.short_assignment_cache = short_assignment_cache
        self.short_length_cache = short_length_cache
        self._database_path: Path | None = None
        self._connection: sqlite3.Connection | None = None

    def attach(self, data_directory: Path) -> None:
        self.close()
        self._database_path = data_directory / self.database_filename

    def _connect(self) -> sqlite3.Connection:
        if self._database_path is None:
            raise RuntimeError("The SQLite index is not attached to a data directory.")
        if self._connection is None:
            self._connection = sqlite3.connect(self._database_path)
            self._connection.execute("PRAGMA query_only = ON")
            self._connection.execute("PRAGMA cache_size = -65536")
            self._connection.execute("PRAGMA mmap_size = 30000000000")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __getstate__(self) -> tuple[
        str,
        tuple[str, ...],
        dict[str, list[int]],
        dict[str, list[int]],
    ]:
        return (
            self.database_filename,
            self.alphabet,
            self.short_assignment_cache,
            self.short_length_cache,
        )

    def __setstate__(
        self,
        state: tuple[
            str,
            tuple[str, ...],
            dict[str, list[int]],
            dict[str, list[int]],
        ],
    ) -> None:
        (
            self.database_filename,
            self.alphabet,
            self.short_assignment_cache,
            self.short_length_cache,
        ) = state
        self._database_path = None
        self._connection = None

    @staticmethod
    def _fts_expression(variants: list[str]) -> str:
        return " OR ".join(
            f'"{variant.replace(chr(34), chr(34) * 2)}"' for variant in variants
        )

    def _query_long_variants(
        self,
        variants: list[str],
        ranking_mode: RankingMode,
    ) -> list[int]:
        if not variants:
            return []
        table = (
            "assignment_fts"
            if ranking_mode is RankingMode.ASSIGNMENT
            else "length_fts"
        )
        sql = f"""
            SELECT sentence_id
            FROM {table}
            WHERE {table} MATCH ?
            ORDER BY rowid
            LIMIT ?
        """
        rows = self._connect().execute(
            sql,
            (self._fts_expression(variants), MAX_NODE_CACHE_SIZE),
        )
        return [int(row[0]) for row in rows]

    def _query_single_wildcard(
        self,
        query_prefix: str,
        query_suffix: str,
        ranking_mode: RankingMode,
    ) -> list[int]:
        anchors = [part for part in (query_prefix, query_suffix) if len(part) >= 3]
        if not anchors:
            return []
        match_expression = " AND ".join(
            self._fts_expression([anchor]) for anchor in anchors
        )
        table = (
            "assignment_fts"
            if ranking_mode is RankingMode.ASSIGNMENT
            else "length_fts"
        )
        sql = f"""
            SELECT sentence_id
            FROM {table}
            WHERE {table} MATCH ?
              AND normalized GLOB ?
            ORDER BY rowid
            LIMIT ?
        """
        rows = self._connect().execute(
            sql,
            (
                match_expression,
                f"*{query_prefix}?{query_suffix}*",
                MAX_NODE_CACHE_SIZE,
            ),
        )
        return [int(row[0]) for row in rows]

    @staticmethod
    def _keep_candidate_scores(
        best_candidates: dict[int, int],
        candidate_ids: list[int] | set[int],
        score: int,
    ) -> None:
        for sentence_id in candidate_ids:
            previous = best_candidates.get(sentence_id)
            if previous is None or score > previous:
                best_candidates[sentence_id] = score

    @staticmethod
    def _assignment_top_five_are_final(
        best_candidates: dict[int, int],
        current_score: int,
        ranking_mode: RankingMode,
    ) -> bool:
        if ranking_mode is not RankingMode.ASSIGNMENT or len(best_candidates) < 5:
            return False
        fifth_score = sorted(best_candidates.values(), reverse=True)[4]
        return fifth_score >= current_score

    def _candidate_scores_from_patterns(
        self,
        query: str,
        ranking_mode: RankingMode,
        best_candidates: dict[int, int],
        short_cache: dict[str, list[int]],
    ) -> dict[int, int]:
        operations: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
        query_length = len(query)

        for index in range(query_length):
            position = index + 1
            substitution_score = (
                2 * (query_length - 1) - substitution_penalty(position)
            )
            operations[substitution_score].append(
                ("wildcard", query[:index], query[index + 1 :])
            )

            deleted_variant = query[:index] + query[index + 1 :]
            deletion_score = 2 * (query_length - 1) - indel_penalty(position)
            operations[deletion_score].append(("exact", deleted_variant, ""))

        for index in range(query_length + 1):
            insertion_score = 2 * query_length - indel_penalty(index + 1)
            operations[insertion_score].append(
                ("wildcard", query[:index], query[index:])
            )

        for score in sorted(operations, reverse=True):
            exact_variants: list[str] = []
            wildcard_patterns: list[tuple[str, str]] = []
            for operation_kind, prefix, suffix in operations[score]:
                if operation_kind == "exact":
                    exact_variants.append(prefix)
                else:
                    wildcard_patterns.append((prefix, suffix))

            exact_candidate_ids: set[int] = set()
            long_exact_variants: list[str] = []
            for variant in exact_variants:
                if len(variant) < 3:
                    exact_candidate_ids.update(short_cache.get(variant, ()))
                else:
                    long_exact_variants.append(variant)
            for start in range(0, len(long_exact_variants), SQLITE_VARIANT_BATCH_SIZE):
                exact_candidate_ids.update(
                    self._query_long_variants(
                        long_exact_variants[
                            start : start + SQLITE_VARIANT_BATCH_SIZE
                        ],
                        ranking_mode,
                    )
                )
            self._keep_candidate_scores(best_candidates, exact_candidate_ids, score)

            for prefix, suffix in wildcard_patterns:
                candidate_ids = self._query_single_wildcard(
                    prefix, suffix, ranking_mode
                )
                self._keep_candidate_scores(best_candidates, candidate_ids, score)

            if self._assignment_top_five_are_final(
                best_candidates, score, ranking_mode
            ):
                break

        return best_candidates

    def candidate_text_scores(
        self,
        query: str,
        ranking_mode: RankingMode,
        allow_popularity_exact_shortcut: bool = False,
        prioritize_qwerty: bool = False,
        soften_qwerty_penalty: bool = False,
    ) -> dict[int, int]:
        short_cache = (
            self.short_assignment_cache
            if ranking_mode is RankingMode.ASSIGNMENT
            else self.short_length_cache
        )
        if len(query) < 3:
            exact_candidate_ids = list(short_cache.get(query, ()))
        else:
            exact_candidate_ids = self._query_long_variants([query], ranking_mode)
        best_candidates = {
            sentence_id: 2 * len(query) for sentence_id in exact_candidate_ids
        }

        # In assignment mode text score is the primary sort key. Five exact
        # matches already dominate every edited match, so no approximate work
        # can change the public top five.
        if len(best_candidates) >= 5 and (
            ranking_mode is RankingMode.ASSIGNMENT
            or allow_popularity_exact_shortcut
        ):
            return best_candidates

        if len(query) >= 7:
            return self._candidate_scores_from_patterns(
                query,
                ranking_mode,
                best_candidates,
                short_cache,
            )

        # The supplied corpus is English. Punctuation is removed during
        # normalization, so the scalable backend's edit alphabet is the
        # observed ASCII letters, digits, and space. Exact queries may still
        # contain and match any Unicode character.
        edit_alphabet = (
            character
            for character in self.alphabet
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

        for score in sorted(variants_by_score, reverse=True):
            score_variants = variants_by_score[score]
            candidate_ids: set[int] = set()
            long_variants: list[str] = []

            for variant in score_variants:
                if len(variant) < 3:
                    candidate_ids.update(short_cache.get(variant, ()))
                else:
                    long_variants.append(variant)

            for start in range(0, len(long_variants), SQLITE_VARIANT_BATCH_SIZE):
                candidate_ids.update(
                    self._query_long_variants(
                        long_variants[start : start + SQLITE_VARIANT_BATCH_SIZE],
                        ranking_mode,
                    )
                )

            for sentence_id in candidate_ids:
                previous = best_candidates.get(sentence_id)
                if previous is None or score > previous:
                    best_candidates[sentence_id] = score

            if self._assignment_top_five_are_final(
                best_candidates, score, ranking_mode
            ):
                break

        return best_candidates
