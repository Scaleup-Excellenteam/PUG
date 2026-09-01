"""Run discovery under ``trace`` and persist absolute-file line counts."""

from __future__ import annotations

import json
import sys
import threading
import trace
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DIRECTORY = PROJECT_ROOT / "tests"
sys.path.insert(0, str(PROJECT_ROOT))

def run_tests() -> unittest.result.TestResult:
    suite = unittest.defaultTestLoader.discover(
        str(TEST_DIRECTORY),
        pattern="test_*.py",
    )
    return unittest.TextTestRunner(verbosity=2).run(suite)


if len(sys.argv) != 2:
    raise SystemExit("usage: _trace_test_driver.py COUNTS_JSON")

tracer = trace.Trace(count=True, trace=False, ignoredirs=[sys.base_prefix])
threading.settrace(tracer.globaltrace)
try:
    result = tracer.runfunc(run_tests)
finally:
    threading.settrace(None)
counts_by_file: dict[str, set[int]] = {}
for (filename, line_number), execution_count in tracer.results().counts.items():
    if execution_count:
        resolved = str(Path(filename).resolve())
        counts_by_file.setdefault(resolved, set()).add(line_number)

serialized = {
    "counts": {
        filename: sorted(line_numbers)
        for filename, line_numbers in counts_by_file.items()
    },
    "loaded_modules": sorted(
        module_name
        for module_name in ("autocomplete_system", "autocomplete_system.constants")
        if module_name in sys.modules
    ),
}
Path(sys.argv[1]).write_text(json.dumps(serialized), encoding="utf-8")
raise SystemExit(0 if result.wasSuccessful() else 1)
