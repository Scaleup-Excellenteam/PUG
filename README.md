# Text Autocomplete System

Python 3.10+ autocomplete system with normalization, substring matching from
every character position, one substitution/insertion/deletion, top-20 candidate
caches, exact assignment scoring, optional popularity weighting, persistence,
and both CLI and local web interfaces. The implementation uses only the
standard library.

## Architecture

- `SentenceRecord` master array: original and normalized text, relative source
  path, 1-based source line, and independent `usage_count` for every input line.
- Literal backend: a path-compressed all-character suffix Trie whose nodes use
  `__slots__` and cache only sentence IDs. It performs DFS with an edit budget
  of at most one and is intended for small/medium corpora and structural tests.
- Scalable backend: a disk-backed SQLite FTS5 trigram substring index plus
  explicit top-20 caches for one- and two-character nodes. It implements the
  same normalization, edit variants, cache limits, scores, and result API and
  is the practical backend for the supplied multi-million-line archive.

The literal structure is available because it directly represents every
character suffix. The scalable backend is the default because materializing
every suffix and every node cache for the supplied archive would require far
more memory than a normal workstation provides.

## Build from the supplied archive

No extraction is needed. The default source is `Archive/Archive.zip`:

```powershell
python build_index.py
```

This creates `data/index.pkl`, `data/sentences.pkl`,
`data/sentences.sqlite3`, and `data/usage_stats.json`.

Other accepted sources are recursive directories, individual `.txt` files,
and ZIP archives. Repeat `--source` to combine them:

```powershell
python build_index.py --source corpus1 --source corpus2.zip --data-dir data
```

To build the literal compressed Trie for a smaller corpus:

```powershell
python build_index.py --backend trie --source small_corpus
```

Every text file is decoded strictly as UTF-8, blank lines are ignored, and ZIP
entry or directory-relative paths are retained in results.

## Run the CLI

Assignment-compatible ranking is the default:

```powershell
python main.py
```

Popularity-weighted ranking uses `final_score = text_score + 5 * usage_count`:

```powershell
python main.py --mode popularity
```

Each line entered is appended to the current query. Enter `#` to select the
previous number-one result and reset the query. `Ctrl+C` and EOF save usage
statistics and close the database cleanly.

## Run the local website

Start the Google-like English interface after building the index:

```powershell
python web_app.py
```

Then open `http://localhost:8000`. Suggestions appear while typing. Click a
suggestion, or select it with the arrow keys and Enter, to increment its
`usage_count`, show the selected sentence, clear the search box, and begin a
new search. This is the website equivalent of entering `#` in the CLI, except
that the website can select any of the five suggestions instead of only the
first one.

The website defaults to popularity ranking so selections influence future
results. To preserve the assignment's official text-only score and ordering:

```powershell
python web_app.py --mode assignment
```

The server binds to `127.0.0.1` by default, so it is accessible only from the
local computer. Stop it with `Ctrl+C`; usage statistics are saved both after
each selection and during graceful shutdown.

The microphone button uses the browser's Web Speech API for English voice
input. It requires a supported browser (normally Chrome or Edge), microphone
permission, and may require an internet connection depending on the browser's
recognition service. The feature is detected at runtime and is disabled with a
clear tooltip when the browser does not provide speech recognition. Recognized
text is placed in the same search field and goes through the unchanged local
normalization, scoring, and autocomplete engine.

## Local administration dashboard

Open `http://localhost:8000/admin` while the website is running. Because the
server binds to the local loopback interface, the dashboard currently has no
password. It provides:

- complete corpus, source-file, Master Array, index, runtime, and storage data;
- an append-only `data/analytics_events.jsonl` audit trail containing every web
  search, result IDs and scores, input method, latency, selection, local client
  metadata, error, server lifecycle event, and administrative action;
- search totals, typed/voice split, no-result rate, unique and top queries,
  P50/P95/maximum latency, top selections, hourly activity, and recent events;
- paginated access to every Master Array record;
- complete JSON and CSV analytics exports;
- confirmed actions for resetting analytics or popularity data; and
- a confirmed, background replacement-index build from `Archive/Archive.zip`.

Replacement builds are created under `rebuilds/` and never overwrite or
activate the live index automatically. Reset buttons require their displayed
confirmation phrase exactly to guard against accidental data loss.

## Real-time operational logs

`logs/system.jsonl` is the separate operational log for the complete system.
It uses UTF-8 JSON Lines and is flushed after every event. Records include UTC
time, severity, component, process/thread information, event name, and complete
event details. Search records deliberately include the full original and
normalized query plus the complete returned sentences, sources, offsets,
scores, IDs, timing, ranking mode, and backend.

The log covers system/index loading, searches, selections, popularity, usage
storage, HTTP requests, client and voice events, Admin operations, CLI input,
source-file processing, Trie/SQLite builds and progress, benchmarks, shutdown,
and uncaught main-thread or worker-thread failures.

Rotation occurs automatically when the active file reaches 10 MiB. Five
backups are retained as `system.jsonl.1` through `system.jsonl.5`; the oldest is
removed by the standard-library rotating handler. This bounds operational-log
storage to approximately 60 MiB including the active file. These logs contain
full user text and should therefore remain local and must not be committed.

`data/analytics_events.jsonl` remains separate: it is the durable user-activity
and dashboard audit trail, while `logs/system.jsonl` is intended for debugging,
operations, errors, and end-to-end tracing.

The Admin dashboard includes a live operational Log Viewer. It polls without
holding an HTTP connection open, can pause/resume automatic refresh, select the
active file or any retained rotation backup, show 50/200/500 recent records,
filter by severity and component, search through the complete JSON payload,
expand an event's formatted JSON, and download the selected file. The server
accepts only the configured `system.jsonl` names and rejects arbitrary paths.

## Python API

```python
from autocomplete import get_best_k_completions, initialize
from autocomplete_system.models import RankingMode

initialize(ranking_mode=RankingMode.POPULARITY)
results = get_best_k_completions("example substring")
```

The function returns up to five `AutoCompleteData` instances containing the
original sentence, source path, 1-based line offset, and integer score.

## Tests

```powershell
python -m unittest discover -v
```

The suite covers all scoring examples from the assignment, punctuation and
case normalization, all-character substring starts, duplicate lines, cache
limits/orders, popularity, cumulative CLI input, ZIP sources, persistence, and
differential behavior between the Trie and SQLite backends.

Measured build/search results and asymptotic analysis are documented in
[`COMPLEXITY.md`](COMPLEXITY.md). Run `python benchmark.py` to repeat the
latency measurements on the current machine.

Pickle files must only be loaded from a trusted source.
