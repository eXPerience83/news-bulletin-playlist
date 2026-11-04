"""Offline public-release evidence and catalogue fixtures shared across suites."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.models import TemporalStrategy
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.public_detector import PublicReleaseDateShape as Shape
from news_bulletin_playlist.spotify.public_detector import spotify_show_id
from news_bulletin_playlist.spotify.public_release import PublicReleaseEvidence

NOW = datetime(2026, 9, 28, 5, 37, 47, tzinfo=UTC)
EPISODE = "6kVcmyoWqymilhqVzMOJ0E"
OLD = "7KDb9bGN2MIdmkFaiSOnAc"
SOURCE = replace(
    BUILTIN_CATALOG.sources[0],
    temporal_strategy=TemporalStrategy.AUTO,
    timezone=ZoneInfo("Europe/Madrid"),
)


def evidence(raw="2026-09-28T05:21:00Z", *, episode=EPISODE, observed=NOW):
    return PublicReleaseEvidence(
        SOURCE.id, spotify_show_id(SOURCE), episode, raw, Shape.OBJECT_ISO_STRING, observed
    )


def fetch(
    public=None,
    *,
    title="Iberdrola, Riquelme, el BCE, Trump y Anthropic, Nvidia y guerra híbrida",
    source=SOURCE,
    day="2026-09-28",
    precision="day",
    episode=EPISODE,
    extra=(),
):
    items = [
        dict(
            id=episode,
            uri=f"spotify:episode:{episode}",
            name=title,
            release_date=day,
            release_date_precision=precision,
            duration_ms=100000,
        )
    ]
    items.extend(extra)

    class Client:
        calls = 0

        def show_episodes(self, show_id, *, limit=50, offset=0):
            self.calls += 1
            return catalogue_page(items)

    client = Client()
    snapshot = fetch_source_catalogue(
        client,
        source,
        now=NOW,
        cutoff=NOW - timedelta(hours=48),
        public_release_evidence=public,
        collect_evidence=True,
    )
    assert client.calls == snapshot.request_count == 1
    assert len(snapshot.episodes) == len(items)
    return snapshot
