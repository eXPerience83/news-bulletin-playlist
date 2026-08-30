from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_AUTH_FILENAME = "spotify-auth.json"
SPOTIFY_CALLBACK_PATH = "/admin/spotify/callback"
PRODUCTION_SCOPES = (
    "playlist-read-private",
    "playlist-modify-private",
    "playlist-modify-public",
)
_AUTHORIZATION_TTL = timedelta(minutes=10)
_ACCESS_TOKEN_REFRESH_SKEW = timedelta(seconds=60)


class SpotifyAuthError(RuntimeError):
    """Base class for safe production Spotify authorization failures."""


class SpotifyAuthConfigurationError(SpotifyAuthError):
    """Runtime Spotify authorization configuration is invalid."""


class SpotifyOAuthCallbackError(SpotifyAuthError):
    """An OAuth callback failed validation."""


class SpotifyAuthorizationDenied(SpotifyOAuthCallbackError):
    """The user denied the Spotify authorization request."""


class SpotifyTokenError(SpotifyAuthError):
    """Spotify token exchange or refresh failed safely."""


class SpotifyTokenEndpointError(SpotifyTokenError):
    def __init__(self, message: str, *, status: int, error_code: str | None) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class SpotifyReauthorizationRequired(SpotifyAuthError):
    """Durable Spotify authorization is absent, expired or revoked."""


class SpotifyCredentialStoreError(SpotifyAuthError):
    """Spotify credential persistence could not be read or updated safely."""


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    REAUTH_REQUIRED = "reauth_required"


class AuthorizationState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    REAUTH_REQUIRED = "reauth_required"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    expires_in: int
    granted_scopes: tuple[str, ...]
    refresh_token: str | None


@dataclass(frozen=True, slots=True)
class SpotifyCredentialRecord:
    status: CredentialStatus
    refresh_token: str | None
    granted_scopes: tuple[str, ...]
    authorized_at: datetime


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    state: str
    verifier: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _CachedAccessToken:
    value: str
    expires_at: datetime


class SpotifyTokenTransport(Protocol):
    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> TokenResponse: ...

    def refresh_token(self, *, client_id: str, refresh_token: str) -> TokenResponse: ...


class SpotifyAccountsClient:
    """Minimal PKCE token endpoint client with secret-safe errors."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def exchange_code(
        self,
        *,
        client_id: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> TokenResponse:
        return self._post_token(
            {
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )

    def refresh_token(self, *, client_id: str, refresh_token: str) -> TokenResponse:
        return self._post_token(
            {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    def _post_token(self, fields: dict[str, str]) -> TokenResponse:
        body = urllib.parse.urlencode(fields).encode("ascii")
        request = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = bytes(response.read())
        except urllib.error.HTTPError as exc:
            error_code = _oauth_error_code(_read_http_error_body(exc))
            raise SpotifyTokenEndpointError(
                f"Spotify token endpoint rejected the request (HTTP {exc.code})",
                status=exc.code,
                error_code=error_code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SpotifyTokenError("Spotify token endpoint is temporarily unreachable") from exc

        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SpotifyTokenError("Spotify token endpoint returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise SpotifyTokenError("Spotify token endpoint returned an unexpected response")
        return _validate_token_response(cast(dict[str, Any], decoded))


class SpotifyCredentialStore:
    """Atomic, owner-only persistence for the long-lived Spotify refresh credential."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SpotifyCredentialRecord | None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SpotifyCredentialStoreError(
                "Spotify authorization state cannot be inspected"
            ) from exc

        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SpotifyCredentialStoreError("Spotify authorization state is not a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SpotifyCredentialStoreError(
                "Spotify authorization state permissions must not grant group/other access"
            )

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SpotifyCredentialStoreError("Spotify authorization state is unreadable") from exc
        return _parse_credential_record(payload)

    def save(self, record: SpotifyCredentialRecord) -> None:
        payload = {
            "version": 1,
            "status": record.status.value,
            "refresh_token": record.refresh_token,
            "granted_scopes": list(record.granted_scopes),
            "authorized_at": _as_utc(record.authorized_at).isoformat(),
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SpotifyCredentialStoreError(
                "Spotify authorization directory cannot be created"
            ) from exc

        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        fd: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            raise SpotifyCredentialStoreError(
                "Spotify authorization state could not be saved"
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def mark_reauthorization_required(self, previous: SpotifyCredentialRecord) -> None:
        self.save(
            SpotifyCredentialRecord(
                status=CredentialStatus.REAUTH_REQUIRED,
                refresh_token=None,
                granted_scopes=previous.granted_scopes,
                authorized_at=previous.authorized_at,
            )
        )


class SpotifyAuthService:
    """Own one production PKCE flow and the refresh-token lifecycle."""

    def __init__(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        store: SpotifyCredentialStore,
        transport: SpotifyTokenTransport | None = None,
        scopes: Sequence[str] = PRODUCTION_SCOPES,
    ) -> None:
        if not client_id.strip():
            raise SpotifyAuthConfigurationError("Spotify client ID is required")
        self.client_id = client_id.strip()
        self.redirect_uri = validate_redirect_uri(redirect_uri)
        self.store = store
        self.transport = transport if transport is not None else SpotifyAccountsClient()
        self.scopes = tuple(scopes)
        if not self.scopes or any(not scope.strip() for scope in self.scopes):
            raise SpotifyAuthConfigurationError("Spotify authorization scopes are invalid")
        self._pending: PendingAuthorization | None = None
        self._access_token: _CachedAccessToken | None = None

    def authorization_state(self) -> AuthorizationState:
        try:
            record = self.store.load()
        except SpotifyCredentialStoreError:
            return AuthorizationState.ERROR
        if record is None:
            return AuthorizationState.DISCONNECTED
        if record.status is CredentialStatus.REAUTH_REQUIRED:
            return AuthorizationState.REAUTH_REQUIRED
        return AuthorizationState.CONNECTED

    def start_authorization(self, *, now: datetime | None = None) -> str:
        observed_at = _as_utc(now or datetime.now(UTC))
        verifier = create_code_verifier()
        state = secrets.token_urlsafe(32)
        self._pending = PendingAuthorization(
            state=state,
            verifier=verifier,
            expires_at=observed_at + _AUTHORIZATION_TTL,
        )
        challenge = create_code_challenge(verifier)
        return build_authorize_url(
            self.client_id,
            redirect_uri=self.redirect_uri,
            state=state,
            challenge=challenge,
            scopes=self.scopes,
        )

    def complete_callback(self, query: str, *, now: datetime | None = None) -> None:
        observed_at = _as_utc(now or datetime.now(UTC))
        pending = self._pending
        if pending is None:
            raise SpotifyOAuthCallbackError("No Spotify authorization request is pending")
        if observed_at > pending.expires_at:
            self._pending = None
            raise SpotifyOAuthCallbackError("Spotify authorization request expired")

        params = _parse_callback_query(query)
        callback_state = _single_parameter(params, "state", required=True)
        if callback_state is None or not hmac.compare_digest(callback_state, pending.state):
            raise SpotifyOAuthCallbackError("Spotify OAuth state validation failed")

        code = _single_parameter(params, "code", required=False)
        error = _single_parameter(params, "error", required=False)
        self._pending = None
        if (code is None) == (error is None):
            raise SpotifyOAuthCallbackError(
                "Spotify callback must contain exactly one of code or error"
            )
        if error is not None:
            raise SpotifyAuthorizationDenied("Spotify authorization was not granted")
        if code is None:
            raise SpotifyOAuthCallbackError(
                "Spotify callback did not contain an authorization code"
            )

        token = self.transport.exchange_code(
            client_id=self.client_id,
            code=code,
            redirect_uri=self.redirect_uri,
            verifier=pending.verifier,
        )
        _require_scopes(token.granted_scopes, self.scopes)
        if token.refresh_token is None or not token.refresh_token.strip():
            raise SpotifyTokenError("Spotify did not return a refresh token")

        record = SpotifyCredentialRecord(
            status=CredentialStatus.ACTIVE,
            refresh_token=token.refresh_token,
            granted_scopes=token.granted_scopes,
            authorized_at=observed_at,
        )
        self.store.save(record)
        self._access_token = _CachedAccessToken(
            token.access_token,
            observed_at + timedelta(seconds=token.expires_in),
        )

    def get_access_token(self, *, now: datetime | None = None) -> str:
        observed_at = _as_utc(now or datetime.now(UTC))
        cached = self._access_token
        if cached is not None and cached.expires_at > observed_at + _ACCESS_TOKEN_REFRESH_SKEW:
            return cached.value

        record = self.store.load()
        if record is None or record.status is CredentialStatus.REAUTH_REQUIRED:
            self._access_token = None
            raise SpotifyReauthorizationRequired("Spotify authorization is required")
        refresh_token = record.refresh_token
        if refresh_token is None:
            self._access_token = None
            raise SpotifyReauthorizationRequired("Spotify authorization is required")

        try:
            token = self.transport.refresh_token(
                client_id=self.client_id,
                refresh_token=refresh_token,
            )
        except SpotifyTokenEndpointError as exc:
            if exc.error_code == "invalid_grant":
                self._access_token = None
                self.store.mark_reauthorization_required(record)
                raise SpotifyReauthorizationRequired(
                    "Spotify authorization expired or was revoked"
                ) from exc
            raise

        _require_scopes(token.granted_scopes, self.scopes)
        if token.refresh_token is not None and token.refresh_token != refresh_token:
            self.store.save(
                SpotifyCredentialRecord(
                    status=CredentialStatus.ACTIVE,
                    refresh_token=token.refresh_token,
                    granted_scopes=token.granted_scopes,
                    authorized_at=record.authorized_at,
                )
            )
        self._access_token = _CachedAccessToken(
            token.access_token,
            observed_at + timedelta(seconds=token.expires_in),
        )
        return token.access_token


def build_redirect_uri(external_url: str) -> str:
    value = external_url.strip()
    if not value:
        raise SpotifyAuthConfigurationError("External application URL is required")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SpotifyAuthConfigurationError("External application URL is malformed") from exc
    del port
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise SpotifyAuthConfigurationError(
            "External application URL must be an origin without path, query or credentials"
        )
    allowed_scheme = parsed.scheme == "https" or (
        parsed.scheme == "http" and _is_loopback_hostname(parsed.hostname)
    )
    if not allowed_scheme:
        raise SpotifyAuthConfigurationError(
            "External application URL must use HTTPS except for an explicit loopback IP"
        )
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return f"{origin}{SPOTIFY_CALLBACK_PATH}"


def validate_redirect_uri(redirect_uri: str) -> str:
    expected = build_redirect_uri(
        redirect_uri[: -len(SPOTIFY_CALLBACK_PATH)]
        if redirect_uri.endswith(SPOTIFY_CALLBACK_PATH)
        else ""
    )
    if expected != redirect_uri:
        raise SpotifyAuthConfigurationError(
            f"Spotify redirect URI must end with {SPOTIFY_CALLBACK_PATH}"
        )
    return redirect_uri


def create_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def create_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(
    client_id: str,
    *,
    redirect_uri: str,
    state: str,
    challenge: str,
    scopes: Sequence[str],
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _validate_token_response(payload: dict[str, Any]) -> TokenResponse:
    access_token = payload.get("access_token")
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    scope = payload.get("scope")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise SpotifyTokenError("Spotify token response did not contain a valid access token")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise SpotifyTokenError("Spotify token response did not contain a Bearer token type")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise SpotifyTokenError("Spotify token response did not contain a valid expiry")
    if not isinstance(scope, str):
        raise SpotifyTokenError("Spotify token response did not contain granted scopes")
    if refresh_token is not None and (
        not isinstance(refresh_token, str) or not refresh_token.strip()
    ):
        raise SpotifyTokenError("Spotify token response contained an invalid refresh token")
    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        granted_scopes=tuple(scope.split()),
        refresh_token=refresh_token,
    )


def _require_scopes(granted_scopes: Sequence[str], required_scopes: Sequence[str]) -> None:
    granted = set(granted_scopes)
    missing = [scope for scope in required_scopes if scope not in granted]
    if missing:
        raise SpotifyTokenError(
            "Spotify did not grant all required scopes: " + ", ".join(missing)
        )


def _parse_callback_query(query: str) -> dict[str, list[str]]:
    try:
        return urllib.parse.parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise SpotifyOAuthCallbackError("Spotify callback query is malformed") from exc


def _single_parameter(
    params: dict[str, list[str]], name: str, *, required: bool
) -> str | None:
    values = params.get(name, [])
    if len(values) > 1:
        raise SpotifyOAuthCallbackError(
            f"Spotify callback contained duplicate {name} parameters"
        )
    if not values:
        if required:
            raise SpotifyOAuthCallbackError(f"Spotify callback did not contain {name}")
        return None
    if not values[0]:
        raise SpotifyOAuthCallbackError(f"Spotify callback contained an empty {name} parameter")
    return values[0]


def _parse_credential_record(payload: object) -> SpotifyCredentialRecord:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise SpotifyCredentialStoreError("Spotify authorization state has an unknown format")
    try:
        status = CredentialStatus(payload.get("status"))
    except ValueError as exc:
        raise SpotifyCredentialStoreError(
            "Spotify authorization state has an invalid status"
        ) from exc
    refresh_token = payload.get("refresh_token")
    raw_scopes = payload.get("granted_scopes")
    raw_authorized_at = payload.get("authorized_at")
    if not isinstance(raw_scopes, list) or not raw_scopes or not all(
        isinstance(scope, str) and scope for scope in raw_scopes
    ):
        raise SpotifyCredentialStoreError("Spotify authorization state has invalid scopes")
    if not isinstance(raw_authorized_at, str):
        raise SpotifyCredentialStoreError("Spotify authorization state has an invalid timestamp")
    try:
        authorized_at = _as_utc(datetime.fromisoformat(raw_authorized_at))
    except ValueError as exc:
        raise SpotifyCredentialStoreError(
            "Spotify authorization state has an invalid timestamp"
        ) from exc
    if status is CredentialStatus.ACTIVE:
        if not isinstance(refresh_token, str) or not refresh_token:
            raise SpotifyCredentialStoreError(
                "Active Spotify authorization state is missing its refresh credential"
            )
    elif refresh_token is not None:
        raise SpotifyCredentialStoreError(
            "Reauthorization-required state must not retain a refresh credential"
        )
    return SpotifyCredentialRecord(
        status=status,
        refresh_token=cast(str | None, refresh_token),
        granted_scopes=tuple(cast(list[str], raw_scopes)),
        authorized_at=authorized_at,
    )


def _oauth_error_code(payload: bytes) -> str | None:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    value = decoded.get("error")
    return value if isinstance(value, str) and value else None


def _read_http_error_body(error: urllib.error.HTTPError) -> bytes:
    try:
        return bytes(error.read())
    except OSError:
        return b""


def _is_loopback_hostname(hostname: str) -> bool:
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
