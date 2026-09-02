"""Project-wide constants."""

from pathlib import Path

MAX_NODE_CACHE_SIZE = 20
ALPHA = 5
INDEX_VERSION = 2
DEFAULT_INPUT_SOURCES = (Path("Archive"),)
DEFAULT_DATA_DIRECTORY = Path("data")
INDEX_FILENAME = "index.pkl"
MASTER_ARRAY_FILENAME = "sentences.pkl"
USAGE_STATS_FILENAME = "usage_stats.json"
RANKING_SETTINGS_FILENAME = "ranking_settings.json"
ANALYTICS_EVENTS_FILENAME = "analytics_events.jsonl"
SQLITE_INDEX_FILENAME = "sentences.sqlite3"
SQLITE_VARIANT_BATCH_SIZE = 100
SQLITE_INSERT_BATCH_SIZE = 10_000
SQLITE_BUILD_CACHE_MIB = 256
