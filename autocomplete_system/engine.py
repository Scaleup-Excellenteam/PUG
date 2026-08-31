"""Online approximate search, ranking, and popularity updates."""

from __future__ import annotations

from pathlib import Path

from .constants import ALPHA
from .models import AutoCompleteData, RankingMode, SentenceRecord
from .normalization import normalize_text
from .sqlite_index import SQLiteSubstringIndex
from .storage import SearchIndex, load_index, save_usage_stats


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
        index, master_array = load_index(data_directory)
        return cls(index, master_array, data_directory, ranking_mode)

    def get_ranked_completions(
        self,
        prefix: str,
        k: int = 5,
        ranking_mode: RankingMode | None = None,
    ) -> list[tuple[int, AutoCompleteData]]:
        """Return result IDs and public completion records, best first."""

        normalized_query = normalize_text(prefix)
        if not normalized_query or k <= 0:
            return []

        mode = self.ranking_mode if ranking_mode is None else ranking_mode
        if isinstance(self.index, SQLiteSubstringIndex) or type(self.index).__name__ == 'SuffixArrayIndex':
            text_scores = self.index.candidate_text_scores(
                normalized_query,
                mode,
                allow_popularity_exact_shortcut=(
                    mode is RankingMode.POPULARITY and not self._has_usage_counts
                ),
            )
        else:
            text_scores = self.index.candidate_text_scores(normalized_query, mode)
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
        return results

    def get_best_k_completions(self, prefix: str) -> list[AutoCompleteData]:
        """Return up to five autocomplete results for ``prefix``."""

        return [completion for _, completion in self.get_ranked_completions(prefix)]

    def record_selection(self, sentence_id: int) -> None:
        """Increment one selected sentence's usage count."""

        if sentence_id < 0 or sentence_id >= len(self.master_array):
            raise IndexError(f"Unknown sentence ID: {sentence_id}")
        self.master_array[sentence_id].usage_count += 1
        self._has_usage_counts = True

    def save_usage_stats(self) -> None:
        """Persist popularity data when this system has a data directory."""

        if self.data_directory is None:
            raise ValueError("No data directory is configured for this system.")
        save_usage_stats(self.data_directory, self.master_array)

    def close(self) -> None:
        """Release resources held by a disk-backed index."""

        if isinstance(self.index, SQLiteSubstringIndex):
            self.index.close()
