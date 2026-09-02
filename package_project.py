"""Create small, verified upload bundles without transient project files."""

from __future__ import annotations

import argparse
import os
import time
import zipfile
from pathlib import Path


ALWAYS_EXCLUDED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "logs",
    "rebuilds",
}
ALWAYS_EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".tmp", ".log"}
GENERATED_DATA_PREFIXES = ("data_compact", "data_legacy", ".data_compact")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a clean project upload ZIP.")
    parser.add_argument(
        "--profile",
        choices=("source", "ready"),
        default="source",
        help=(
            "source excludes generated data and rebuilds it from Archive; "
            "ready also includes the runnable data directory"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing bundle at the exact output path",
    )
    return parser.parse_args()


def _iter_files(root: Path, profile: str, output: Path):
    excluded_directories = set(ALWAYS_EXCLUDED_DIRECTORIES)
    if profile == "source":
        excluded_directories.add("data")
    for current, directory_names, file_names in os.walk(root):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in excluded_directories
            and not name.startswith(GENERATED_DATA_PREFIXES)
        )
        for name in sorted(file_names):
            path = current_path / name
            if path.resolve() == output:
                continue
            if path.suffix.lower() in ALWAYS_EXCLUDED_SUFFIXES:
                continue
            if path.is_symlink():
                continue
            yield path


def build_bundle(root: Path, output: Path, profile: str, *, force: bool = False) -> Path:
    root = root.resolve()
    output = output.resolve()
    if output.exists() and not force:
        raise FileExistsError(
            f"Output already exists: {output}. Pass --force to replace it."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    started = time.perf_counter()
    file_count = 0
    input_bytes = 0
    archive_root = root.name

    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for path in _iter_files(root, profile, output):
                relative = path.relative_to(root)
                archive_name = (Path(archive_root) / relative).as_posix()
                # Nested source ZIPs are already compressed. Storing them saves
                # substantial packaging time with no meaningful size penalty.
                compression = (
                    zipfile.ZIP_STORED
                    if path.suffix.lower() == ".zip"
                    else zipfile.ZIP_DEFLATED
                )
                archive.write(path, archive_name, compress_type=compression)
                file_count += 1
                input_bytes += path.stat().st_size
                if file_count % 250 == 0:
                    print(f"Packed {file_count:,} files...", flush=True)
        with zipfile.ZipFile(temporary) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                raise RuntimeError(f"ZIP verification failed at {corrupt}")
        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    output_bytes = output.stat().st_size
    elapsed = time.perf_counter() - started
    print(
        f"Created verified {profile} bundle: {output}\n"
        f"Files: {file_count:,}\n"
        f"Input: {input_bytes / (1024**3):.2f} GiB\n"
        f"ZIP: {output_bytes / (1024**3):.2f} GiB "
        f"({output_bytes / (1024**2):.1f} MiB)\n"
        f"Duration: {elapsed:.1f}s",
        flush=True,
    )
    return output


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parent
    output = args.output or root / "dist" / f"PUG-{args.profile}.zip"
    if not output.is_absolute():
        output = root / output
    build_bundle(root, output, args.profile, force=args.force)


if __name__ == "__main__":
    main()
