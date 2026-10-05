"""Source-local public policy, DST resolution and forward-only durable chronology."""

import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_managed_sources import _source, _state
from test_public_release_fallback import EPISODE, NOW, OLD, SOURCE, evidence, fetch
from test_source_admin_http import (
    _configured_source,
    _create_form,
    _csrf,
    _Handler,
    _inspect,
    _login,
    _request,
    _server,
    _update_form,
)

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import (
    EngineRunner,
    SourceCycleOutcome,
    TemporalRefreshMetrics,
)
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.engine_runtime import _playlist_write_contract
from news_bulletin_playlist.managed_state import (
    ManagedSourceOverride,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    compile_engine_config,
    effective_sources,
    parse_managed_state,
    serialize_managed_state,
)
from news_bulletin_playlist.models import PublicReleaseMode as Mode
from news_bulletin_playlist.models import SourceId, TemporalStrategy
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.source_admin import SourceAuditBatch, SourceAuditBatchItem
from news_bulletin_playlist.source_audit_export import build_source_audit_bundle
from news_bulletin_playlist.source_inspection import (
    inspect_configured_source,
    serialize_source_inspection,
)
from news_bulletin_playlist.spotify.first_models import RefreshReason, TimestampSource
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorCycleSummary,
    PublicDetectorJournal,
    PublicEpisodeObservation,
    PublicReleaseDateShape,
    spotify_show_id,
)
from news_bulletin_playlist.spotify.public_release import PublicReleaseOutcome as Outcome
from news_bulletin_playlist.spotify.public_release import validate_public_release


@pytest.mark.parametrize(
    "source_id,raw,expected,semantic,delay_minutes",
    [
        ("rne", "2026-09-30T00:13:00Z", "2026-09-29T22:13:00Z", "2026-09-29T22:00:00Z", 13),
        ("ser", "2026-09-30T08:00:00Z", "2026-09-30T08:00:00Z", "2026-09-30T08:00:00Z", 0),
        (
            "ondacero",
            "2026-09-30T08:00:00Z",
            "2026-09-30T08:00:00Z",
            "2026-09-30T08:00:00Z",
            0,
        ),
        (
            "rmf_fakty",
            "2026-09-30T08:00:00Z",
            "2026-09-30T08:00:00Z",
            "2026-09-30T08:00:00Z",
            0,
        ),
    ],
)
def test_september_30_audit_semantic_controls(source_id, raw, expected, semantic, delay_minutes):
    source = next(s for s in BUILTIN_CATALOG.sources if s.id == source_id)
    # Constructed audit-time observation, deliberately not a first runtime sighting.
    public = replace(
        evidence(raw, observed=datetime(2026, 9, 30, 12, tzinfo=UTC)),
        source_id=source.id,
        spotify_show_id=spotify_show_id(source),
        spotify_episode_id="7y7VNxFD6xIXRSK9Jxjxnn" if source_id == "rne" else EPISODE,
    )
    arguments = dict(
        source_id=source.id,
        show_id=spotify_show_id(source),
        episode_id=public.spotify_episode_id,
        release_date="2026-09-30",
        precision="day",
        timezone=source.timezone,
    )
    instant, outcome = validate_public_release(
        public, **arguments, semantic_exact=False, mode=source.public_release_mode
    )
    assert outcome is Outcome.ACCEPTED
    assert instant == datetime.fromisoformat(expected)
    assert instant - datetime.fromisoformat(semantic) == timedelta(minutes=delay_minutes)
    for mode in Mode:
        assert validate_public_release(public, **arguments, semantic_exact=True, mode=mode) == (
            None,
            Outcome.SHADOWED,
        )
    if source_id == "rne":
        literal, outcome = validate_public_release(
            public, **arguments, semantic_exact=False, mode=Mode.INSTANT
        )
        assert outcome is Outcome.ACCEPTED  # A later audit cannot prove first-sighting FUTURE.
        assert literal - datetime.fromisoformat(semantic) == timedelta(minutes=133)


def test_september_30_expansion_audit_keeps_instant_precision():
    snapshot = fetch(
        evidence("2026-09-30T05:22:00Z", observed=datetime(2026, 9, 30, 12, tzinfo=UTC)),
        day="2026-09-30",
    )
    assert snapshot.public_release_mode is Mode.INSTANT
    assert snapshot.evidence[0].public_release_outcome is Outcome.ACCEPTED
    assert snapshot.episodes[0].edition_at == datetime(2026, 9, 30, 5, 22, tzinfo=UTC)


@pytest.mark.parametrize(
    "mode,outcome,hour",
    [
        (Mode.INSTANT, Outcome.FUTURE, None),
        (Mode.WALL_TIME, Outcome.ACCEPTED, 19),
        (Mode.DISABLED, Outcome.DISABLED, None),
    ],
)
def test_real_rne_case(mode, outcome, hour):
    source = replace(SOURCE, public_release_mode=mode)
    result = fetch(
        evidence("2026-09-26T21:13:00Z", observed=datetime(2026, 9, 26, 19, 31, tzinfo=UTC)),
        source=source,
        day="2026-09-26",
    )
    assert result.public_release_mode is mode
    assert result.evidence[0].public_release_outcome is outcome
    item = result.episodes[0]
    assert item.edition_at == (
        None if hour is None else datetime(2026, 9, 26, hour, 13, tzinfo=UTC)
    )
    assert item.timestamp_source is (
        TimestampSource.FIRST_SEEN if hour is None else TimestampSource.SPOTIFY_PUBLIC_RELEASE
    )


@pytest.mark.parametrize("mode", list(Mode))
def test_title_precedence_for_all_modes(mode):
    source = replace(
        SOURCE, public_release_mode=mode, temporal_strategy=TemporalStrategy.FULL_DATETIME
    )
    result = fetch(evidence(), source=source, title="Las noticias de la SER, 07:00 (28/09/2026)")
    assert result.episodes[0].timestamp_source is TimestampSource.TITLE
    assert result.episodes[0].edition_at == datetime(2026, 9, 28, 5, tzinfo=UTC)
    assert result.evidence[0].public_release_outcome is Outcome.SHADOWED


@pytest.mark.parametrize(
    "zone", ["Europe/Madrid", "Europe/Berlin", "Europe/Warsaw", "America/New_York"]
)
@pytest.mark.parametrize("case", ["gap", "fold", "unique_summer", "unique_winter"])
def test_wall_dst_and_precision(zone, case):
    american = zone == "America/New_York"
    values = {
        "gap": (
            "2026-03-08T02:30:17.123456+05:30" if american else "2026-03-29T02:30:17.123456-04:00",
            None,
        ),
        "fold": (
            "2026-11-01T01:30:17.123456Z" if american else "2026-10-25T02:30:17.123456Z",
            None,
        ),
        "unique_summer": (
            "2026-09-26T21:13:17.123456+05:30",
            "2026-09-27T01:13:17.123456Z" if american else "2026-09-26T19:13:17.123456Z",
        ),
        "unique_winter": (
            "2026-01-26T21:13:17.123456-04:00",
            "2026-01-27T02:13:17.123456Z" if american else "2026-01-26T20:13:17.123456Z",
        ),
    }
    raw, expected = values[case]
    result = fetch(
        evidence(raw, observed=datetime(2026, 12, 1, tzinfo=UTC)),
        day=raw[:10],
        source=replace(SOURCE, timezone=ZoneInfo(zone), public_release_mode=Mode.WALL_TIME),
    )
    assert result.evidence[0].public_release_outcome is (
        Outcome.INVALID if expected is None else Outcome.ACCEPTED
    )
    assert result.episodes[0].edition_at == (
        None if expected is None else datetime.fromisoformat(expected)
    )


@pytest.mark.parametrize(
    "changes,day,outcome",
    [
        ({"source_id": SourceId("other")}, "2026-09-28", Outcome.IDENTITY_MISMATCH),
        ({"release_date_raw": "2026-09-28T05:21:00+00:99"}, "2026-09-28", Outcome.INVALID),
        ({"release_date_raw": "2026-09-28T05:21:00"}, "2026-09-28", Outcome.INVALID),
        ({"release_date_shape": PublicReleaseDateShape.STRING}, "2026-09-28", Outcome.INVALID),
        ({"release_date_raw": "2026-09-29T05:21:00Z"}, "2026-09-28", Outcome.CATALOGUE_CONFLICT),
        ({"release_date_raw": "2026-09-28T07:42:48Z"}, "2026-09-28", Outcome.FUTURE),
    ],
)
def test_wall_keeps_all_guards(changes, day, outcome):
    result = fetch(
        replace(evidence(), **changes),
        day=day,
        source=replace(SOURCE, public_release_mode=Mode.WALL_TIME),
    )
    assert result.evidence[0].public_release_outcome is outcome
    assert len(result.episodes) == 1


def test_mode_isolation_and_temporal_strategy_independence():
    public = evidence("2026-09-28T07:21:00Z")
    wall = replace(
        SOURCE, public_release_mode=Mode.WALL_TIME, temporal_strategy=TemporalStrategy.DISABLED
    )
    assert fetch(public, source=wall).episodes[0].edition_at == datetime(
        2026, 9, 28, 5, 21, tzinfo=UTC
    )
    assert fetch(public).evidence[0].public_release_outcome is Outcome.FUTURE
    assert (
        fetch(evidence(), source=replace(SOURCE, temporal_strategy=TemporalStrategy.DISABLED))
        .episodes[0]
        .timestamp_source
        is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    )


@pytest.mark.parametrize("mode", list(Mode))
def test_custom_roundtrip_and_builtin_isolation(mode):
    state = replace(
        _state(replace(_source(), public_release_mode=mode)),
        source_overrides=(ManagedSourceOverride(SourceId("rne"), Mode.DISABLED),),
    )
    payload = serialize_managed_state(state)
    assert payload["schema_version"] == 6
    assert payload["source_overrides"] == [{"source_id": "rne", "public_release_mode": "disabled"}]
    assert parse_managed_state(payload) == state
    compiled = effective_sources(BUILTIN_CATALOG, state)
    assert compiled[-1].public_release_mode is mode
    assert next(s for s in compiled if s.id == "rne").public_release_mode is Mode.DISABLED
    assert (
        next(s for s in BUILTIN_CATALOG.sources if s.id == "rne").public_release_mode
        is Mode.WALL_TIME
    )
    assert compiled[0] is BUILTIN_CATALOG.sources[0]


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_legacy_to_v5_atomic_defaults_and_playlist_preservation(tmp_path, version):
    payload = serialize_managed_state(_state())
    payload["schema_version"] = version
    payload.pop("source_overrides")
    for source in payload["sources"]:
        source.pop("public_release_mode")
    if version < 3:
        payload.pop("sources")
        payload["playlists"][0]["source_ids"] = ["ser"]
    expected = payload["playlists"]
    path = tmp_path / "managed-state.json"
    path.write_text(json.dumps(payload))
    state = ManagedStateStore(path).load()
    assert state.schema_version == 6
    assert state.source_overrides == ()
    assert all(s.public_release_mode is Mode.INSTANT for s in state.sources)
    current = json.loads(path.read_text())
    assert current["playlists"] == expected
    assert current["source_overrides"] == []
    canonical = path.read_bytes()
    assert ManagedStateStore(path).load() == state
    assert path.read_bytes() == canonical


@pytest.mark.parametrize("value", ["unknown", None, 42, [], {}])
def test_unknown_modes_fail_closed(value):
    payload = serialize_managed_state(_state())
    payload["sources"][0]["public_release_mode"] = value
    with pytest.raises(ManagedStateError, match="public_release_mode"):
        parse_managed_state(payload)
    payload = serialize_managed_state(ManagedState())
    payload["source_overrides"] = [{"source_id": "rne", "public_release_mode": value}]
    with pytest.raises(ManagedStateError, match="public_release_mode"):
        parse_managed_state(payload)


def test_override_validation_and_canonical_order():
    overrides = (
        ManagedSourceOverride(SourceId("ser"), Mode.WALL_TIME),
        ManagedSourceOverride(SourceId("rne"), Mode.INSTANT),
    )
    payload = serialize_managed_state(ManagedState(source_overrides=overrides))
    assert payload == serialize_managed_state(ManagedState(source_overrides=overrides[::-1]))
    assert [o["source_id"] for o in payload["source_overrides"]] == ["rne", "ser"]
    for invalid in (
        (overrides[0], overrides[0]),
        (ManagedSourceOverride(SourceId("unknown"), Mode.INSTANT),),
        (ManagedSourceOverride(_source().id, Mode.INSTANT),),
    ):
        with pytest.raises(ManagedStateError):
            serialize_managed_state(ManagedState(source_overrides=invalid))
        with pytest.raises(ManagedStateError):
            effective_sources(BUILTIN_CATALOG, ManagedState(source_overrides=invalid))


def test_no_v4_variant_with_new_policy():
    payload = serialize_managed_state(_state())
    payload["schema_version"] = 4
    with pytest.raises(ManagedStateError):
        parse_managed_state(payload)
    payload.pop("source_overrides")
    with pytest.raises(ManagedStateError, match="requires schema v5"):
        parse_managed_state(payload)


@pytest.mark.parametrize("coverage", [False, True])
def test_disabled_preserves_history_and_reenable_restores_original_instant(tmp_path, coverage):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    original = fetch(evidence()).episodes[0]
    journal.replace_source_episodes(SOURCE.id, [original], coverage_proven=True)
    fallback = replace(fetch().episodes[0], last_seen_at=NOW + timedelta(hours=1))
    journal.replace_source_episodes(
        SOURCE.id, [fallback], coverage_proven=coverage, reuse_public_release_history=False
    )
    assert journal.list_episodes()[0].timestamp_source is TimestampSource.FIRST_SEEN
    history = journal.list_episode_history(
        observed_since=NOW - timedelta(days=1), observed_until=NOW + timedelta(days=1)
    )
    assert history[0].edition_at == original.edition_at
    assert history[0].timestamp_source is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    assert history[0].last_seen_at == fallback.last_seen_at
    # Re-enable either mode: original accepted instant is eligible without reinterpretation.
    journal.replace_source_episodes(SOURCE.id, [fallback], coverage_proven=True)
    assert journal.list_episodes()[0].edition_at == original.edition_at
    assert store.format_version() == 3


@pytest.mark.parametrize(
    "initial,next_mode", [(Mode.INSTANT, Mode.WALL_TIME), (Mode.WALL_TIME, Mode.INSTANT)]
)
@pytest.mark.parametrize("coverage", [False, True])
@pytest.mark.parametrize("reenable", [False, True])
def test_mode_changes_reuse_history_only_without_new_public_evidence(
    tmp_path, initial, next_mode, coverage, reenable
):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    first = fetch(
        evidence("2026-09-28T05:21:17.123456Z"),
        source=replace(SOURCE, public_release_mode=initial),
    ).episodes[0]
    retained = replace(first, spotify_episode_id=OLD, spotify_episode_uri=f"spotify:episode:{OLD}")
    journal.replace_source_episodes(SOURCE.id, [first, retained], coverage_proven=True)
    fallback = fetch(source=replace(SOURCE, public_release_mode=next_mode)).episodes[0]
    assert fallback.timestamp_source is TimestampSource.FIRST_SEEN
    if reenable:
        journal.replace_source_episodes(
            SOURCE.id, [fallback], coverage_proven=False, reuse_public_release_history=False
        )
        assert all(
            e.timestamp_source is TimestampSource.FIRST_SEEN for e in journal.list_episodes()
        )
        assert all(
            e.timestamp_source is TimestampSource.SPOTIFY_PUBLIC_RELEASE
            for e in journal.list_episode_history(
                observed_since=NOW - timedelta(days=1), observed_until=NOW + timedelta(days=1)
            )
        )
    # A setting change alone does not reinterpret persisted timestamps. Without new
    # exact evidence, compatible PUBLIC remains eligible (also after re-enable).
    assert journal.list_episodes()[0].edition_at == (None if reenable else first.edition_at)
    journal.replace_source_episodes(SOURCE.id, [fallback], coverage_proven=False)
    current = {e.spotify_episode_id: e for e in journal.list_episodes()}
    assert current[EPISODE].edition_at == first.edition_at
    assert current[EPISODE].timestamp_source is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    if reenable:
        # Disable again to exercise re-enable directly WITH new public evidence.
        journal.replace_source_episodes(
            SOURCE.id, [fallback], coverage_proven=False, reuse_public_release_history=False
        )
    # A later observation of the SAME identity/calendar is validated under the new
    # mode; both directions must replace the old interpretation in current/history.
    public = evidence("2026-09-28T05:22:18.654321Z", observed=NOW + timedelta(hours=1))
    interpreted = replace(
        fetch(public, source=replace(SOURCE, public_release_mode=next_mode)).episodes[0],
        last_seen_at=NOW + timedelta(hours=1),
    )
    assert interpreted.timestamp_source is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    assert interpreted.edition_at != first.edition_at
    journal.replace_source_episodes(SOURCE.id, [interpreted], coverage_proven=coverage)
    current = {e.spotify_episode_id: e for e in journal.list_episodes()}
    assert current[EPISODE].edition_at == interpreted.edition_at
    assert current[EPISODE].timestamp_source is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    history = {
        e.spotify_episode_id: e
        for e in journal.list_episode_history(
            observed_since=NOW - timedelta(days=1), observed_until=NOW + timedelta(days=1)
        )
    }
    assert history[EPISODE].edition_at == interpreted.edition_at
    assert history[OLD].edition_at == first.edition_at
    if coverage:
        assert OLD not in current
    else:
        assert current[OLD].edition_at == (None if reenable else first.edition_at)
        assert current[OLD].timestamp_source is (
            TimestampSource.FIRST_SEEN if reenable else TimestampSource.SPOTIFY_PUBLIC_RELEASE
        )
    # The latest validated public instant is subsequently reusable without evidence.
    fallback = replace(fallback, last_seen_at=NOW + timedelta(hours=2))
    journal.replace_source_episodes(SOURCE.id, [fallback], coverage_proven=coverage)
    assert journal.list_episodes()[0].edition_at == interpreted.edition_at
    # Fresh semantic TITLE still wins over both the public observation and history.
    semantic = replace(
        fetch(
            public,
            title="Las noticias de la SER, 07:00 (28/09/2026)",
            source=replace(
                SOURCE,
                public_release_mode=next_mode,
                temporal_strategy=TemporalStrategy.FULL_DATETIME,
            ),
        ).episodes[0],
        last_seen_at=NOW + timedelta(hours=3),
    )
    assert semantic.timestamp_source is TimestampSource.TITLE
    journal.replace_source_episodes(SOURCE.id, [semantic], coverage_proven=coverage)
    assert journal.list_episodes()[0].edition_at == semantic.edition_at
    assert journal.list_episodes()[0].timestamp_source is TimestampSource.TITLE
    history = journal.list_episode_history(
        observed_since=NOW - timedelta(days=1), observed_until=NOW + timedelta(days=1)
    )
    assert history[0].timestamp_source is TimestampSource.TITLE
    assert store.format_version() == 3


def test_disabled_demotes_retained_public_current_rows_without_deleting_history(tmp_path):
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    journal.replace_source_episodes(SOURCE.id, fetch(evidence()).episodes, coverage_proven=True)
    journal.replace_source_episodes(
        SOURCE.id, (), coverage_proven=False, reuse_public_release_history=False
    )
    assert journal.list_episodes()[0].timestamp_source is TimestampSource.FIRST_SEEN
    assert (
        journal.list_episode_history(observed_since=NOW - timedelta(days=1), observed_until=NOW)[
            0
        ].timestamp_source
        is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    )


def test_public_mode_participates_in_playlist_write_guard():
    state = _state()
    config = compile_engine_config(BUILTIN_CATALOG, state)
    contract = _playlist_write_contract(config, state.playlists[0].id)
    changed = compile_engine_config(
        BUILTIN_CATALOG,
        replace(state, sources=(replace(state.sources[0], public_release_mode=Mode.DISABLED),)),
    )
    assert _playlist_write_contract(changed, state.playlists[0].id) != contract


@pytest.mark.parametrize("mode", list(Mode))
def test_http_custom_modes_create_edit_and_next_refresh_without_extra_io(tmp_path, mode):
    from _html import assert_valid_admin_html
    from test_source_admin_http import OTHER, SHOW

    with _server(tmp_path) as (server, client, store):
        cookie = _login(server)
        _, review = _inspect(server, cookie)
        assert b'<option value="instant" selected>Instant (recommended)</option>' in review
        assert b"Title time detection" in review
        form = {**_create_form(review), "public_release_mode": mode.value}
        assert _request(server, "POST", "/admin/sources/create", cookie=cookie, form=form)[0] == 303
        source_id = f"custom_{SHOW}"
        calls = list(client.calls)
        status, _, edit = _request(
            server, "GET", f"/admin/sources/edit?source_id={source_id}", cookie=cookie
        )
        assert status == 200
        assert_valid_admin_html(edit.decode())
        assert f'<option value="{mode.value}" selected>'.encode() in edit
        assert client.calls == calls
        changed = Mode.DISABLED if mode is not Mode.DISABLED else Mode.WALL_TIME
        form = {**_update_form(_csrf(edit)), "public_release_mode": changed.value}
        assert _request(server, "POST", "/admin/sources/update", cookie=cookie, form=form)[0] == 303
        assert client.calls == calls
        source = effective_sources(BUILTIN_CATALOG, store.load())[-1]
        assert source.public_release_mode is changed
        assert source.temporal_strategy is TemporalStrategy.AUTO
        service = _Handler.source_admin_service
        observed = datetime.now(UTC)
        raw = (
            (observed.astimezone(source.timezone) - timedelta(minutes=1))
            .replace(tzinfo=UTC)
            .isoformat()
        )
        observation = PublicEpisodeObservation(
            source.id,
            SHOW,
            OTHER,
            f"spotify:episode:{OTHER}",
            "News",
            "News",
            raw,
            None,
            None,
            observed,
            PublicReleaseDateShape.OBJECT_ISO_STRING,
        )
        service.detector_journal.record_success(observation)
        runner = object.__new__(EngineRunner)
        runner.config = compile_engine_config(BUILTIN_CATALOG, store.load())
        runner.first_journal = service.first_journal
        runner.detector_journal = service.detector_journal
        runner.max_catalogue_pages = 4
        outcome = runner._refresh_source(
            source,
            RefreshReason.CHANGE,
            PublicDetectorCycleSummary(1, 1, 0, 0),
            client,
            now=observed,
        )  # noqa: SLF001
        assert outcome.ok and outcome.api_request_count == 1
        assert client.calls == [*calls, "episodes"]
        assert outcome.temporal_metrics.public_release_mode is changed
        persisted = service.first_journal.list_episodes()[0]
        assert persisted.timestamp_source is (
            TimestampSource.FIRST_SEEN
            if changed is Mode.DISABLED
            else TimestampSource.SPOTIFY_PUBLIC_RELEASE
        )
        if changed is Mode.WALL_TIME:
            assert persisted.edition_at == observed - timedelta(minutes=1)


@pytest.mark.parametrize("mode", list(Mode))
def test_http_builtin_edit_only_policy_and_restores_image_default(tmp_path, mode):
    from _html import assert_valid_admin_html

    with _server(tmp_path) as (server, client, store):
        _configured_source(store)
        previous = store.load()
        cookie = _login(server)
        status, _, page = _request(
            server, "GET", "/admin/sources/edit?source_id=rne", cookie=cookie
        )
        assert status == 200
        assert_valid_admin_html(page.decode())
        assert b"Title time detection (read-only)" in page
        assert b'<select name="temporal_strategy">' not in page
        assert b'<option value="wall_time" selected>' in page
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/public-release-mode",
                cookie=cookie,
                form={
                    "csrf_token": _csrf(page),
                    "source_id": "rne",
                    "public_release_mode": mode.value,
                },
            )[0]
            == 303
        )
        state = store.load()
        assert state.sources == previous.sources and state.playlists == previous.playlists
        assert len(state.source_overrides) == (0 if mode is Mode.WALL_TIME else 1)
        assert (
            next(
                s for s in effective_sources(BUILTIN_CATALOG, state) if s.id == "rne"
            ).public_release_mode
            is mode
        )
        assert client.calls == []
        _, _, page = _request(server, "GET", "/admin/sources/edit?source_id=rne", cookie=cookie)
        assert (
            _request(
                server,
                "POST",
                "/admin/sources/public-release-mode",
                cookie=cookie,
                form={
                    "csrf_token": _csrf(page),
                    "source_id": "rne",
                    "public_release_mode": "wall_time",
                },
            )[0]
            == 303
        )
        assert store.load().source_overrides == ()
        assert client.calls == []


@pytest.mark.parametrize("action", ["update", "public-release-mode"])
def test_http_mode_auth_csrf_unknown_enum_source_and_duplicate_fields(tmp_path, action):
    with _server(tmp_path) as (server, client, store):
        custom = _configured_source(store)
        source_id = str(custom.id) if action == "update" else "rne"
        path = f"/admin/sources/{action}"
        assert _request(server, "POST", path, form={})[0] == 303
        assert _request(server, "GET", f"/admin/sources/edit?source_id={source_id}")[0] == 303
        cookie = _login(server)
        previous = store.load()
        assert _request(server, "POST", path, cookie=cookie, form={"csrf_token": "bad"})[0] == 403
        for values, expected in [
            ({"public_release_mode": "invalid"}, 400),
            ({"source_id": "unknown", "public_release_mode": "instant"}, 409),
            ({"public_release_mode": ["instant", "disabled"]}, 400),
        ]:
            _, _, page = _request(
                server, "GET", f"/admin/sources/edit?source_id={source_id}", cookie=cookie
            )
            form = (
                _update_form(_csrf(page))
                if action == "update"
                else {"csrf_token": _csrf(page), "source_id": "rne"}
            )
            form.update(values)
            assert _request(server, "POST", path, cookie=cookie, form=form)[0] == expected
            assert store.load() == previous
        assert (
            _request(server, "GET", "/admin/sources/edit?source_id=unknown", cookie=cookie)[0]
            == 404
        )
        assert client.calls == []


@pytest.mark.parametrize("mode", list(Mode))
def test_mode_diagnostics_current_snapshot_counts(tmp_path, mode):
    result = fetch(evidence(), source=replace(SOURCE, public_release_mode=mode))
    metrics = TemporalRefreshMetrics.from_snapshot(result)
    store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    store.initialize()
    runner = InstrumentedEngineCycleRunner(None, OperationalDiagnostics(store))
    outcome = SourceCycleOutcome(
        SOURCE.id, True, 1, True, "change", 1, NOW, NOW, True, True, temporal_metrics=metrics
    )
    runner._emit_source(outcome, NOW, "cycle-mode")  # noqa: SLF001
    details = store.list_events(limit=1)[0].details
    assert details["public_release_mode"] == mode.value
    assert details["public_release_disabled_count"] == int(mode is Mode.DISABLED)
    assert details["public_release_accepted_count"] == int(mode is not Mode.DISABLED)
    assert (
        sum(
            details[key]
            for key in (
                "timestamp_title_count",
                "timestamp_public_count",
                "timestamp_first_seen_count",
            )
        )
        == 1
    )


@pytest.mark.parametrize("mode", list(Mode))
def test_individual_audit_and_zip_share_mode_outcome_and_request_counts(tmp_path, mode):
    from test_source_admin_http import OTHER

    with _server(tmp_path) as (_server_unused, client, store):
        custom = _configured_source(store)
        store.save(replace(store.load(), sources=(replace(custom, public_release_mode=mode),)))
        service = _Handler.source_admin_service
        now = datetime.now(UTC)

        class Public:
            calls = 0

            def observe_episode(self, source_id, spotify_show_id, *, observed_at):
                self.calls += 1
                raw = (
                    observed_at
                    if mode is Mode.INSTANT
                    else observed_at.astimezone(ZoneInfo("Europe/Madrid")).replace(tzinfo=UTC)
                )
                return PublicEpisodeObservation(
                    source_id,
                    spotify_show_id,
                    OTHER,
                    f"spotify:episode:{OTHER}",
                    "News",
                    "News",
                    raw.isoformat(),
                    None,
                    None,
                    observed_at,
                    PublicReleaseDateShape.OBJECT_ISO_STRING,
                )

        public = Public()
        report = inspect_configured_source(
            custom.id,
            catalog=BUILTIN_CATALOG,
            state=store.load(),
            auth=service.auth,
            rate_limit=service.rate_limit,
            now=now,
            client_factory=lambda token: client,
            public_client=public,
        )
        assert public.calls == 1 and client.calls == ["details", "episodes"]
        payload = serialize_source_inspection(report)
        document = json.loads(payload)
        assert document["public_release_mode"] == mode.value
        assert document["episodes"][0]["public_release_outcome"] == (
            "disabled_by_policy" if mode is Mode.DISABLED else "accepted"
        )
        batch = SourceAuditBatch(
            now,
            (
                SourceAuditBatchItem(
                    custom.id, custom.display_name, "custom", True, "succeeded", report
                ),
            ),
        )
        archive = build_source_audit_bundle(batch, build_revision="test")
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            assert bundle.read(f"sources/{custom.id}.json") == payload
        assert public.calls == 1 and client.calls == ["details", "episodes"]
        for secret in (
            b"secret-access-token",
            b"secret-refresh-token",
            b"secret-provider-body",
            b"Authorization",
        ):
            assert secret not in payload


@pytest.mark.parametrize(
    "source_id,raw,observed,day",
    [
        ("ser", "2026-09-27T18:43:00Z", "2026-09-27T19:15:00Z", "2026-09-27"),
        ("ondacero", "2026-09-27T11:00:00Z", "2026-09-27T11:24:00Z", "2026-09-27"),
        ("rmf_fakty", "2026-09-27T18:00:00Z", "2026-09-27T18:25:00Z", "2026-09-27"),
        ("abc", "2026-09-28T00:45:00Z", "2026-09-28T01:04:47Z", "2026-09-28"),
        ("reuters_world", "2026-09-27T13:58:00Z", "2026-09-27T14:02:48Z", "2026-09-27"),
        ("cnn", "2026-09-27T17:09:00Z", "2026-09-27T17:25:48Z", "2026-09-27"),
        ("un_news_en", "2026-09-28T15:54:00Z", "2026-09-28T17:16:11Z", "2026-09-28"),
    ],
)
def test_recorded_sources_keep_literal_instant(source_id, raw, observed, day):
    source = next(s for s in BUILTIN_CATALOG.sources if s.id == source_id)
    assert source.public_release_mode is Mode.INSTANT
    public = replace(
        evidence(raw, observed=datetime.fromisoformat(observed)),
        source_id=source.id,
        spotify_show_id=spotify_show_id(source),
    )
    result = fetch(public, source=source, day=day, title="No editorial clock")
    assert result.episodes[0].edition_at == datetime.fromisoformat(raw)
    assert result.evidence[0].public_release_outcome is Outcome.ACCEPTED


def test_audit_all_uses_effective_builtin_override_and_custom_policy(tmp_path):
    from test_source_admin_http import _PublicAudit

    with _server(tmp_path) as (_server_unused, client, store):
        custom = _configured_source(store)
        service = _Handler.source_admin_service
        # Bounded test catalogue still uses real SourceAdminService.audit_all and inspection.
        store.save(replace(store.load(), playlists=()))
        service.catalog = replace(
            BUILTIN_CATALOG, sources=BUILTIN_CATALOG.sources[:2], playlists=()
        )
        service.update_builtin_public_release_mode("rne", "disabled")
        source_state = store.load()
        store.save(
            replace(
                source_state,
                sources=(replace(custom, public_release_mode=Mode.WALL_TIME),),
                playlists=(),
            )
        )
        public = _PublicAudit()
        service.public_client = public
        batch = service.audit_all()
        reports = {item.source_id: item.report for item in batch.items}
        assert reports["ser"].public_release_mode is Mode.INSTANT
        assert reports["rne"].public_release_mode is Mode.DISABLED
        assert reports["rne"].episodes[0].public_release_outcome is Outcome.DISABLED
        assert reports[custom.id].public_release_mode is Mode.WALL_TIME
        assert public.calls == 3 and client.calls == ["details", "episodes"] * 3
        with zipfile.ZipFile(
            io.BytesIO(build_source_audit_bundle(batch, build_revision="test"))
        ) as archive:
            for source_id, report in reports.items():
                assert archive.read(f"sources/{source_id}.json") == serialize_source_inspection(
                    report
                )
        assert public.calls == 3 and len(client.calls) == 6


@pytest.mark.parametrize("mode", [Mode.INSTANT, Mode.WALL_TIME])
def test_exact_fraction_survives_both_enabled_modes(mode):
    result = fetch(
        evidence("2026-09-28T05:21:17.123456Z"), source=replace(SOURCE, public_release_mode=mode)
    )
    assert result.episodes[0].edition_at == datetime(
        2026, 9, 28, 5 if mode is Mode.INSTANT else 3, 21, 17, 123456, tzinfo=UTC
    )


@pytest.mark.parametrize("seconds,accepted", [(299, True), (300, True), (301, False)])
def test_wall_future_bound_uses_resulting_instant(seconds, accepted):
    raw = (
        (NOW + timedelta(seconds=seconds))
        .astimezone(SOURCE.timezone)
        .replace(tzinfo=UTC)
        .isoformat()
    )
    result = fetch(evidence(raw), source=replace(SOURCE, public_release_mode=Mode.WALL_TIME))
    assert result.evidence[0].public_release_outcome is (
        Outcome.ACCEPTED if accepted else Outcome.FUTURE
    )


@pytest.mark.parametrize(
    "invalid",
    [
        [{"source_id": "rne", "public_release_mode": "instant"}] * 2,
        [{"source_id": "unknown", "public_release_mode": "instant"}],
        [{"source_id": "rne", "public_release_mode": "instant", "timezone": "UTC"}],
    ],
)
def test_invalid_override_payload_fails_closed(invalid):
    payload = serialize_managed_state(ManagedState())
    payload["source_overrides"] = invalid
    with pytest.raises(ManagedStateError):
        parse_managed_state(payload)


@pytest.mark.parametrize("version", [3, 4])
def test_new_migration_write_failure_preserves_original(tmp_path, monkeypatch, version):
    payload = serialize_managed_state(_state())
    payload["schema_version"] = version
    payload.pop("source_overrides")
    payload["sources"][0].pop("public_release_mode")
    path = tmp_path / "managed-state.json"
    original = json.dumps(payload).encode()
    path.write_bytes(original)

    def fail_replace(*args):
        raise OSError("test failure")

    monkeypatch.setattr("news_bulletin_playlist.managed_state.os.replace", fail_replace)
    with pytest.raises(ManagedStateError, match="migration could not be persisted"):
        ManagedStateStore(path).load()
    assert path.read_bytes() == original


def test_disabled_engine_refresh_blocks_historical_reattachment_then_reenable(tmp_path):
    from _spotify_catalogue import catalogue_page

    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    detector = PublicDetectorJournal(store)
    detector.initialize()
    original = fetch(evidence()).episodes[0]
    journal.replace_source_episodes(SOURCE.id, [original], coverage_proven=True)
    # No current public evidence: the refresh would previously have reused PUBLIC history.
    runner = object.__new__(EngineRunner)
    runner.config = compile_engine_config(BUILTIN_CATALOG, ManagedState())
    runner.first_journal = journal
    runner.detector_journal = detector
    runner.max_catalogue_pages = 4

    class Client:
        calls = 0

        def show_episodes(self, show_id, *, limit=50, offset=0):
            self.calls += 1
            return catalogue_page(
                [
                    dict(
                        id=EPISODE,
                        uri=f"spotify:episode:{EPISODE}",
                        name="Unstructured",
                        release_date="2026-09-28",
                        release_date_precision="day",
                    )
                ]
            )

    client = Client()
    for mode in (Mode.DISABLED, Mode.WALL_TIME):
        source = replace(SOURCE, public_release_mode=mode)
        outcome = runner._refresh_source(
            source, RefreshReason.CHANGE, PublicDetectorCycleSummary(0, 0, 0, 0), client, now=NOW
        )  # noqa: SLF001
        assert outcome.ok
        assert journal.list_episodes()[0].timestamp_source is (
            TimestampSource.FIRST_SEEN
            if mode is Mode.DISABLED
            else TimestampSource.SPOTIFY_PUBLIC_RELEASE
        )
        history = journal.list_episode_history(
            observed_since=NOW - timedelta(days=1), observed_until=NOW
        )
        assert history[0].edition_at == original.edition_at
    assert client.calls == 2


def test_disabled_history_failure_rolls_back_current_demotion(tmp_path):
    import sqlite3

    from news_bulletin_playlist.persistence import PersistenceError

    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    original = fetch(evidence()).episodes
    journal.replace_source_episodes(SOURCE.id, original, coverage_proven=True)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_history_insert BEFORE INSERT ON "
            "spotify_first_episode_history BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    with pytest.raises(PersistenceError):
        journal.replace_source_episodes(
            SOURCE.id, fetch().episodes, coverage_proven=True, reuse_public_release_history=False
        )
    assert journal.list_episodes() == original
    assert (
        journal.list_episode_history(observed_since=NOW - timedelta(days=1), observed_until=NOW)
        == original
    )


@pytest.mark.parametrize("operation", ["update", "set_enabled", "stop_managing", "activate"])
def test_playlist_operations_preserve_builtin_overrides(tmp_path, operation):
    from test_managed_admin import _service

    service, factory = _service(tmp_path, [{"id": "new-destination"}])
    overrides = (ManagedSourceOverride(SourceId("rne"), Mode.DISABLED),)
    state = replace(_state(), source_overrides=overrides)
    service.store.save(state)
    playlist = state.playlists[0]
    if operation == "activate":
        template = BUILTIN_CATALOG.playlist("international_english_news")
        service.activate(
            template_id=template.id,
            display_name=template.display_name,
            description=template.description,
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            access_token="test-token",
        )
    elif operation == "update":
        service.update(
            playlist.id,
            display_name=playlist.display_name,
            description="Updated",
            cover_id=playlist.cover_id,
            source_ids=playlist.source_ids,
            enabled=True,
            access_token=None,
        )
    elif operation == "set_enabled":
        service.set_enabled(playlist.id, False)
    else:
        service.stop_managing(playlist.id)
    assert service.store.load().source_overrides == overrides
    assert service.store.load().sources == state.sources
    if operation != "activate":
        assert factory.tokens == []
