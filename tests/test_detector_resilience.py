from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_bulletin_playlist.engine import EngineRunner
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    EngineConfig,
    ExternalReference,
    LanguageTag,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.spotify.first_models import RefreshReason, SpotifyFirstSourceState
from news_bulletin_playlist.spotify.public_detector import (
    DETECTOR_SCHEMA_VERSION,
    PublicDetectorCycleSummary,
    PublicDetectorState,
)

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
SOURCE_ID = SourceId("source")
SHOW_ID = "S" * 22
EPISODE_ID = "E" * 22


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SOURCE_ID,
        display_name="Source",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        external_references=(ExternalReference("spotify", "show", SHOW_ID),),
    )


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("playlist"),
        display_name="Playlist",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SOURCE_ID,)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id="playlist-destination",
        ),
    )


def _local_state() -> SpotifyFirstSourceState:
    return SpotifyFirstSourceState(
        source_id=SOURCE_ID,
        spotify_show_id=SHOW_ID,
        last_refresh_at=NOW,
        reason=RefreshReason.BOOTSTRAP,
        detector_episode_id=EPISODE_ID,
        api_episode_id=EPISODE_ID,
        request_count=1,
        catalogue_complete=True,
        coverage_proven=True,
        coverage_cutoff_at=NOW - timedelta(hours=48),
    )


def _public_state(*, failures: int) -> PublicDetectorState:
    return PublicDetectorState(
        source_id=SOURCE_ID,
        spotify_show_id=SHOW_ID,
        last_episode_id=EPISODE_ID,
        last_episode_title="Same title",
        last_public_release_date_raw="2026-09-20T10:00:00Z",
        last_public_observed_at=NOW,
        last_detector_success_at=NOW,
        last_change_at=NOW,
        last_failure_at=None if failures == 0 else NOW + timedelta(minutes=10),
        last_catalogue_refresh_at=NOW,
        consecutive_detector_failures=failures,
        detector_schema_version=DETECTOR_SCHEMA_VERSION,
        last_oembed_title="Same title",
        last_public_release_date_shape=None,
        last_degraded_audit_at=None,
    )


class _FirstJournal:
    def get_source_state(self, source_id: SourceId) -> SpotifyFirstSourceState | None:
        assert source_id == SOURCE_ID
        return _local_state()


class _DetectorJournal:
    def __init__(self, state: PublicDetectorState) -> None:
        self.state = state

    def get(self, source_id: SourceId, show_id: str) -> PublicDetectorState | None:
        assert source_id == SOURCE_ID
        assert show_id == SHOW_ID
        return self.state


class _PublicDetector:
    def __init__(self, *, marked: bool = True) -> None:
        self.marked = marked
        self.audit_calls: list[tuple[SourceId, str, datetime]] = []

    def mark_degraded_audit_attempt(
        self,
        source_id: SourceId,
        show_id: str,
        *,
        attempted_at: datetime,
    ) -> bool:
        self.audit_calls.append((source_id, show_id, attempted_at))
        return self.marked


def _runner(
    *,
    public_state: PublicDetectorState,
    public_detector: _PublicDetector,
) -> EngineRunner:
    runner = object.__new__(EngineRunner)
    runner.config = EngineConfig(
        sources=(_source(),),
        playlists=(_playlist(),),
    )
    runner.first_journal = _FirstJournal()
    runner.detector_journal = _DetectorJournal(public_state)
    runner.public_detector = public_detector
    runner.source_inactivity = timedelta(hours=24)
    runner.degraded_source_audit = timedelta(minutes=30)
    return runner


def _summary(*, degraded_change: bool = False) -> PublicDetectorCycleSummary:
    return PublicDetectorCycleSummary(
        observed_count=0,
        changed_count=0,
        unchanged_count=0,
        failure_count=1,
        degraded_change_source_ids=(SOURCE_ID,) if degraded_change else (),
    )


def test_degraded_title_change_refreshes_only_source_immediately() -> None:
    detector = _PublicDetector()
    runner = _runner(
        public_state=_public_state(failures=1),
        public_detector=detector,
    )

    plan = runner._refresh_plan(  # noqa: SLF001
        _summary(degraded_change=True),
        now=NOW + timedelta(minutes=10),
    )

    assert plan == ((SOURCE_ID, RefreshReason.CHANGE),)
    assert detector.audit_calls == []


def test_persistent_primary_failure_waits_for_degraded_audit_deadline() -> None:
    detector = _PublicDetector()
    runner = _runner(
        public_state=_public_state(failures=2),
        public_detector=detector,
    )

    assert (
        runner._refresh_plan(  # noqa: SLF001
            _summary(),
            now=NOW + timedelta(minutes=20),
        )
        == ()
    )
    assert detector.audit_calls == []

    assert runner._refresh_plan(  # noqa: SLF001
        _summary(),
        now=NOW + timedelta(minutes=30),
    ) == ((SOURCE_ID, RefreshReason.INACTIVITY_AUDIT),)
    assert detector.audit_calls == [(SOURCE_ID, SHOW_ID, NOW + timedelta(minutes=30))]


def test_degraded_audit_is_fail_closed_when_attempt_cannot_be_persisted() -> None:
    detector = _PublicDetector(marked=False)
    runner = _runner(
        public_state=_public_state(failures=2),
        public_detector=detector,
    )

    plan = runner._refresh_plan(  # noqa: SLF001
        _summary(),
        now=NOW + timedelta(minutes=30),
    )

    assert plan == ()
    assert detector.audit_calls == [(SOURCE_ID, SHOW_ID, NOW + timedelta(minutes=30))]
