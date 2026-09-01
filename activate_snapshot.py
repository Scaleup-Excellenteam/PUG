"""Command-line entry point that atomically publishes one built snapshot.

This is the ZDT ("Zero DownTime") counterpart to ``build_index.py``: once a
replacement snapshot directory has been built (by ``build_index.py`` itself,
or by the Admin dashboard's "Start safe rebuild" action), this script
validates it and atomically flips the shared pointer file to name it as
active. It never talks to a running ``web_app.py`` server directly -- any
server whose ``SnapshotWatcher`` is polling the same ``--pointer`` file
(the default is the same one that server uses) adopts the change live,
without a restart, the next time it polls. Because the hand-off is purely a
shared filesystem location, this can run on the same host as the server, or
on any other host/process with access to that location (for example a
shared or remote-mounted volume), which is what lets a new data source be
added "live, remotely, with zero downtime."
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from autocomplete_system.constants import ACTIVE_SNAPSHOT_POINTER_FILENAME
from autocomplete_system.logging_config import configure_system_logging, log_event
from autocomplete_system.snapshot import write_snapshot_pointer


LOGGER = logging.getLogger("autocomplete.activate_cli")
DEFAULT_POINTER_PATH = (
    Path(__file__).resolve().parent / "rebuilds" / ACTIVE_SNAPSHOT_POINTER_FILENAME
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically publish one already-built snapshot directory as the "
            "active one. Any web_app.py server polling the same pointer file "
            "adopts it live, with no restart and no downtime."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="A snapshot directory previously built by build_index.py.",
    )
    parser.add_argument(
        "--pointer",
        type=Path,
        default=DEFAULT_POINTER_PATH,
        help=f"Pointer file to update (default: {DEFAULT_POINTER_PATH}).",
    )
    return parser.parse_args()


def main() -> None:
    configure_system_logging()
    args = parse_args()
    record = write_snapshot_pointer(args.pointer, args.data_dir)
    print(
        f"Activated {record['data_directory']} "
        f"({record['sentence_count']:,} sentences) via {args.pointer}."
    )
    log_event(
        LOGGER,
        "snapshot_activation_cli_completed",
        data_directory=record["data_directory"],
        pointer_path=str(args.pointer),
        sentence_count=record["sentence_count"],
    )


if __name__ == "__main__":
    main()
