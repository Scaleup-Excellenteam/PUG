"""Path-compressed all-character suffix Trie and one-edit DFS search."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable

from .constants import MAX_NODE_CACHE_SIZE
from .models import RankingMode
from .scoring import indel_penalty, substitution_penalty
from .qwerty import get_qwerty_neighbors, calculate_qwerty_substitution_penalty

CandidateSortKey = Callable[[int], tuple[object, ...]]
CACHE_DEPTH_LIMIT = 20


class RadixEdge:
    __slots__ = ("label", "child")

    def __init__(self, label: str, child: TrieNode) -> None:
        self.label = label
        self.child = child


class TrieNode:
    """Compact explicit node containing only ID caches and child edges."""

    __slots__ = ("children", "candidate_ids", "assignment_candidate_ids", "exact_match_ids")

    def __init__(self) -> None:
        self.children: dict[str, RadixEdge] = {}
        self.candidate_ids: list[int] = []
        self.assignment_candidate_ids: list[int] = []
        self.exact_match_ids: set[int] = set()


class CompressedSuffixTrie:
    """Radix Trie containing the suffix beginning at every character offset."""

    __slots__ = ("root",)

    def __init__(self) -> None:
        self.root = TrieNode()

    @staticmethod
    def _cache_candidate(
        candidates: list[int], sentence_id: int, sort_key: CandidateSortKey
    ) -> None:
        if sentence_id in candidates:
            return
        insertion_index = bisect_left(
            candidates, sort_key(sentence_id), key=sort_key
        )
        if insertion_index >= MAX_NODE_CACHE_SIZE:
            return
        candidates.insert(insertion_index, sentence_id)
        if len(candidates) > MAX_NODE_CACHE_SIZE:
            candidates.pop()

    @classmethod
    def _cache_both(
        cls,
        node: TrieNode,
        sentence_id: int,
        length_key: CandidateSortKey,
        alphabetical_key: CandidateSortKey,
        depth: int,
    ) -> None:
        if depth > CACHE_DEPTH_LIMIT:
            return
        cls._cache_candidate(node.candidate_ids, sentence_id, length_key)
        cls._cache_candidate(
            node.assignment_candidate_ids, sentence_id, alphabetical_key
        )

    def insert_suffix(
        self,
        suffix: str,
        sentence_id: int,
        length_key: CandidateSortKey,
        alphabetical_key: CandidateSortKey,
    ) -> None:
        if not suffix:
            return
        node = self.root
        remaining = suffix
        current_depth = 0
        self._cache_both(node, sentence_id, length_key, alphabetical_key, current_depth)

        while remaining:
            edge = node.children.get(remaining[0])
            if edge is None:
                child = TrieNode()
                current_depth += len(remaining)
                self._cache_both(child, sentence_id, length_key, alphabetical_key, current_depth)
                child.exact_match_ids.add(sentence_id)
                node.children[remaining[0]] = RadixEdge(remaining, child)
                return

            common_length = 0
            maximum = min(len(remaining), len(edge.label))
            while (
                common_length < maximum
                and remaining[common_length] == edge.label[common_length]
            ):
                common_length += 1

            if common_length == len(edge.label):
                remaining = remaining[common_length:]
                node = edge.child
                current_depth += common_length
                self._cache_both(node, sentence_id, length_key, alphabetical_key, current_depth)
                if not remaining:
                    node.exact_match_ids.add(sentence_id)
                continue

            split_node = TrieNode()
            split_node.candidate_ids = edge.child.candidate_ids.copy()
            split_node.assignment_candidate_ids = (
                edge.child.assignment_candidate_ids.copy()
            )
            current_depth += common_length
            self._cache_both(
                split_node, sentence_id, length_key, alphabetical_key, current_depth
            )

            old_remainder = edge.label[common_length:]
            split_node.children[old_remainder[0]] = RadixEdge(
                old_remainder, edge.child
            )
            node.children[remaining[0]] = RadixEdge(
                edge.label[:common_length], split_node
            )

            new_remainder = remaining[common_length:]
            if new_remainder:
                new_child = TrieNode()
                self._cache_both(
                    new_child, sentence_id, length_key, alphabetical_key, current_depth + len(new_remainder)
                )
                new_child.exact_match_ids.add(sentence_id)
                split_node.children[new_remainder[0]] = RadixEdge(
                    new_remainder, new_child
                )
            else:
                split_node.exact_match_ids.add(sentence_id)
            return

    def insert_sentence(
        self,
        normalized_sentence: str,
        sentence_id: int,
        length_key: CandidateSortKey,
        alphabetical_key: CandidateSortKey,
    ) -> None:
        for start_index in range(len(normalized_sentence)):
            self.insert_suffix(
                normalized_sentence[start_index:],
                sentence_id,
                length_key,
                alphabetical_key,
            )

    @staticmethod
    def _next_characters(
        node: TrieNode | None, edge: RadixEdge | None, edge_offset: int
    ) -> list[tuple[str, TrieNode | None, RadixEdge | None, int]]:
        if edge is not None:
            character = edge.label[edge_offset]
            next_offset = edge_offset + 1
            if next_offset == len(edge.label):
                return [(character, edge.child, None, 0)]
            return [(character, None, edge, next_offset)]

        assert node is not None
        transitions = []
        for character, child_edge in node.children.items():
            if len(child_edge.label) == 1:
                transitions.append((character, child_edge.child, None, 0))
            else:
                transitions.append((character, None, child_edge, 1))
        return transitions

    @staticmethod
    def _cursor_node(node: TrieNode | None, edge: RadixEdge | None) -> TrieNode:
        if edge is not None:
            return edge.child
        assert node is not None
        return node

    @staticmethod
    def _collect_dfs(node: TrieNode) -> set[int]:
        stack = [node]
        results = set()
        while stack:
            curr = stack.pop()
            results.update(curr.exact_match_ids)
            for edge in curr.children.values():
                stack.append(edge.child)
        return results

    def candidate_text_scores(
        self,
        query: str,
        ranking_mode: RankingMode,
        allow_popularity_exact_shortcut: bool = False,
        prioritize_qwerty: bool = False,
        soften_qwerty_penalty: bool = False,
    ) -> dict[int, int]:
        """Intersect the Trie with a Levenshtein automaton of budget one."""

        stack: list[
            tuple[TrieNode | None, RadixEdge | None, int, int, bool, int]
        ] = [(self.root, None, 0, 0, False, 0)]
        best_states: dict[tuple[int, int, int, int, bool], int] = {}
        best_candidates: dict[int, int] = {}

        while stack:
            node, edge, edge_offset, query_index, edit_used, score = stack.pop()
            state_key = (
                id(node) if node is not None else 0,
                id(edge) if edge is not None else 0,
                edge_offset,
                query_index,
                edit_used,
            )
            previous_score = best_states.get(state_key)
            if previous_score is not None and previous_score >= score:
                continue
            best_states[state_key] = score

            if query_index == len(query):
                cursor_node = self._cursor_node(node, edge)
                candidates = (
                    cursor_node.assignment_candidate_ids
                    if ranking_mode is RankingMode.ASSIGNMENT
                    else cursor_node.candidate_ids
                )
                if not candidates:
                    candidates = self._collect_dfs(cursor_node)

                for sentence_id in candidates:
                    previous = best_candidates.get(sentence_id)
                    if previous is None or score > previous:
                        best_candidates[sentence_id] = score

                if not edit_used:
                    penalty = indel_penalty(query_index + 1)
                    for _, next_node, next_edge, next_offset in self._next_characters(
                        node, edge, edge_offset
                    ):
                        stack.append(
                            (
                                next_node,
                                next_edge,
                                next_offset,
                                query_index,
                                True,
                                score - penalty,
                            )
                        )
                continue

            query_character = query[query_index]
            position = query_index + 1
            transitions = self._next_characters(node, edge, edge_offset)

            qwerty_neighbors = get_qwerty_neighbors(query_character) if (prioritize_qwerty or soften_qwerty_penalty) else set()
            if prioritize_qwerty:
                # Sort so QWERTY neighbors are last (will be popped first from LIFO stack)
                transitions.sort(key=lambda t: t[0] in qwerty_neighbors)

            for stored_character, next_node, next_edge, next_offset in transitions:
                if stored_character == query_character:
                    stack.append(
                        (
                            next_node,
                            next_edge,
                            next_offset,
                            query_index + 1,
                            edit_used,
                            score + 2,
                        )
                    )
                elif not edit_used:
                    penalty = (
                        calculate_qwerty_substitution_penalty(position)
                        if soften_qwerty_penalty and stored_character in qwerty_neighbors
                        else substitution_penalty(position)
                    )
                    stack.append(
                        (
                            next_node,
                            next_edge,
                            next_offset,
                            query_index + 1,
                            True,
                            score - penalty,
                        )
                    )

                if not edit_used:
                    stack.append(
                        (
                            next_node,
                            next_edge,
                            next_offset,
                            query_index,
                            True,
                            score - indel_penalty(position),
                        )
                    )

            if not edit_used:
                stack.append(
                    (
                        node,
                        edge,
                        edge_offset,
                        query_index + 1,
                        True,
                        score - indel_penalty(position),
                    )
                )

        return best_candidates


SuffixTrie = CompressedSuffixTrie
