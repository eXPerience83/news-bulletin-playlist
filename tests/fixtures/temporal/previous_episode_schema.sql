-- Episode schemas from 32b924c1 (database format 2).
CREATE TABLE IF NOT EXISTS spotify_first_episode_state (
    source_id TEXT NOT NULL,
    spotify_show_id TEXT NOT NULL,
    spotify_episode_id TEXT NOT NULL,
    spotify_episode_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    edition_at TEXT,
    timestamp_source TEXT NOT NULL
        CHECK (timestamp_source IN ('title', 'first_seen')),
    release_date_raw TEXT NOT NULL,
    release_date_precision TEXT NOT NULL
        CHECK (release_date_precision IN ('day', 'month', 'year')),
    duration_seconds INTEGER
        CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_id, spotify_episode_id)
);
CREATE TABLE spotify_first_episode_history (
    source_id TEXT NOT NULL,
    spotify_show_id TEXT NOT NULL,
    spotify_episode_id TEXT NOT NULL,
    spotify_episode_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    edition_at TEXT,
    timestamp_source TEXT NOT NULL CHECK (timestamp_source IN ('title', 'first_seen')),
    release_date_raw TEXT NOT NULL,
    release_date_precision TEXT NOT NULL
        CHECK (release_date_precision IN ('day', 'month', 'year')),
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_id, spotify_episode_id)
);
