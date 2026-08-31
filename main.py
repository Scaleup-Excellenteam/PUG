"""Command-line entry point for the online autocomplete phase."""

from __future__ import annotations

import argparse
from pathlib import Path

from autocomplete_system.constants import DEFAULT_DATA_DIRECTORY
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.models import RankingMode


READY_PROMPT = "The system is ready. Enter your text:"
SUGGESTIONS_HEADER = "Here are 5 suggestions:"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the autocomplete CLI.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIRECTORY,
        help="Directory containing the serialized index (default: data).",
    )
    parser.add_argument(
        "--mode",
        type=RankingMode,
        choices=list(RankingMode),
        default=RankingMode.ASSIGNMENT,
        help=(
            "Ranking mode: assignment uses text score/alphabetical order; "
            "popularity adds Alpha * usage_count."
        ),
    )
    return parser.parse_args()


def run_cli(system: AutocompleteSystem) -> None:
    """Read prefixes until interrupted, recording top choices on '#'."""

    current_input = ""
    previous_top_sentence_id: int | None = None
    print(READY_PROMPT)
    try:
        while True:
            try:
                user_input = input()
            except EOFError:
                break

            if user_input == "#":
                if previous_top_sentence_id is not None:
                    system.record_selection(previous_top_sentence_id)
                current_input = ""
                previous_top_sentence_id = None
                print(READY_PROMPT)
                continue

            current_input += user_input
            ranked = system.get_ranked_completions(current_input)
            previous_top_sentence_id = ranked[0][0] if ranked else None
            if not ranked:
                continue

            print(SUGGESTIONS_HEADER)
            for rank, (_, completion) in enumerate(ranked, start=1):
                print(
                    f"{rank}. {completion.completed_sentence} "
                    f"({completion.source_text}:{completion.offset}, "
                    f"score={completion.score})"
                )
    except KeyboardInterrupt:
        pass
    finally:
        if system.data_directory is not None:
            system.save_usage_stats()
        system.close()


def main() -> None:
    args = parse_args()
    run_cli(AutocompleteSystem.load(args.data_dir, args.mode))


if __name__ == "__main__":
    main()
