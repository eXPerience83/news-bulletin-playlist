from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_admin import (
    ManagedAdminError,
    ManagedAdminService,
    PlaylistProvisioningRecoveryRequired,
    SpotifyPlaylistCreationUncertainError,
    SpotifyPlaylistProvisioningError,
)
from news_bulletin_playlist.managed_provisioning import (
    ProvisioningIntent,
    ProvisioningJournal,
    ProvisioningJournalError,
    ProvisioningState,
)
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError


class _Client:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.responses = list(responses or [{"id": "created-destination"}])
        self.create_calls: list[tuple[str, bool]] = []
        self.user = {"id": "owner"}
        self.playlist: dict[str, Any] = {"id": "A" * 22, "owner": {"id": "owner"}}

    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, Any]:
        del description
        self.create_calls.append((name, public))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response

    def current_user(self) -> dict[str, Any]:
        return dict(self.user)

    def playlist_details(self, playlist_id: str) -> dict[str, Any]:
        result = dict(self.playlist)
        result["id"] = playlist_id if result.get("id") == "MATCH" else result.get("id")
        return result

    def change_playlist_details(
        self, playlist_id: str, *, name: str, description: str
    ) -> dict[str, Any]:
        del playlist_id, name, description
        return {}

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        del playlist_id, jpeg_bytes
        return {}


class _Factory:
    def __init__(self, client: _Client) -> None:
        self.client = client

    def __call__(self, token: str) -> _Client:
        assert token
        return self.client


class _FailingStateStore(ManagedStateStore):
    def save(self, state: object) -> None:
        del state
        raise OSError("simulated durable-state failure")


class _FailingJournal(ProvisioningJournal):
    def __init__(self, path: Path, *, fail_known: bool = False, fail_clear: bool = False) -> None:
        super().__init__(path)
        self.fail_known = fail_known
        self.fail_clear = fail_clear

    def save(self, intent: ProvisioningIntent) -> None:
        if self.fail_known and intent.state is ProvisioningState.DESTINATION_KNOWN:
            raise ProvisioningJournalError("simulated destination journal failure")
        super().save(intent)

    def clear(self) -> None:
        if self.fail_clear:
            raise ProvisioningJournalError("simulated clear failure")
        super().clear()


class _ForbiddenProfileClient(_Client):
    def current_user(self) -> dict[str, Any]:
        raise SpotifyApiError(403, "forbidden")


def _service(
    tmp_path: Path,
    client: _Client,
    *,
    store: ManagedStateStore | None = None,
    journal: ProvisioningJournal | None = None,
    catalog: BuiltInCatalog = BUILTIN_CATALOG,
) -> ManagedAdminService:
    return ManagedAdminService(
        store or ManagedStateStore(tmp_path / "managed-state.json"),
        catalog=catalog,
        client_factory=_Factory(client),
        provisioning_journal=journal or ProvisioningJournal(tmp_path / "provisioning.json"),
    )


def _activate(service: ManagedAdminService):  # type: ignore[no-untyped-def]
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    return service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )


def _request_intent() -> ProvisioningIntent:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    return ProvisioningIntent(
        state=ProvisioningState.REQUEST_STARTED,
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        retention_hours=template.retention_hours,
        max_episodes=template.max_episodes,
        max_duration_seconds=1800,
    )


def test_request_journal_failure_prevents_remote_create(tmp_path: Path) -> None:
    client = _Client()
    journal = _FailingJournal(tmp_path / "provisioning.json")
    journal.fail_known = False
    journal.save = lambda _intent: (_ for _ in ()).throw(ProvisioningJournalError("no disk"))  # type: ignore[method-assign]
    with pytest.raises(ProvisioningJournalError):
        _activate(_service(tmp_path, client, journal=journal))
    assert client.create_calls == []


def test_restart_after_request_started_before_remote_create_blocks_duplicate_create(
    tmp_path: Path,
) -> None:
    client = _Client()
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(_request_intent())

    with pytest.raises(PlaylistProvisioningRecoveryRequired):
        _activate(_service(tmp_path, client, journal=journal))

    assert client.create_calls == []


@pytest.mark.parametrize(
    "failure", [SpotifyTransportError("timeout"), SpotifyApiError(503, "down")]
)
def test_uncertain_create_survives_restart_and_blocks_second_create(
    tmp_path: Path, failure: Exception
) -> None:
    client = _Client([failure])
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    service = _service(tmp_path, client, journal=journal)
    with pytest.raises(SpotifyPlaylistCreationUncertainError):
        _activate(service)
    assert journal.load().state is ProvisioningState.REQUEST_STARTED  # type: ignore[union-attr]
    restarted = _service(tmp_path, client, journal=journal)
    with pytest.raises(PlaylistProvisioningRecoveryRequired):
        _activate(restarted)
    assert len(client.create_calls) == 1


@pytest.mark.parametrize(
    "playlist",
    [
        {"id": "MATCH", "name": "Wrong name", "owner": {"id": "owner"}},
        {"id": "MATCH", "name": "Noticias en Español", "owner": {}},
    ],
)
def test_adoption_rejects_wrong_name_or_malformed_response_without_changing_intent(
    tmp_path: Path, playlist: dict[str, Any]
) -> None:
    client = _Client()
    client.playlist = playlist
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(_request_intent())
    with pytest.raises(ManagedAdminError):
        _service(tmp_path, client, journal=journal).adopt_uncertain_provisioning(
            "A" * 22, access_token="token"
        )
    assert journal.load().state is ProvisioningState.REQUEST_STARTED  # type: ignore[union-attr]


def test_adoption_profile_scope_failure_is_actionable_and_preserves_intent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(_request_intent())
    with pytest.raises(
        ManagedAdminError,
        match="reconnecting Spotify.*user-read-private",
    ) as raised:
        _service(tmp_path, _ForbiddenProfileClient(), journal=journal).adopt_uncertain_provisioning(
            "A" * 22, access_token="token-sentinel"
        )
    assert journal.load().state is ProvisioningState.REQUEST_STARTED  # type: ignore[union-attr]
    assert "token-sentinel" not in str(raised.value)
    assert "token-sentinel" not in capsys.readouterr().out


def test_known_destination_recovery_uses_journaled_template_snapshot(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    intent = _request_intent().with_destination("created")
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(intent)
    changed_template = replace(template, retention_hours=12, max_episodes=7)
    changed_catalog = BuiltInCatalog(
        sources=BUILTIN_CATALOG.sources,
        playlists=tuple(
            changed_template if item.id == template.id else item
            for item in BUILTIN_CATALOG.playlists
        ),
    )
    recovered = _service(
        tmp_path,
        _Client(),
        journal=journal,
        catalog=changed_catalog,
    ).finalize_known_provisioning()
    assert recovered is not None
    assert recovered.retention_hours == template.retention_hours
    assert recovered.max_episodes == template.max_episodes


def test_definite_create_rejection_clears_intent_and_allows_safe_retry(tmp_path: Path) -> None:
    client = _Client([SpotifyApiError(400, "bad"), {"id": "created"}])
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    service = _service(tmp_path, client, journal=journal)
    with pytest.raises(SpotifyApiError):
        _activate(service)
    assert journal.load() is None
    assert _activate(service).destination.external_id == "created"
    assert len(client.create_calls) == 2


def test_normal_activation_snapshots_template_retention_and_max_episodes(tmp_path: Path) -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = _activate(_service(tmp_path, _Client([{"id": "created"}])))
    assert managed.retention_hours == template.retention_hours
    assert managed.max_episodes == template.max_episodes


def test_success_before_known_journal_persistence_blocks_retry(tmp_path: Path) -> None:
    client = _Client([{"id": "created"}])
    journal = _FailingJournal(tmp_path / "provisioning.json", fail_known=True)
    with pytest.raises(SpotifyPlaylistCreationUncertainError):
        _activate(_service(tmp_path, client, journal=journal))
    assert journal.load().state is ProvisioningState.REQUEST_STARTED  # type: ignore[union-attr]
    with pytest.raises(PlaylistProvisioningRecoveryRequired):
        _activate(_service(tmp_path, client, journal=journal))
    assert len(client.create_calls) == 1


def test_known_destination_restart_finalizes_without_remote_create(tmp_path: Path) -> None:
    client = _Client()
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(_request_intent().with_destination("created"))
    service = _service(tmp_path, client, journal=journal)
    assert service.finalize_known_provisioning().destination.external_id == "created"  # type: ignore[union-attr]
    assert journal.load() is None
    assert client.create_calls == []


def test_state_save_failure_leaves_known_destination_recoverable(tmp_path: Path) -> None:
    client = _Client([{"id": "created"}])
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    with pytest.raises(SpotifyPlaylistProvisioningError):
        _activate(
            _service(
                tmp_path,
                client,
                store=_FailingStateStore(tmp_path / "managed-state.json"),
                journal=journal,
            )
        )
    assert journal.load().destination_id == "created"  # type: ignore[union-attr]
    recovered = _service(tmp_path, client, journal=journal).finalize_known_provisioning()
    assert recovered is not None and recovered.destination.external_id == "created"
    assert client.create_calls == [("Noticias en Español", True)]


def test_stale_known_journal_after_state_save_self_heals_without_create(tmp_path: Path) -> None:
    client = _Client([{"id": "created"}])
    journal = _FailingJournal(tmp_path / "provisioning.json", fail_clear=True)
    with pytest.raises(SpotifyPlaylistProvisioningError):
        _activate(_service(tmp_path, client, journal=journal))
    journal.fail_clear = False
    recovered = _service(tmp_path, client, journal=journal).finalize_known_provisioning()
    assert recovered is not None and recovered.destination.external_id == "created"
    assert journal.load() is None
    assert len(client.create_calls) == 1


def test_adoption_requires_existing_playlist_owned_by_authorized_user(tmp_path: Path) -> None:
    client = _Client()
    client.playlist = {
        "id": "MATCH",
        "name": BUILTIN_CATALOG.playlist("spain_spanish_news").display_name,
        "owner": {"id": "owner"},
    }
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(_request_intent())
    playlist_id = "A" * 22
    adopted = _service(tmp_path, client, journal=journal).adopt_uncertain_provisioning(
        playlist_id, access_token="token"
    )
    assert adopted.destination.external_id == playlist_id
    assert journal.load() is None


@pytest.mark.parametrize(
    "playlist",
    [
        {"id": "other", "name": "Noticias en Español", "owner": {"id": "owner"}},
        {"id": "MATCH", "name": "Noticias en Español", "owner": {"id": "foreign"}},
    ],
)
def test_adoption_rejects_unexpected_or_foreign_playlist(
    tmp_path: Path, playlist: dict[str, Any]
) -> None:
    client = _Client()
    client.playlist = playlist
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(_request_intent())
    with pytest.raises(ManagedAdminError):
        _service(tmp_path, client, journal=journal).adopt_uncertain_provisioning(
            "A" * 22, access_token="token"
        )
    assert journal.load().state is ProvisioningState.REQUEST_STARTED  # type: ignore[union-attr]


def test_clear_uncertain_intent_is_local_only(tmp_path: Path) -> None:
    client = _Client()
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(_request_intent())
    _service(tmp_path, client, journal=journal).clear_uncertain_provisioning()
    assert journal.load() is None
    assert client.create_calls == []


def test_journal_contains_no_credentials(tmp_path: Path) -> None:
    journal = ProvisioningJournal(tmp_path / "provisioning.json")
    journal.save(_request_intent())
    payload = json.loads((tmp_path / "provisioning.json").read_text())
    assert set(payload) == {
        "schema_version",
        "state",
        "template_id",
        "display_name",
        "description",
        "cover_id",
        "source_ids",
        "retention_hours",
        "max_episodes",
        "max_duration_seconds",
        "destination_id",
    }
