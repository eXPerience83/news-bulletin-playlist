from __future__ import annotations

import json
import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import news_bulletin_playlist.spotify.auth as auth
from news_bulletin_playlist.spotify.auth import (
    PRODUCTION_SCOPES,
    AuthorizationState,
    CredentialStatus,
    SpotifyAuthConfigurationError,
    SpotifyAuthorizationDenied,
    SpotifyAuthService,
    SpotifyCredentialRecord,
    SpotifyCredentialStore,
    SpotifyCredentialStoreError,
    SpotifyOAuthCallbackError,
    SpotifyReauthorizationRequired,
    SpotifyTokenEndpointError,
    SpotifyTokenError,
    TokenResponse,
    build_redirect_uri,
)

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
REDIRECT_URI = "https://news.example.test/admin/spotify/callback"


class _FakeTransport:
    def __init__(self) -> None:
        self.exchange_response = TokenResponse(
            access_token="access-initial",
            expires_in=3600,
            granted_scopes=PRODUCTION_SCOPES,
            refresh_token="refresh-initial",
        )
        self.refresh_response = TokenResponse(
            access_token="access-refreshed",
            expires_in=3600,
            granted_scopes=PRODUCTION_SCOPES,
            refresh_token=None,
        )
        self.exchange_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.exchange_calls: list[dict[str, str]] = []
        self.refresh_calls: list[dict[str, str]] = []

    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> TokenResponse:
        self.exchange_calls.append(
            {
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "verifier": verifier,
            }
        )
        if self.exchange_error is not None:
            raise self.exchange_error
        return self.exchange_response

    def refresh_token(self, *, client_id: str, refresh_token: str) -> TokenResponse:
        self.refresh_calls.append({"client_id": client_id, "refresh_token": refresh_token})
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.refresh_response


class _FailingStore(SpotifyCredentialStore):
    def save(self, record: SpotifyCredentialRecord) -> None:
        del record
        raise SpotifyCredentialStoreError("simulated persistence failure")


def _service(
    tmp_path: Path,
    *,
    transport: _FakeTransport | None = None,
    store: SpotifyCredentialStore | None = None,
) -> tuple[SpotifyAuthService, _FakeTransport, SpotifyCredentialStore]:
    fake = transport if transport is not None else _FakeTransport()
    credential_store = (
        store
        if store is not None
        else SpotifyCredentialStore(tmp_path / auth.SPOTIFY_AUTH_FILENAME)
    )
    service = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=REDIRECT_URI,
        store=credential_store,
        transport=fake,
    )
    return service, fake, credential_store


def _start(service: SpotifyAuthService) -> tuple[str, str]:
    authorize_url = service.start_authorization(now=NOW)
    parsed = urllib.parse.urlsplit(authorize_url)
    params = urllib.parse.parse_qs(parsed.query)
    return authorize_url, params["state"][0]


def _authorize(service: SpotifyAuthService) -> None:
    _, state = _start(service)
    service.complete_callback(
        urllib.parse.urlencode({"state": state, "code": "authorization-code"}),
        now=NOW,
    )


def test_redirect_uri_requires_https_except_explicit_loopback() -> None:
    assert build_redirect_uri("https://news.example.test") == REDIRECT_URI
    assert (
        build_redirect_uri("http://127.0.0.1:8788")
        == "http://127.0.0.1:8788/admin/spotify/callback"
    )
    assert (
        build_redirect_uri("http://[::1]:8788")
        == "http://[::1]:8788/admin/spotify/callback"
    )

    for invalid in (
        "http://192.168.1.20:8788",
        "http://localhost:8788",
        "https://user:pass@example.test",
        "https://example.test/base",
        "https://example.test?query=yes",
    ):
        with pytest.raises(SpotifyAuthConfigurationError):
            build_redirect_uri(invalid)


def test_authorize_url_uses_pkce_state_exact_redirect_and_minimal_scopes(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    authorize_url, state = _start(service)

    parsed = urllib.parse.urlsplit(authorize_url)
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.spotify.com"
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [state]
    assert tuple(params["scope"][0].split()) == PRODUCTION_SCOPES
    assert "client_secret" not in params


def test_state_mismatch_fails_without_consuming_legitimate_pending_flow(tmp_path: Path) -> None:
    service, transport, _ = _service(tmp_path)
    _, state = _start(service)

    with pytest.raises(SpotifyOAuthCallbackError, match="state validation failed"):
        service.complete_callback("state=wrong&code=attacker", now=NOW)

    service.complete_callback(
        urllib.parse.urlencode({"state": state, "code": "real-code"}),
        now=NOW,
    )
    assert len(transport.exchange_calls) == 1
    assert transport.exchange_calls[0]["code"] == "real-code"


def test_denial_is_safe_and_consumes_pending_flow(tmp_path: Path) -> None:
    service, transport, _ = _service(tmp_path)
    _, state = _start(service)

    with pytest.raises(SpotifyAuthorizationDenied, match="not granted"):
        service.complete_callback(
            urllib.parse.urlencode({"state": state, "error": "access_denied"}),
            now=NOW,
        )
    with pytest.raises(SpotifyOAuthCallbackError, match="No Spotify authorization"):
        service.complete_callback(
            urllib.parse.urlencode({"state": state, "code": "late-code"}),
            now=NOW,
        )
    assert transport.exchange_calls == []


def test_expired_callback_fails_closed_and_consumes_pending_flow(tmp_path: Path) -> None:
    service, transport, store = _service(tmp_path)
    _, state = _start(service)
    callback = urllib.parse.urlencode({"state": state, "code": "late-code"})

    with pytest.raises(SpotifyOAuthCallbackError, match="expired"):
        service.complete_callback(callback, now=NOW + timedelta(minutes=11))
    with pytest.raises(SpotifyOAuthCallbackError, match="No Spotify authorization"):
        service.complete_callback(callback, now=NOW)

    assert transport.exchange_calls == []
    assert not store.path.exists()


def test_successful_callback_cannot_be_replayed(tmp_path: Path) -> None:
    service, transport, store = _service(tmp_path)
    _, state = _start(service)
    callback = urllib.parse.urlencode({"state": state, "code": "single-use-code"})

    service.complete_callback(callback, now=NOW)
    first_record = store.load()
    with pytest.raises(SpotifyOAuthCallbackError, match="No Spotify authorization"):
        service.complete_callback(callback, now=NOW)

    assert len(transport.exchange_calls) == 1
    assert store.load() == first_record


def test_success_persists_only_refresh_credential_with_owner_only_permissions(
    tmp_path: Path,
) -> None:
    service, _, store = _service(tmp_path)

    _authorize(service)

    assert service.authorization_state() is AuthorizationState.CONNECTED
    record = store.load()
    assert record is not None
    assert record.status is CredentialStatus.ACTIVE
    assert record.refresh_token == "refresh-initial"
    assert record.authorized_at == NOW
    raw = store.path.read_text(encoding="utf-8")
    assert "access-initial" not in raw
    assert "refresh-initial" in raw
    assert os.stat(store.path).st_mode & 0o077 == 0


def test_refresh_credential_survives_restart_and_refreshes_access_token(tmp_path: Path) -> None:
    first, _, store = _service(tmp_path)
    _authorize(first)
    restarted_transport = _FakeTransport()
    restarted = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=REDIRECT_URI,
        store=store,
        transport=restarted_transport,
    )

    token = restarted.get_access_token(now=NOW)

    assert token == "access-refreshed"
    assert restarted_transport.refresh_calls == [
        {"client_id": "client-id", "refresh_token": "refresh-initial"}
    ]


def test_access_token_cache_avoids_unnecessary_refresh(tmp_path: Path) -> None:
    service, transport, _ = _service(tmp_path)
    _authorize(service)

    assert service.get_access_token(now=NOW) == "access-initial"
    assert transport.refresh_calls == []


def test_rotated_refresh_token_is_persisted_without_resetting_authorization_age(
    tmp_path: Path,
) -> None:
    service, _, store = _service(tmp_path)
    _authorize(service)
    transport = _FakeTransport()
    transport.refresh_response = TokenResponse(
        access_token="access-new",
        expires_in=3600,
        granted_scopes=PRODUCTION_SCOPES,
        refresh_token="refresh-rotated",
    )
    restarted = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=REDIRECT_URI,
        store=store,
        transport=transport,
    )

    assert restarted.get_access_token(now=NOW) == "access-new"

    record = store.load()
    assert record is not None
    assert record.refresh_token == "refresh-rotated"
    assert record.authorized_at == NOW


def test_invalid_grant_discards_secret_and_surfaces_reauthorization(tmp_path: Path) -> None:
    service, _, store = _service(tmp_path)
    _authorize(service)
    transport = _FakeTransport()
    transport.refresh_error = SpotifyTokenEndpointError(
        "Spotify token endpoint rejected the request (HTTP 400)",
        status=400,
        error_code="invalid_grant",
    )
    restarted = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=REDIRECT_URI,
        store=store,
        transport=transport,
    )

    with pytest.raises(SpotifyReauthorizationRequired, match="expired or was revoked"):
        restarted.get_access_token(now=NOW)

    assert restarted.authorization_state() is AuthorizationState.REAUTH_REQUIRED
    record = store.load()
    assert record is not None
    assert record.status is CredentialStatus.REAUTH_REQUIRED
    assert record.refresh_token is None
    assert "refresh-initial" not in store.path.read_text(encoding="utf-8")


def test_invalid_grant_stays_fail_closed_when_reauth_marker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, store = _service(tmp_path)
    _authorize(service)
    transport = _FakeTransport()
    transport.refresh_error = SpotifyTokenEndpointError(
        "Spotify token endpoint rejected the request (HTTP 400)",
        status=400,
        error_code="invalid_grant",
    )
    restarted = SpotifyAuthService(
        client_id="client-id",
        redirect_uri=REDIRECT_URI,
        store=store,
        transport=transport,
    )

    def fail_marker(previous: SpotifyCredentialRecord) -> None:
        del previous
        raise SpotifyCredentialStoreError("simulated marker failure")

    monkeypatch.setattr(store, "mark_reauthorization_required", fail_marker)
    with pytest.raises(SpotifyReauthorizationRequired, match="reauthorization"):
        restarted.get_access_token(now=NOW)

    assert restarted.authorization_state() is AuthorizationState.REAUTH_REQUIRED
    with pytest.raises(SpotifyReauthorizationRequired):
        restarted.get_access_token(now=NOW)
    assert len(transport.refresh_calls) == 1


def test_missing_required_scope_fails_before_persistence(tmp_path: Path) -> None:
    transport = _FakeTransport()
    transport.exchange_response = TokenResponse(
        access_token="access-secret",
        expires_in=3600,
        granted_scopes=("playlist-read-private",),
        refresh_token="refresh-secret",
    )
    service, _, store = _service(tmp_path, transport=transport)
    _, state = _start(service)

    with pytest.raises(SpotifyTokenError, match="required scopes"):
        service.complete_callback(
            urllib.parse.urlencode({"state": state, "code": "code"}),
            now=NOW,
        )

    assert not store.path.exists()


def test_callback_persistence_failure_never_claims_connection_or_leaks_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failing_store = _FailingStore(tmp_path / auth.SPOTIFY_AUTH_FILENAME)
    transport = _FakeTransport()
    transport.exchange_response = TokenResponse(
        access_token="access-token-sentinel",
        expires_in=3600,
        granted_scopes=PRODUCTION_SCOPES,
        refresh_token="refresh-token-sentinel",
    )
    service, _, _ = _service(tmp_path, transport=transport, store=failing_store)
    _, state = _start(service)
    code = "authorization-code-sentinel"
    callback = urllib.parse.urlencode({"state": state, "code": code})

    with pytest.raises(SpotifyCredentialStoreError, match="simulated persistence failure") as raised:
        service.complete_callback(callback, now=NOW)
    assert len(transport.exchange_calls) == 1
    verifier = transport.exchange_calls[0]["verifier"]

    with pytest.raises(SpotifyOAuthCallbackError, match="No Spotify authorization"):
        service.complete_callback(callback, now=NOW)
    assert len(transport.exchange_calls) == 1
    assert service.authorization_state() is AuthorizationState.DISCONNECTED

    captured = capsys.readouterr()
    for secret in (
        code,
        verifier,
        "access-token-sentinel",
        "refresh-token-sentinel",
    ):
        assert secret not in str(raised.value)
        assert secret not in captured.out
        assert secret not in captured.err


def test_atomic_save_failure_preserves_previous_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SpotifyCredentialStore(tmp_path / auth.SPOTIFY_AUTH_FILENAME)
    original = SpotifyCredentialRecord(
        status=CredentialStatus.ACTIVE,
        refresh_token="refresh-original",
        granted_scopes=PRODUCTION_SCOPES,
        authorized_at=NOW,
    )
    store.save(original)

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr(auth.os, "replace", fail_replace)
    with pytest.raises(SpotifyCredentialStoreError):
        store.save(
            SpotifyCredentialRecord(
                status=CredentialStatus.ACTIVE,
                refresh_token="refresh-new",
                granted_scopes=PRODUCTION_SCOPES,
                authorized_at=NOW,
            )
        )

    assert store.load() == original


def test_permissive_credential_file_permissions_fail_closed(tmp_path: Path) -> None:
    store = SpotifyCredentialStore(tmp_path / auth.SPOTIFY_AUTH_FILENAME)
    store.save(
        SpotifyCredentialRecord(
            status=CredentialStatus.ACTIVE,
            refresh_token="refresh-secret",
            granted_scopes=PRODUCTION_SCOPES,
            authorized_at=NOW,
        )
    )
    os.chmod(store.path, 0o644)

    with pytest.raises(SpotifyCredentialStoreError, match="permissions"):
        store.load()


def test_token_errors_do_not_include_token_material() -> None:
    error = SpotifyTokenEndpointError(
        "Spotify token endpoint rejected the request (HTTP 400)",
        status=400,
        error_code="invalid_grant",
    )
    assert "refresh-secret" not in str(error)
    assert "access-secret" not in str(error)


def test_reauthorization_marker_contains_no_refresh_secret(tmp_path: Path) -> None:
    store = SpotifyCredentialStore(tmp_path / auth.SPOTIFY_AUTH_FILENAME)
    record = SpotifyCredentialRecord(
        status=CredentialStatus.ACTIVE,
        refresh_token="refresh-secret",
        granted_scopes=PRODUCTION_SCOPES,
        authorized_at=NOW,
    )
    store.save(record)
    store.mark_reauthorization_required(record)

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["status"] == CredentialStatus.REAUTH_REQUIRED
    assert payload["refresh_token"] is None
    assert "refresh-secret" not in store.path.read_text(encoding="utf-8")
