# Text Autocomplete System

Python 3.10+ autocomplete system with normalization, substring matching from
every character position, one substitution/insertion/deletion, top-20 candidate
caches, exact assignment scoring, optional popularity weighting, persistence,
and a cumulative-input CLI. The implementation uses only the standard library.

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
