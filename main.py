"""Command-line entry point for the online autocomplete phase."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from autocomplete_system.constants import DEFAULT_DATA_DIRECTORY
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.logging_config import configure_system_logging, log_event
from autocomplete_system.models import RankingMode
from translation import (
    AdaptationMode,
    InputAdaptationPipeline,
    SigmaPolicy,
)


READY_PROMPT = "The system is ready. Enter your text:"
SUGGESTIONS_HEADER = "Here are 5 suggestions:"
LOGGER = logging.getLogger("autocomplete.cli")


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
    parser.add_argument(
        "--adaptation-mode",
        type=str,
        choices=[m.value for m in AdaptationMode],
        default=AdaptationMode.OFF.value,
        help="Input adaptation mode: off, translate, or keymap (default: off).",
    )
    parser.add_argument(
        "--sigma-policy",
        type=str,
        choices=[p.value for p in SigmaPolicy],
        default=SigmaPolicy.WARN.value,
        help="Alphabet Sigma policy: off, warn, or block (default: warn).",
    )
    parser.add_argument(
        "--keymap-file",
        type=Path,
        default=None,
        help="Path to custom JSON keyboard layout mapping file.",
    )
    return parser.parse_args()


def run_cli(
    system: AutocompleteSystem,
    pipeline: InputAdaptationPipeline | None = None,
) -> None:
    """Read prefixes until interrupted, recording top choices on '#'."""

    if pipeline is None:
        pipeline = InputAdaptationPipeline(
            mode=AdaptationMode.OFF,
            sigma_policy=SigmaPolicy.WARN,
        )

    current_input = ""
    previous_top_sentence_id: int | None = None
    log_event(
        LOGGER,
        "cli_started",
        ranking_mode=system.ranking_mode.value,
        backend=type(system.index).__name__,
        adaptation_mode=pipeline.mode.value,
        sigma_policy=pipeline.sigma_policy.value,
    )
    print(READY_PROMPT)
    try:
        while True:
            try:
                user_input = input()
            except EOFError:
                break

            # Handle interactive command prefixes
            if user_input.startswith(":"):
                cmd = user_input.strip().lower()
                if cmd == ":translate":
                    new_mode = (
                        AdaptationMode.OFF
                        if pipeline.mode is AdaptationMode.TRANSLATE
                        else AdaptationMode.TRANSLATE
                    )
                    pipeline.set_mode(new_mode)
                    print(f"[Config] Adaptation mode set to: {new_mode.value}")
                elif cmd == ":keymap":
                    new_mode = (
                        AdaptationMode.OFF
                        if pipeline.mode is AdaptationMode.KEYBOARD_REMAP
                        else AdaptationMode.KEYBOARD_REMAP
                    )
                    pipeline.set_mode(new_mode)
                    print(f"[Config] Adaptation mode set to: {new_mode.value}")
                elif cmd == ":sigma":
                    next_policy = {
                        SigmaPolicy.WARN: SigmaPolicy.BLOCK,
                        SigmaPolicy.BLOCK: SigmaPolicy.OFF,
                        SigmaPolicy.OFF: SigmaPolicy.WARN,
                    }[pipeline.sigma_policy]
                    pipeline.set_sigma_policy(next_policy)
                    print(f"[Config] Sigma policy set to: {next_policy.value}")
                elif cmd == ":status":
                    print(
                        f"[Status] Adaptation mode: {pipeline.mode.value} | "
                        f"Sigma policy: {pipeline.sigma_policy.value}"
                    )
                elif cmd in (":help", ":?"):
                    print(
                        "Interactive commands:\n"
                        "  #          Select top suggestion and reset query\n"
                        "  :translate Toggle token-level translation\n"
                        "  :keymap    Toggle Hebrew->QWERTY layout remapping\n"
                        "  :sigma     Cycle Sigma policy (warn -> block -> off)\n"
                        "  :status    Display active adaptation settings\n"
                        "  :help      Show this help message"
                    )
                else:
                    print(f"Unknown command: {user_input}. Type :help for commands.")
                continue

            if user_input == "#":
                selected_sentence_id = previous_top_sentence_id
                if previous_top_sentence_id is not None:
                    system.record_selection(previous_top_sentence_id)
                current_input = ""
                previous_top_sentence_id = None
                log_event(
                    LOGGER,
                    "cli_query_reset",
                    selected_sentence_id=selected_sentence_id,
                )
                print(READY_PROMPT)
                continue

            current_input += user_input

            # Run query through adaptation pipeline (translation / keymap / sigma guard)
            adaptation = pipeline.process(current_input)

            if adaptation.is_blocked:
                print(f"[Blocked] {adaptation.warning_message}")
                print("Query was blocked by Sigma policy.")
                continue

            if adaptation.warning_message:
                print(f"[Warning] {adaptation.warning_message}")
                try:
                    proceed = input("Proceed anyway? (y/n): ").strip().lower()
                except EOFError:
                    break
                if proceed not in ("y", "yes"):
                    current_input = ""
                    previous_top_sentence_id = None
                    print(READY_PROMPT)
                    continue

            if adaptation.was_adapted:
                langs = (
                    f" (detected: {', '.join(adaptation.detected_languages)})"
                    if adaptation.detected_languages
                    else ""
                )
                print(
                    f"[Adapted]: '{adaptation.original_query}' -> '{adaptation.final_query}'{langs}"
                )

            effective_query = adaptation.final_query
            log_event(
                LOGGER,
                "cli_input_received",
                input_fragment=user_input,
                current_query=current_input,
                effective_query=effective_query,
            )
            ranked = system.get_ranked_completions(effective_query)
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
        log_event(LOGGER, "cli_stopped")


def main() -> None:
    configure_system_logging()
    args = parse_args()
    system = AutocompleteSystem.load(args.data_dir, args.mode)
    adaptation_mode = getattr(args, "adaptation_mode", None)
    sigma_policy = getattr(args, "sigma_policy", None)
    keymap_file = getattr(args, "keymap_file", None)

    if (
        (adaptation_mode and adaptation_mode != AdaptationMode.OFF.value)
        or (sigma_policy and sigma_policy != SigmaPolicy.WARN.value)
        or keymap_file
    ):
        pipeline = InputAdaptationPipeline(
            mode=AdaptationMode(adaptation_mode or AdaptationMode.OFF.value),
            sigma_policy=SigmaPolicy(sigma_policy or SigmaPolicy.WARN.value),
        )
        if keymap_file:
            pipeline.load_keymap_file(keymap_file)
        run_cli(system, pipeline)
    else:
        run_cli(system)


if __name__ == "__main__":
    main()
