"""Run every project test and print one consolidated system-health report."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import trace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DIRECTORY = PROJECT_ROOT / "tests"
TRACE_TEST_DRIVER = TEST_DIRECTORY / "_trace_test_driver.py"
PRODUCTION_FILES = sorted((PROJECT_ROOT / "autocomplete_system").glob("*.py")) + [
    PROJECT_ROOT / filename
    for filename in (
        "autocomplete.py",
        "benchmark.py",
        "build_index.py",
        "main.py",
        "web_app.py",
    )
]
JAVASCRIPT_FILES = [
    PROJECT_ROOT / "web" / "app.js",
    PROJECT_ROOT / "web" / "admin.js",
]

# This return cannot execute: a full cache rejects positions >= 20 immediately
# above it, while a non-full cache has a maximum insertion position of 19.
# It remains visible in the report rather than being silently ignored.
COVERAGE_EXCLUSIONS: dict[str, dict[int, str]] = {
    "autocomplete_system/indexer.py": {
        116: "provably unreachable redundant cache-bound return",
    },
}
IMPORT_ONLY_MODULES = {
    "autocomplete_system/__init__.py": "autocomplete_system",
    "autocomplete_system/constants.py": "autocomplete_system.constants",
}


def _python_syntax_check() -> tuple[bool, str]:
    files = PRODUCTION_FILES + sorted(TEST_DIRECTORY.glob("*.py"))
    try:
        for path in files:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (OSError, SyntaxError) as error:
        return False, str(error)
    return True, f"{len(files)} Python files compiled"


def _javascript_syntax_check() -> tuple[bool | None, str]:
    node = shutil.which("node")
    if node is None:
        return None, "Node.js unavailable; JS syntax checks were skipped"
    for path in JAVASCRIPT_FILES:
        completed = subprocess.run(
            [node, "--check", str(path)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            return False, completed.stderr.strip() or f"Syntax error in {path.name}"
    return True, f"{len(JAVASCRIPT_FILES)} JavaScript files parsed"


def _coverage_report(
    counts_path: Path,
) -> tuple[float, list[tuple[str, int, int, float, int]], list[str]]:
    coverage_data = json.loads(counts_path.read_text(encoding="utf-8"))
    raw_counts = coverage_data["counts"]
    loaded_modules = set(coverage_data["loaded_modules"])
    counts = {
        str(Path(filename).resolve()): set(line_numbers)
        for filename, line_numbers in raw_counts.items()
    }
    rows: list[tuple[str, int, int, float, int]] = []
    exclusion_messages: list[str] = []
    covered_total = 0
    executable_total = 0
    for source in PRODUCTION_FILES:
        label = source.relative_to(PROJECT_ROOT).as_posix()
        executable_lines = {
            line_number
            for line_number in trace._find_executable_linenos(str(source))
            if isinstance(line_number, int) and line_number > 0
        }
        executed_lines = counts.get(str(source.resolve()), set())
        import_only_module = IMPORT_ONLY_MODULES.get(label)
        if import_only_module in loaded_modules:
            executed_lines = executed_lines | executable_lines
        configured_exclusions = COVERAGE_EXCLUSIONS.get(label, {})
        valid_exclusions: set[int] = set()
        source_lines = source.read_text(encoding="utf-8").splitlines()
        for line_number, reason in configured_exclusions.items():
            if line_number not in executable_lines:
                raise RuntimeError(
                    f"Stale coverage exclusion: {label}:{line_number} is not executable"
                )
            valid_exclusions.add(line_number)
            source_text = source_lines[line_number - 1].strip()
            exclusion_messages.append(
                f"{label}:{line_number} ({source_text!r}) - {reason}"
            )

        testable_lines = executable_lines - valid_exclusions
        covered = len(testable_lines & executed_lines)
        missing = len(testable_lines - executed_lines)
        executable = covered + missing
        percentage = 100.0 * covered / executable if executable else 0.0
        rows.append((label, covered, executable, percentage, len(valid_exclusions)))
        covered_total += covered
        executable_total += executable
    overall = 100.0 * covered_total / executable_total if executable_total else 0.0
    return overall, rows, exclusion_messages


def _run_suite() -> tuple[
    subprocess.CompletedProcess[str],
    float,
    list[tuple[str, int, int, float, int]],
    list[str],
]:
    with tempfile.TemporaryDirectory(prefix="pug-test-coverage-") as temporary_directory:
        counts_path = Path(temporary_directory) / "line-counts.json"
        command = [
            sys.executable,
            str(TRACE_TEST_DRIVER),
            str(counts_path),
        ]
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        overall, rows, exclusions = _coverage_report(counts_path)
    return completed, overall, rows, exclusions


def _test_count(output: str) -> int:
    match = re.search(r"Ran (\d+) tests?", output)
    return int(match.group(1)) if match else 0


def _coverage_opinion(percentage: float) -> str:
    if percentage >= 90:
        return "Excellent automated line coverage"
    if percentage >= 80:
        return "Good automated line coverage"
    if percentage >= 70:
        return "Moderate coverage; additional edge cases are recommended"
    return "Low coverage; important behavior may be unverified"


def main() -> int:
    print("=" * 72)
    print("PUG AUTOCOMPLETE - COMPLETE TEST AND SYSTEM HEALTH REPORT")
    print("=" * 72)
    print(f"Python: {sys.version.split()[0]}")
    print(f"Project: {PROJECT_ROOT}")

    python_ok, python_message = _python_syntax_check()
    javascript_ok, javascript_message = _javascript_syntax_check()
    print(f"Python syntax: {'PASS' if python_ok else 'FAIL'} - {python_message}")
    js_status = "SKIP" if javascript_ok is None else ("PASS" if javascript_ok else "FAIL")
    print(f"JavaScript syntax: {js_status} - {javascript_message}")

    completed, coverage, coverage_rows, coverage_exclusions = _run_suite()
    combined_output = completed.stdout + "\n" + completed.stderr
    count = _test_count(combined_output)
    tests_ok = completed.returncode == 0

    print("-" * 72)
    print(f"Automated tests: {'PASS' if tests_ok else 'FAIL'} - {count} tests executed")
    if not tests_ok:
        print("\nFailure details:\n")
        print(combined_output.strip())

    print(
        f"Testable application line coverage: {coverage:.1f}% - "
        f"{_coverage_opinion(coverage)}"
    )
    print("\nCoverage by production module:")
    for label, covered, executable, percentage, excluded in coverage_rows:
        suffix = f", {excluded} excluded" if excluded else ""
        print(f"  {label:<42} {percentage:6.1f}%  ({covered}/{executable}{suffix})")

    if coverage_exclusions:
        print("\nExplicit coverage exclusions:")
        for exclusion in coverage_exclusions:
            print(f"  [!] {exclusion}")

    coverage_complete = all(
        covered == executable
        for _, covered, executable, _, _ in coverage_rows
    )
    required_checks_passed = (
        python_ok
        and tests_ok
        and javascript_ok is not False
        and coverage_complete
    )
    print("-" * 72)
    if required_checks_passed:
        print("SYSTEM VERDICT: PASS - all required automated checks passed.")
    else:
        print("SYSTEM VERDICT: FAIL - at least one required automated check failed.")
    print("=" * 72)
    return 0 if required_checks_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
