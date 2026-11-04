"""Deterministic three-table source refresh snapshots for transaction assertions."""

from __future__ import annotations

import sqlite3

TABLES = (
    "spotify_first_episode_state",
    "spotify_first_episode_history",
    "spotify_first_source_state",
)


def rows(path):
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2,3').fetchall()
            for table in TABLES
        }
