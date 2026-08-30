"""Minimal long-lived runtime host for container deployments."""

from __future__ import annotations

import base64
import binascii
import hmac
import html
import ipaddress
import json
import os
import secrets
import signal
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import FrameType

from news_bulletin_playlist import __version__
from news_bulletin_playlist.persistence import DEFAULT_DB_FILENAME, SQLiteStore
from news_bulletin_playlist.spotify.auth import (
    SPOTIFY_AUTH_FILENAME,
    AuthorizationState,
    SpotifyAuthConfigurationError,
    SpotifyAuthorizationDenied,
    SpotifyAuthService,
    SpotifyCredentialStore,
    SpotifyCredentialStoreError,
    SpotifyOAuthCallbackError,
    SpotifyTokenError,
    build_redirect_uri,
)

DEFAULT_DATA_DIR = Path("/data")
DEFAULT_HEALTH_HOST = "127.0.0.1"
DEFAULT_HEALTH_PORT = 8080
DEFAULT_HEALTH_URL = f"http://{DEFAULT_HEALTH_HOST}:{DEFAULT_HEALTH_PORT}/healthz"
_ADMIN_PASSWORD_ENV = "NEWS_PLAYLIST_ADMIN_PASSWORD"
_ADMIN_PASSWORD_FILE_ENV = "NEWS_PLAYLIST_ADMIN_PASSWORD_FILE"
_EXTERNAL_URL_ENV = "NEWS_PLAYLIST_EXTERNAL_URL"
_TRUSTED_PROXY_CIDRS_ENV = "NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS"
_SPOTIFY_CLIENT_ID_ENV = "SPOTIFY_CLIENT_ID"
_CSRF_TTL = timedelta(minutes=10)
_MAX_FORM_BYTES = 4096

TrustedProxyNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class AdminSecurity:
    """Protect the small server-rendered administration surface."""

    def __init__(
        self,
        password: str,
        *,
        trusted_proxy_networks: Sequence[TrustedProxyNetwork] = (),
        allow_direct_loopback: bool = True,
    ) -> None:
        if not password.strip():
            raise RuntimeError("administration password must not be blank")
        if len(password) < 16:
            raise RuntimeError("administration password must contain at least 16 characters")
        if "\r" in password or "\n" in password:
            raise RuntimeError("administration password must not contain line breaks")
        self._password = password
        self._trusted_proxy_networks = tuple(trusted_proxy_networks)
        self._allow_direct_loopback = allow_direct_loopback
        self._csrf_token: str | None = None
        self._csrf_expires_at: datetime | None = None

    def is_secure_transport(
        self,
        client_ip: str,
        forwarded_proto_values: Sequence[str] | None,
    ) -> bool:
        """Accept direct loopback or HTTPS asserted by an explicitly trusted proxy."""
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        if self._allow_direct_loopback and address.is_loopback:
            return True
        trusted = any(
            address.version == network.version and address in network
            for network in self._trusted_proxy_networks
        )
        if not trusted or forwarded_proto_values is None or len(forwarded_proto_values) != 1:
            return False
        return forwarded_proto_values[0].strip().lower() == "https"

    def is_authorized(self, header: str | None) -> bool:
        if header is None:
            return False
        scheme, separator, encoded = header.partition(" ")
        if separator != " " or scheme.lower() != "basic" or not encoded:
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        username, separator, password = decoded.partition(":")
        if separator != ":" or username != "admin":
            return False
        return hmac.compare_digest(password, self._password)

    def issue_csrf_token(self, *, now: datetime | None = None) -> str:
        observed_at = _as_utc(now or datetime.now(UTC))
        token = secrets.token_urlsafe(32)
        self._csrf_token = token
        self._csrf_expires_at = observed_at + _CSRF_TTL
        return token

    def consume_csrf_token(self, token: str, *, now: datetime | None = None) -> bool:
        observed_at = _as_utc(now or datetime.now(UTC))
        expected = self._csrf_token
        expires_at = self._csrf_expires_at
        if expected is None or expires_at is None:
            return False
        if observed_at > expires_at or not hmac.compare_digest(token, expected):
            return False
        self._csrf_token = None
        self._csrf_expires_at = None
        return True


def _data_dir_ready(data_dir: Path) -> bool:
    """Return whether the persistent path accepts a real durable write."""
    if not data_dir.is_dir():
        return False
    try:
        with tempfile.NamedTemporaryFile(prefix=".runtime-health-", dir=data_dir) as probe:
            probe.write(b"ok")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError:
        return False
    return True


def ensure_data_dir(data_dir: Path) -> None:
    """Fail early unless the persistent application directory is writable."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"application data directory is not writable: {data_dir}") from exc
    if not _data_dir_ready(data_dir):
        raise RuntimeError(f"application data directory is not writable: {data_dir}")


def initialize_runtime_storage(data_dir: Path) -> Path:
    """Verify `/data` and apply durable SQLite migrations before serving traffic."""
    ensure_data_dir(data_dir)
    database_path = data_dir / DEFAULT_DB_FILENAME
    SQLiteStore(database_path).initialize()
    return database_path


def build_runtime_auth(
    data_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[AdminSecurity | None, SpotifyAuthService | None]:
    """Build optional private administration and Spotify auth from runtime-only settings."""
    env = os.environ if environ is None else environ
    admin_password = _load_admin_password(env)
    client_id = _optional_setting(env.get(_SPOTIFY_CLIENT_ID_ENV))
    external_url = _optional_setting(env.get(_EXTERNAL_URL_ENV))
    trusted_proxy_networks = _parse_trusted_proxy_networks(
        _optional_setting(env.get(_TRUSTED_PROXY_CIDRS_ENV))
    )

    if (client_id is None) != (external_url is None):
        raise RuntimeError(
            f"{_SPOTIFY_CLIENT_ID_ENV} and {_EXTERNAL_URL_ENV} must be configured together"
        )
    if client_id is not None and admin_password is None:
        raise RuntimeError(
            f"{_ADMIN_PASSWORD_ENV} or {_ADMIN_PASSWORD_FILE_ENV} is required when Spotify "
            "Web UI authorization is enabled"
        )
    if admin_password is None and trusted_proxy_networks:
        raise RuntimeError(f"{_TRUSTED_PROXY_CIDRS_ENV} requires administration to be enabled")

    redirect_uri: str | None = None
    external_scheme: str | None = None
    if external_url is not None:
        try:
            redirect_uri = build_redirect_uri(external_url)
        except SpotifyAuthConfigurationError as exc:
            raise RuntimeError(
                f"invalid Spotify Web UI authorization configuration: {exc}"
            ) from exc
        external_scheme = urllib.parse.urlsplit(redirect_uri).scheme

    if admin_password is not None and external_scheme == "https" and not trusted_proxy_networks:
        raise RuntimeError(
            f"{_TRUSTED_PROXY_CIDRS_ENV} is required for HTTPS administration behind a "
            "reverse proxy"
        )

    admin_security = (
        AdminSecurity(
            admin_password,
            trusted_proxy_networks=trusted_proxy_networks,
            allow_direct_loopback=external_scheme != "https",
        )
        if admin_password is not None
        else None
    )
    if client_id is None or redirect_uri is None:
        return admin_security, None

    spotify_auth = SpotifyAuthService(
        client_id=client_id,
        redirect_uri=redirect_uri,
        store=SpotifyCredentialStore(data_dir / SPOTIFY_AUTH_FILENAME),
    )
    return admin_security, spotify_auth


def _status_page(*, ready: bool, spotify_state: AuthorizationState | None) -> bytes:
    """Render the public read-only status portal."""
    status = "Ready" if ready else "Degraded"
    storage = "Writable" if ready else "Unavailable"
    version = html.escape(__version__)
    spotify = _authorization_state_label(spotify_state)
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>News Bulletin Playlists</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 4rem auto;
           padding: 0 1.25rem; line-height: 1.5; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: .5rem 1rem; }}
    dt {{ font-weight: 700; }}
    code {{ font-family: ui-monospace, monospace; }}
  </style>
</head>
<body>
  <h1>News Bulletin Playlists</h1>
  <p>The container runtime is running. This page remains read-only; administrative
     actions are isolated under the authenticated <code>/admin/</code> surface.</p>
  <dl>
    <dt>Runtime</dt><dd>{status}</dd>
    <dt>Persistent storage</dt><dd>{storage}</dd>
    <dt>Spotify authorization</dt><dd>{spotify}</dd>
    <dt>Version</dt><dd><code>{version}</code></dd>
  </dl>
</body>
</html>
"""
    return document.encode("utf-8")


def _admin_page(
    *,
    spotify_state: AuthorizationState | None,
    csrf_token: str,
) -> bytes:
    spotify = html.escape(_authorization_state_label(spotify_state))
    action = (
        "Reconnect Spotify"
        if spotify_state is AuthorizationState.CONNECTED
        else "Connect Spotify"
    )
    disabled = " disabled" if spotify_state is None else ""
    explanation = (
        "Spotify Web UI authorization is not configured for this runtime."
        if spotify_state is None
        else "Authorization uses Spotify Authorization Code + PKCE and stores only the long-lived "
        "refresh credential under /data."
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Administration · News Bulletin Playlists</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 4rem auto;
           padding: 0 1.25rem; line-height: 1.5; }}
    button {{ font: inherit; padding: .55rem .9rem; }}
    code {{ font-family: ui-monospace, monospace; }}
  </style>
</head>
<body>
  <h1>Administration</h1>
  <p>Spotify authorization: <strong>{spotify}</strong></p>
  <p>{html.escape(explanation)}</p>
  <form method="post" action="/admin/spotify/connect">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
    <button type="submit"{disabled}>{html.escape(action)}</button>
  </form>
  <p><a href="/">Return to status</a></p>
</body>
</html>
"""
    return document.encode("utf-8")


def _callback_error_page(message: str) -> bytes:
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Spotify authorization</title>"
        f"<p>{html.escape(message)}</p><p><a href='/admin/'>Return to administration</a></p>"
    ).encode()


class HealthHandler(BaseHTTPRequestHandler):
    """Serve status, health and authenticated private administration without request logs."""

    data_dir = DEFAULT_DATA_DIR
    timeout = 2.0
    admin_security: AdminSecurity | None = None
    spotify_auth: SpotifyAuthService | None = None

    def _send_headers(
        self,
        *,
        content_type: str,
        length: int,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        if extra_headers is not None:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()

    def _reply(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._send_headers(
            content_type=content_type,
            length=len(payload),
            extra_headers=extra_headers,
        )
        if payload:
            self.wfile.write(payload)

    def _spotify_state(self) -> AuthorizationState | None:
        auth_service = self.spotify_auth
        return auth_service.authorization_state() if auth_service is not None else None

    def _require_admin_transport(self) -> AdminSecurity | None:
        security = self.admin_security
        if security is None:
            self._reply(HTTPStatus.NOT_FOUND, b"Not found")
            return None
        forwarded_proto = self.headers.get_all("X-Forwarded-Proto")
        if not security.is_secure_transport(self.client_address[0], forwarded_proto):
            self._reply(
                HTTPStatus.FORBIDDEN,
                b"Administration requires a trusted HTTPS reverse proxy",
                content_type="text/plain; charset=utf-8",
            )
            return None
        return security

    def _require_admin(self) -> AdminSecurity | None:
        security = self._require_admin_transport()
        if security is None:
            return None
        if not security.is_authorized(self.headers.get("Authorization")):
            self._reply(
                HTTPStatus.UNAUTHORIZED,
                b"Administration authentication required",
                content_type="text/plain; charset=utf-8",
                extra_headers={"WWW-Authenticate": 'Basic realm="news-bulletin-playlist-admin"'},
            )
            return None
        return security

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"Invalid request")
            return
        ready = _data_dir_ready(self.data_dir)

        if parsed.path == "/":
            payload = _status_page(ready=ready, spotify_state=self._spotify_state())
            self._reply(
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                payload,
            )
            return

        if parsed.path == "/healthz":
            payload = json.dumps(
                {"status": "ok" if ready else "degraded", "version": __version__},
                separators=(",", ":"),
            ).encode("utf-8")
            self._reply(
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                payload,
                content_type="application/json",
            )
            return

        if parsed.path == "/admin/":
            security = self._require_admin()
            if security is None:
                return
            csrf_token = security.issue_csrf_token()
            payload = _admin_page(
                spotify_state=self._spotify_state(),
                csrf_token=csrf_token,
            )
            self._reply(HTTPStatus.OK, payload)
            return

        if parsed.path == "/admin/spotify/callback":
            if self._require_admin_transport() is None:
                return
            self._handle_spotify_callback(parsed.query)
            return

        self._reply(HTTPStatus.NOT_FOUND, b"Not found", content_type="text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"Invalid request")
            return
        if parsed.path != "/admin/spotify/connect":
            self._reply(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                content_type="text/plain; charset=utf-8",
            )
            return

        security = self._require_admin()
        if security is None:
            return
        auth_service = self.spotify_auth
        if auth_service is None:
            self._reply(
                HTTPStatus.SERVICE_UNAVAILABLE,
                b"Spotify Web UI authorization is not configured",
                content_type="text/plain; charset=utf-8",
            )
            return
        try:
            form = self._read_form()
        except ValueError as exc:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                str(exc).encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
            return
        csrf_values = form.get("csrf_token", [])
        if len(csrf_values) != 1 or not security.consume_csrf_token(csrf_values[0]):
            self._reply(
                HTTPStatus.FORBIDDEN,
                b"Administration form validation failed",
                content_type="text/plain; charset=utf-8",
            )
            return

        authorize_url = auth_service.start_authorization()
        self._reply(
            HTTPStatus.SEE_OTHER,
            b"",
            extra_headers={"Location": authorize_url},
        )

    def _read_form(self) -> dict[str, list[str]]:
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise ValueError("Expected an application/x-www-form-urlencoded form")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ValueError("Invalid form length") from exc
        if length <= 0 or length > _MAX_FORM_BYTES:
            raise ValueError("Invalid form length")
        payload = self.rfile.read(length)
        try:
            decoded = payload.decode("utf-8")
            return urllib.parse.parse_qs(
                decoded,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Malformed administration form") from exc

    def _handle_spotify_callback(self, query: str) -> None:
        auth_service = self.spotify_auth
        if auth_service is None:
            self._reply(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                content_type="text/plain; charset=utf-8",
            )
            return
        try:
            auth_service.complete_callback(query)
        except SpotifyAuthorizationDenied:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                _callback_error_page("Spotify authorization was not granted."),
            )
            return
        except SpotifyOAuthCallbackError:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                _callback_error_page("Spotify authorization callback was invalid or expired."),
            )
            return
        except SpotifyCredentialStoreError:
            self._reply(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                _callback_error_page("Spotify authorization could not be stored safely."),
            )
            return
        except SpotifyTokenError:
            self._reply(
                HTTPStatus.BAD_GATEWAY,
                _callback_error_page("Spotify authorization could not be completed."),
            )
            return
        self._reply(
            HTTPStatus.SEE_OTHER,
            b"",
            extra_headers={"Location": "/admin/"},
        )

    def version_string(self) -> str:
        """Avoid disclosing the Python runtime version in HTTP headers."""
        return "news-bulletin-playlist"

    def log_message(self, format: str, *args: object) -> None:
        """Suppress request logging, including OAuth callback query parameters."""
        return


def serve(
    *,
    host: str = DEFAULT_HEALTH_HOST,
    port: int = DEFAULT_HEALTH_PORT,
    data_dir: Path = DEFAULT_DATA_DIR,
    stop_event: threading.Event | None = None,
) -> int:
    """Run the durable container host until SIGTERM/SIGINT requests shutdown."""
    database_path = initialize_runtime_storage(data_dir)
    admin_security, spotify_auth = build_runtime_auth(data_dir)
    HealthHandler.data_dir = data_dir
    HealthHandler.admin_security = admin_security
    HealthHandler.spotify_auth = spotify_auth
    server = HTTPServer((host, port), HealthHandler)
    server.timeout = 0.5
    stopped = stop_event if stop_event is not None else threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stopped.set()

    handlers_installed = (
        stop_event is None and threading.current_thread() is threading.main_thread()
    )
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    if handlers_installed:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    auth_status = "enabled" if spotify_auth is not None else "disabled"
    print(
        f"news-bulletin-playlist runtime ready; web=http://{host}:{server.server_port}/ "
        f"health=http://127.0.0.1:{server.server_port}/healthz data={data_dir} "
        f"db={database_path} spotify_auth={auth_status}",
        flush=True,
    )
    try:
        while not stopped.is_set():
            server.handle_request()
    finally:
        server.server_close()
        if handlers_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
    return 0


def healthcheck(*, url: str = DEFAULT_HEALTH_URL, timeout: float = 2.0) -> int:
    """Return a Docker-healthcheck-compatible status code."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != HTTPStatus.OK:
                return 1
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return 1
    return 0 if isinstance(payload, dict) and payload.get("status") == "ok" else 1


def _load_admin_password(environ: Mapping[str, str]) -> str | None:
    direct = _optional_setting(environ.get(_ADMIN_PASSWORD_ENV), strip=False)
    file_setting = _optional_setting(environ.get(_ADMIN_PASSWORD_FILE_ENV))
    if direct is not None and file_setting is not None:
        raise RuntimeError(
            f"configure only one of {_ADMIN_PASSWORD_ENV} or {_ADMIN_PASSWORD_FILE_ENV}"
        )
    if file_setting is None:
        return direct

    path = Path(file_setting)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("administration password file cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("administration password file must be a regular file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("administration password file must not grant group/other access")
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("administration password file cannot be read") from exc
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    return value or None


def _parse_trusted_proxy_networks(
    value: str | None,
) -> tuple[TrustedProxyNetwork, ...]:
    """Parse explicit reverse-proxy IP/CIDR entries used to trust HTTPS forwarding."""
    if value is None:
        return ()
    entries = [entry.strip() for entry in value.split(",")]
    if not entries or any(not entry for entry in entries):
        raise RuntimeError(f"{_TRUSTED_PROXY_CIDRS_ENV} contains an empty entry")
    networks: list[TrustedProxyNetwork] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise RuntimeError(
                f"{_TRUSTED_PROXY_CIDRS_ENV} contains an invalid IP/CIDR"
            ) from exc
    return tuple(networks)


def _optional_setting(value: str | None, *, strip: bool = True) -> str | None:
    if value is None:
        return None
    candidate = value.strip() if strip else value
    return candidate if candidate else None


def _authorization_state_label(state: AuthorizationState | None) -> str:
    if state is None:
        return "Not configured"
    return {
        AuthorizationState.DISCONNECTED: "Not connected",
        AuthorizationState.CONNECTED: "Connected",
        AuthorizationState.REAUTH_REQUIRED: "Reauthorization required",
        AuthorizationState.ERROR: "Authorization state error",
    }[state]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)