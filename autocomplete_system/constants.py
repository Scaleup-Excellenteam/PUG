"""Project-wide constants."""

from pathlib import Path

MAX_NODE_CACHE_SIZE = 20
ALPHA = 5
INDEX_VERSION = 2
DEFAULT_INPUT_SOURCES = (Path("Archive/Archive.zip"),)
DEFAULT_DATA_DIRECTORY = Path("data")
INDEX_FILENAME = "index.pkl"
MASTER_ARRAY_FILENAME = "sentences.pkl"
USAGE_STATS_FILENAME = "usage_stats.json"
RANKING_SETTINGS_FILENAME = "ranking_settings.json"
ANALYTICS_EVENTS_FILENAME = "analytics_events.jsonl"
SQLITE_INDEX_FILENAME = "sentences.sqlite3"
SQLITE_VARIANT_BATCH_SIZE = 100
ACTIVE_SNAPSHOT_POINTER_FILENAME = "active_snapshot.json"
DEFAULT_SNAPSHOT_POLL_INTERVAL_SECONDS = 2.0
