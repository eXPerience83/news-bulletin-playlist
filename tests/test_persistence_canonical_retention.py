from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.models import CanonicalEdition, SourceId
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore


def _edition(native_id: str, published_at: datetime) -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id=native_id,
        title=f"bulletin {native_id}",
        published_at=published_at,
        edition_at=published_at,
    )


def test_canonical_retention_requires_explicit_protection_boundary(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "data" / "state.sqlite3")
    store.initialize()
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    old_protected = _edition("protected", now - timedelta(days=40))
    old_stale = _edition("stale", now - timedelta(days=40))
    recent = _edition("recent", now - timedelta(days=5))
    store.upsert_editions(
        (old_protected, old_stale, recent),
        observed_at=now,
    )
    store.set_match_state(
        old_stale.source_id,
        old_stale.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:stale",
        updated_at=now,
    )

    safe_default = store.prune_operational_history(now=now)
    assert safe_default.canonical_editions_deleted == 0
    assert store.get_edition(old_stale.source_id, old_stale.source_native_id) == old_stale

    result = store.prune_operational_history(
        now=now,
        protected_identities=(old_protected.identity,),
    )

    assert result.canonical_editions_deleted == 1
    assert (
        store.get_edition(old_protected.source_id, old_protected.source_native_id)
        == old_protected
    )
    assert store.get_edition(old_stale.source_id, old_stale.source_native_id) is None
    assert store.get_spotify_episode_uri(old_stale.source_id, old_stale.source_native_id) is None
    assert store.get_edition(recent.source_id, recent.source_native_id) == recent
