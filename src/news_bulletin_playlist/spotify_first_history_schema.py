"""Schema shared by the format-2 migration and Spotify-first journal."""

EPISODE_HISTORY_TABLE = """
CREATE TABLE spotify_first_episode_history (
    source_id TEXT NOT NULL,
    spotify_show_id TEXT NOT NULL,
    spotify_episode_id TEXT NOT NULL,
    spotify_episode_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    edition_at TEXT,
    timestamp_source TEXT NOT NULL
        CHECK (timestamp_source IN ('title', 'spotify_public_release', 'first_seen')),
    release_date_raw TEXT NOT NULL,
    release_date_precision TEXT NOT NULL
        CHECK (release_date_precision IN ('day', 'month', 'year')),
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_id, spotify_episode_id)
)
"""

EPISODE_HISTORY_INDEX = """
CREATE INDEX idx_spotify_first_history_source_seen
ON spotify_first_episode_history (source_id, last_seen_at DESC, spotify_episode_id)
"""

EPISODE_HISTORY_COLUMNS = (
    "source_id",
    "spotify_show_id",
    "spotify_episode_id",
    "spotify_episode_uri",
    "title",
    "edition_at",
    "timestamp_source",
    "release_date_raw",
    "release_date_precision",
    "duration_seconds",
    "first_seen_at",
    "last_seen_at",
)
