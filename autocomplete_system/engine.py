"""Online approximate search, ranking, and popularity updates."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .constants import ALPHA, MAX_NODE_CACHE_SIZE
from .models import AutoCompleteData, RankingMode, SentenceRecord
from .logging_config import log_event
from .normalization import normalize_text
from .sqlite_index import SQLiteSubstringIndex
from .storage import SearchIndex, load_index, save_usage_stats
from autocomplete_system.error_cache import ErrorCache


LOGGER = logging.getLogger("autocomplete.engine")


class AutocompleteSystem:
    """A searchable immutable index plus mutable usage statistics."""

    def __init__(
        self,
        index: SearchIndex,
        master_array: list[SentenceRecord],
        data_directory: Path | None = None,
        ranking_mode: RankingMode = RankingMode.ASSIGNMENT,
    ) -> None:
        self.index = index
        self.master_array = master_array
        self.data_directory = data_directory
        self.ranking_mode = ranking_mode
        self._has_usage_counts = any(record.usage_count for record in master_array)
        self.error_cache = ErrorCache()
        self._search_count = 0
        # Call on activation for demonstration
        self._run_error_cache_worker(trigger="activation")

    def _run_error_cache_worker(self, trigger: str) -> None:
        """Execute an error cache background cycle with operational logging."""
        started = time.perf_counter()
        queued_items = len(self.error_cache.queue)
        self.error_cache.rebuild_cycle()
        log_event(
            LOGGER,
            "error_cache_worker_executed",
            trigger=trigger,
            search_count=self._search_count,
            queued_items=queued_items,
            cache_size=len(self.error_cache.cache),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    @property
    def trie(self) -> SearchIndex:
        """Backward-compatible name for callers that used the original API."""

        return self.index

    @classmethod
    def load(
        cls,
        data_directory: Path,
        ranking_mode: RankingMode = RankingMode.ASSIGNMENT,
    ) -> AutocompleteSystem:
        started = time.perf_counter()
        log_event(
            LOGGER,
            "system_load_started",
            data_directory=str(data_directory),
            ranking_mode=ranking_mode.value,
        )
        index, master_array = load_index(data_directory)
        system = cls(index, master_array, data_directory, ranking_mode)
        log_event(
            LOGGER,
            "system_load_completed",
            data_directory=str(data_directory),
            ranking_mode=ranking_mode.value,
            backend=type(index).__name__,
            sentence_count=len(master_array),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return system

    def get_ranked_completions(
        self,
        prefix: str,
        k: int = 5,
        ranking_mode: RankingMode | None = None,
    ) -> list[tuple[int, AutoCompleteData]]:
        """Return result IDs and public completion records, best first."""

        started = time.perf_counter()
        normalized_query = normalize_text(prefix)
        if not normalized_query or k <= 0:
            log_event(
                LOGGER,
                "search_completed",
                query=prefix,
                normalized_query=normalized_query,
                requested_k=k,
                result_count=0,
                results=[],
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                reason="empty_normalized_query_or_nonpositive_k",
            )
            return []

        # Phase 1: Fast O(L) scan and replace via Aho-Corasick DFA
        search_query = self.error_cache.scan_and_replace(normalized_query)
        cache_hit = search_query != normalized_query

        mode = self.ranking_mode if ranking_mode is None else ranking_mode

        # Candidate retrieval must not change when popularity is toggled. Both
        # modes search the mandatory length-ranked node cache; the active mode
        # affects only the final usage-count bonus below. Passing ASSIGNMENT to
        # the index would select its legacy alphabetical cache and could return
        # an entirely different group of sentences for the same query.
        candidate_cache_mode = RankingMode.POPULARITY
        if isinstance(self.index, SQLiteSubstringIndex) or type(self.index).__name__ == 'SuffixArrayIndex':
            text_scores = self.index.candidate_text_scores(
                search_query,
                candidate_cache_mode,
                allow_popularity_exact_shortcut=(
                    not self._has_usage_counts
                ),
            )
        else:
            text_scores = self.index.candidate_text_scores(
                search_query,
                candidate_cache_mode,
            )
        ranked: list[tuple[int, int]] = []
        for sentence_id, text_score in text_scores.items():
            final_score = text_score
            if mode is RankingMode.POPULARITY:
                final_score += ALPHA * self.master_array[sentence_id].usage_count
            ranked.append((sentence_id, final_score))

        ranked.sort(
            key=lambda item: (
                -item[1],
                self.master_array[item[0]].normalized_text,
                self.master_array[item[0]].original_text,
                self.master_array[item[0]].source_path,
                self.master_array[item[0]].line_number,
                item[0],
            )
        )

        results: list[tuple[int, AutoCompleteData]] = []
        for sentence_id, final_score in ranked[:k]:
            record = self.master_array[sentence_id]
            results.append(
                (
                    sentence_id,
                    AutoCompleteData(
                        completed_sentence=record.original_text,
                        source_text=record.source_path,
                        offset=record.line_number,
                        score=int(final_score),
                    ),
                )
            )
        log_event(
            LOGGER,
            "search_completed",
            query=prefix,
            normalized_query=normalized_query,
            requested_k=k,
            ranking_mode=mode.value,
            backend=type(self.index).__name__,
            candidate_count=len(text_scores),
            result_count=len(results),
            results=[
                {
                    "sentence_id": sentence_id,
                    "completed_sentence": completion.completed_sentence,
                    "source_text": completion.source_text,
                    "offset": completion.offset,
                    "score": completion.score,
                }
                for sentence_id, completion in results
            ],
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )

        # Phase 2: Organic Learning (Asynchronous Write Path)
        # If we didn't use the cache, but found a typo correction organically, submit it!
        if not cache_hit and results:
            best_id, best_data = results[0]
            # A score < 2*len means we used a 1-edit penalty to find it
            if best_data.score < 2 * len(normalized_query):
                record_norm = self.master_array[best_id].normalized_text
                # Find which 1-edit variant successfully matched
                from autocomplete_system.scoring import generate_scored_variants

                # Use a basic english alphabet for recovery
                alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 "
                variants = generate_scored_variants(normalized_query, alphabet)

                best_variant = None
                best_variant_score = -1
                for var, sc in variants.items():
                    if sc > best_variant_score and var in record_norm:
                        best_variant = var
                        best_variant_score = sc

                if best_variant:
                    self.error_cache.submit_correction(normalized_query, best_variant)

        # TODO - implement: periodic timed cycle will be determined later;
        # using a 10-call cycle for demonstration.
        self._search_count += 1
        if self._search_count % 10 == 0:
            self._run_error_cache_worker(trigger="10_call_cycle")

        return results

    def get_best_k_completions(
        self,
        prefix: str,
        k: int = 5,
    ) -> list[AutoCompleteData]:
        """Return up to ``k`` autocomplete results for ``prefix``."""

        return [
            completion
            for _, completion in self.get_ranked_completions(prefix, k=k)
        ]

    def get_next_word(self, prefix: str) -> str | None:
        """Return the normalized continuation at the first matching context."""

        normalized_prefix = normalize_text(prefix)
        if not normalized_prefix:
            return None

        completions = self.get_best_k_completions(
            prefix,
            k=MAX_NODE_CACHE_SIZE,
        )
        for completion in completions:
            normalized_sentence = normalize_text(completion.completed_sentence)
            match_start = normalized_sentence.find(normalized_prefix)
            if match_start < 0:
                continue

            cursor = match_start + len(normalized_prefix)
            if prefix[-1:].isspace():
                # A trailing space explicitly ends the current word.  Do not
                # mistake the remainder of a longer word (for example,
                # ``debe `` matching ``debecho``) for a next-word prediction.
                if (
                    cursor >= len(normalized_sentence)
                    or normalized_sentence[cursor] != " "
                ):
                    continue
                while cursor < len(normalized_sentence) and normalized_sentence[cursor] == " ":
                    cursor += 1
                word_end = normalized_sentence.find(" ", cursor)
                if word_end < 0:
                    word_end = len(normalized_sentence)
                next_word = normalized_sentence[cursor:word_end]
                if next_word:
                    return next_word
                continue

            word_end = normalized_sentence.find(" ", cursor)
            if word_end < 0:
                word_end = len(normalized_sentence)
            continuation = normalized_sentence[cursor:word_end]
            if continuation:
                return continuation

        return None

    def record_selection(self, sentence_id: int) -> None:
        """Increment one selected sentence's usage count."""

        if sentence_id < 0 or sentence_id >= len(self.master_array):
            raise IndexError(f"Unknown sentence ID: {sentence_id}")
        self.master_array[sentence_id].usage_count += 1
        self._has_usage_counts = True
        record = self.master_array[sentence_id]
        log_event(
            LOGGER,
            "selection_recorded",
            sentence_id=sentence_id,
            completed_sentence=record.original_text,
            source_text=record.source_path,
            offset=record.line_number,
            usage_count=record.usage_count,
        )

    def reset_usage_counts(self) -> None:
        """Reset every popularity counter without changing the search index."""

        for record in self.master_array:
            record.usage_count = 0
        self._has_usage_counts = False
        log_event(
            LOGGER,
            "popularity_reset",
            sentence_count=len(self.master_array),
        )

    def save_usage_stats(self) -> None:
        """Persist popularity data when this system has a data directory."""

        if self.data_directory is None:
            raise ValueError("No data directory is configured for this system.")
        save_usage_stats(self.data_directory, self.master_array)
        log_event(
            LOGGER,
            "usage_stats_saved",
            data_directory=str(self.data_directory),
        )

    def close(self) -> None:
        """Release resources held by a disk-backed index."""

        if isinstance(self.index, SQLiteSubstringIndex):
            self.index.close()
        log_event(LOGGER, "system_closed", backend=type(self.index).__name__)
