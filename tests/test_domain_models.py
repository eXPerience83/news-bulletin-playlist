from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from news_bulletin_playlist.models import CanonicalEdition, SourceId


def test_canonical_identity_is_source_and_native_id() -> None:
    edition = CanonicalEdition(
        source_id=SourceId("rne"),
        source_native_id="asset-123",
        title="NOTICIAS RNE - 25.08.2026 - 18.30 H",
        published_at=datetime(2026, 8, 25, 16, 31, tzinfo=UTC),
        edition_at=datetime(2026, 8, 25, 18, 30, tzinfo=timezone(timedelta(hours=2))),
    )

    assert edition.identity == (SourceId("rne"), "asset-123")
    assert edition.edition_at == datetime(2026, 8, 25, 16, 30, tzinfo=UTC)
    with pytest.raises(FrozenInstanceError):
        edition.title = "changed"  # type: ignore[misc]


def test_rne_like_assets_for_same_apparent_edition_can_coexist() -> None:
    common = {
        "source_id": SourceId("rne"),
        "title": "NOTICIAS RNE - 25.08.2026 - 18.30 H",
        "published_at": datetime(2026, 8, 25, 16, 31, tzinfo=UTC),
        "edition_at": datetime(2026, 8, 25, 16, 30, tzinfo=UTC),
    }
    first = CanonicalEdition(source_native_id="asset-a", **common)
    second = CanonicalEdition(source_native_id="asset-b", **common)

    assert first.identity != second.identity
    assert len({first.identity, second.identity}) == 2


@pytest.mark.parametrize("field", ["published_at", "edition_at"])
def test_canonical_edition_rejects_naive_datetimes(field: str) -> None:
    values = {
        "source_id": SourceId("ser"),
        "source_native_id": "guid",
        "title": "Bulletin",
        "published_at": datetime(2026, 8, 25, tzinfo=UTC),
        "edition_at": datetime(2026, 8, 25, tzinfo=UTC),
    }
    values[field] = datetime(2026, 8, 25)

    with pytest.raises(ValueError, match=f"{field} must be timezone-aware"):
        CanonicalEdition(**values)  # type: ignore[arg-type]


def test_duration_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="duration_seconds"):
        CanonicalEdition(
            source_id=SourceId("cnn"),
            source_native_id="episode",
            title="CNN 5 cosas",
            published_at=datetime(2026, 8, 25, tzinfo=UTC),
            edition_at=None,
            duration_seconds=-1,
        )
