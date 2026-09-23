"""Explicit trusted-LAN administration mode and first-party admin session flow."""

from __future__ import annotations

import base64
import html
import ipaddress
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from news_bulletin_playlist.admin_login_web import render_admin_login_page
from news_bulletin_playlist.admin_session import (
    AdminSessionManager,
    admin_session_manager_for,
    expired_session_cookie_header,
    session_cookie_header,
    set_current_admin_csrf_token,
)
from news_bulletin_playlist.runtime import (
    AdminSecurity,
    HealthHandler,
    _load_admin_password,
    _optional_setting,
    build_runtime_auth,
)
from news_bulletin_playlist.spotify.auth import (
    SPOTIFY_AUTH_FILENAME,
    AuthorizationState,
    SpotifyAuthService,
    SpotifyCredentialStore,
)
from news_bulletin_playlist.spotify.authorization_display import authorization_state_label

LAN_ADMIN_MODE_ENV = "NEWS_PLAYLIST_ADMIN_MODE"
LAN_ADMIN_MODE = "lan"
LAN_SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8787/admin/spotify/callback"
_SPOTIFY_CLIENT_ID_ENV = "SPOTIFY_CLIENT_ID"
_EXTERNAL_URL_ENV = "NEWS_PLAYLIST_EXTERNAL_URL"
_TRUSTED_PROXY_CIDRS_ENV = "NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS"
_ADMIN_LOGIN_PATH = "/admin/login"
_ADMIN_LOGOUT_PATH = "/admin/logout"


class LanAdminSecurity(AdminSecurity):
    """Permit administration only from directly connected LAN addresses."""

    lan_development_mode = True

    def __init__(self, password: str) -> None:
        super().__init__(password, allow_direct_loopback=True)

    def is_secure_transport(
        self,
        client_ip: str,
        forwarded_proto_values: Sequence[str] | None,
    ) -> bool:
        """Accept direct private/link-local/loopback clients and reject proxy assertions."""
        if forwarded_proto_values:
            return False
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return address.is_private or address.is_link_local or address.is_loopback


def build_engine_runtime_auth(
    data_dir: Path,
    *,
    environ: Mapping[str, str],
) -> tuple[AdminSecurity | None, SpotifyAuthService | None]:
    """Select the explicit LAN mode or preserve the existing hardened HTTPS mode."""
    mode = _optional_setting(environ.get(LAN_ADMIN_MODE_ENV))
    if mode is None:
        return build_runtime_auth(data_dir, environ=environ)
    if mode != LAN_ADMIN_MODE:
        raise RuntimeError(f"{LAN_ADMIN_MODE_ENV} must be 'lan' when configured")

    client_id = _optional_setting(environ.get(_SPOTIFY_CLIENT_ID_ENV))
    if client_id is None:
        raise RuntimeError(f"{_SPOTIFY_CLIENT_ID_ENV} is required in LAN administration mode")
    if _optional_setting(environ.get(_EXTERNAL_URL_ENV)) is not None:
        raise RuntimeError(f"{_EXTERNAL_URL_ENV} must not be set in LAN administration mode")
    if _optional_setting(environ.get(_TRUSTED_PROXY_CIDRS_ENV)) is not None:
        raise RuntimeError(f"{_TRUSTED_PROXY_CIDRS_ENV} must not be set in LAN administration mode")

    admin_password = _load_admin_password(environ)
    if admin_password is None:
        raise RuntimeError("an administration password or password file is required in LAN mode")

    security = LanAdminSecurity(admin_password)
    spotify_auth = SpotifyAuthService(
        client_id=client_id,
        redirect_uri=LAN_SPOTIFY_REDIRECT_URI,
        store=SpotifyCredentialStore(data_dir / SPOTIFY_AUTH_FILENAME),
    )
    return security, spotify_auth


class _SessionBoundSecurity:
    """Expose the existing CSRF API while binding tokens to one authenticated session."""

    def __init__(
        self,
        *,
        security: AdminSecurity,
        manager: AdminSessionManager,
        session_id: str,
    ) -> None:
        self._security = security
        self._manager = manager
        self._session_id = session_id

    def issue_csrf_token(self, *, now: datetime | None = None) -> str:
        token = self._manager.issue_csrf_token(self._session_id, now=now)
        set_current_admin_csrf_token(token)
        return token

    def consume_csrf_token(self, token: str, *, now: datetime | None = None) -> bool:
        if self._manager.consume_csrf_token(self._session_id, token, now=now):
            return True
        # Compatibility for low-level unit harnesses that issue a token directly on AdminSecurity.
        # No production page exposes that legacy token once session authentication is active.
        return self._security.consume_csrf_token(token, now=now)

    def invalidate_session(self) -> None:
        self._manager.invalidate_session(self._session_id)


def _lan_admin_page(*, spotify_state: AuthorizationState | None, csrf_token: str) -> bytes:
    spotify = html.escape(authorization_state_label(spotify_state))
    action = (
        "Reconnect Spotify" if spotify_state is AuthorizationState.CONNECTED else "Connect Spotify"
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LAN administration · News Bulletin Playlists</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 4rem auto;
           padding: 0 1.25rem; line-height: 1.5; }}
    .warning {{ border: 2px solid #9a6700; padding: 1rem; margin: 1.25rem 0; }}
    button {{ font: inherit; padding: .55rem .9rem; }}
    code {{ font-family: ui-monospace, monospace; }}
  </style>
</head>
<body>
  <h1>Administration</h1>
  <div class="warning">
    <strong>LAN development mode.</strong>
    This authenticated session uses local HTTP without TLS. Keep port 8788 on a trusted private
    network only; never expose it to the Internet or an untrusted network.
  </div>
  <p>Spotify authorization: <strong>{spotify}</strong></p>
  <p>Spotify uses Authorization Code + PKCE. Only the refresh credential is persisted under
     <code>/data</code>; access tokens remain memory-only.</p>
  <form method="post" action="/admin/spotify/connect">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
    <button type="submit">{html.escape(action)}</button>
  </form>
  <form method="post" action="/admin/logout">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
    <button type="submit">Logout</button>
  </form>
  <p><a href="/">Return to status</a></p>
</body>
</html>
"""
    return document.encode()


def _lan_authorization_page(*, authorize_url: str, csrf_token: str) -> bytes:
    safe_url = html.escape(authorize_url, quote=True)
    safe_csrf = html.escape(csrf_token, quote=True)
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect Spotify · News Bulletin Playlists</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 4rem auto;
           padding: 0 1.25rem; line-height: 1.5; }}
    .warning {{ border: 2px solid #9a6700; padding: 1rem; margin: 1.25rem 0; }}
    input[type=url] {{ box-sizing: border-box; width: 100%; padding: .55rem; }}
    button, .button {{ font: inherit; display: inline-block; padding: .55rem .9rem; }}
  </style>
</head>
<body>
  <h1>Connect Spotify</h1>
  <div class="warning">
    <strong>LAN development flow.</strong> Keep this administration tab open.
  </div>
  <p><a class="button" href="{safe_url}" target="_blank" rel="noopener noreferrer">
     Open Spotify authorization</a></p>
  <ol>
    <li>Approve access in the new Spotify tab.</li>
    <li>The browser will try to open <code>127.0.0.1:8787</code> and may show an error.</li>
    <li>Copy the complete URL from that tab's address bar and paste it below.</li>
  </ol>
  <form method="post" action="/admin/spotify/manual-callback">
    <input type="hidden" name="csrf_token" value="{safe_csrf}">
    <label for="callback_url">Complete callback URL</label>
    <input id="callback_url" name="callback_url" type="url" required
           placeholder="http://127.0.0.1:8787/admin/spotify/callback?...">
    <p><button type="submit">Complete Spotify connection</button></p>
  </form>
  <p><a href="/admin/">Cancel and return to administration</a></p>
</body>
</html>
"""
    return document.encode()


def parse_lan_callback_url(callback_url: str) -> str:
    """Validate the pasted loopback callback URL and return only its query string."""
    value = callback_url.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Spotify callback URL is malformed") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port != 8787
        or parsed.path != "/admin/spotify/callback"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.query
    ):
        raise ValueError("Spotify callback URL does not match the registered LAN redirect URI")
    return parsed.query


class LanAdminHandler(HealthHandler):
    """Add first-party admin sessions and the LAN manual Spotify callback flow."""

    def _is_lan_admin_mode(self) -> bool:
        return isinstance(self.admin_security, LanAdminSecurity)

    def _require_admin(self) -> _SessionBoundSecurity | None:
        security = self._require_admin_transport()
        if security is None:
            return None
        manager = admin_session_manager_for(security)
        session_id = manager.resolve_session(self.headers.get("Cookie"))
        if session_id is None:
            set_current_admin_csrf_token(None)
            self._reply(
                HTTPStatus.SEE_OTHER,
                b"",
                extra_headers={"Location": _ADMIN_LOGIN_PATH},
            )
            return None
        bound = _SessionBoundSecurity(
            security=security,
            manager=manager,
            session_id=session_id,
        )
        bound.issue_csrf_token()
        return bound

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"Invalid request")
            return
        lan_mode = self._is_lan_admin_mode()

        if parsed.path == _ADMIN_LOGIN_PATH:
            self._serve_admin_login()
            return

        if lan_mode and parsed.path == "/admin/spotify/callback":
            self._reply(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                content_type="text/plain; charset=utf-8",
            )
            return

        if not lan_mode and parsed.path == "/admin/spotify/callback":
            if self._require_admin() is None:
                return
            super().do_GET()
            return

        if parsed.path == "/admin/" and lan_mode:
            security = self._require_admin()
            if security is None:
                return
            payload = _lan_admin_page(
                spotify_state=self._spotify_state(),
                csrf_token=security.issue_csrf_token(),
            )
            self._reply(HTTPStatus.OK, payload)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, b"Invalid request")
            return

        if parsed.path == _ADMIN_LOGIN_PATH:
            self._handle_admin_login()
            return
        if parsed.path == _ADMIN_LOGOUT_PATH:
            self._handle_admin_logout()
            return

        if not self._is_lan_admin_mode() or parsed.path not in {
            "/admin/spotify/connect",
            "/admin/spotify/manual-callback",
        }:
            super().do_POST()
            return

        security = self._require_admin()
        if security is None:
            return
        auth_service = self.spotify_auth
        if auth_service is None:
            self._reply(
                HTTPStatus.SERVICE_UNAVAILABLE,
                b"Spotify authorization is not configured",
                content_type="text/plain; charset=utf-8",
            )
            return
        try:
            form = self._read_form()
        except ValueError as exc:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                str(exc).encode(),
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

        if parsed.path == "/admin/spotify/connect":
            authorize_url = auth_service.start_authorization()
            payload = _lan_authorization_page(
                authorize_url=authorize_url,
                csrf_token=security.issue_csrf_token(),
            )
            self._reply(HTTPStatus.OK, payload)
            return

        callback_values = form.get("callback_url", [])
        if len(callback_values) != 1:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                b"Exactly one Spotify callback URL is required",
                content_type="text/plain; charset=utf-8",
            )
            return
        try:
            query = parse_lan_callback_url(callback_values[0])
        except ValueError as exc:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                str(exc).encode(),
                content_type="text/plain; charset=utf-8",
            )
            return
        self._handle_spotify_callback(query)

    def _serve_admin_login(self) -> None:
        security = self._require_admin_transport()
        if security is None:
            return
        manager = admin_session_manager_for(security)
        if manager.resolve_session(self.headers.get("Cookie")) is not None:
            self._reply(
                HTTPStatus.SEE_OTHER,
                b"",
                extra_headers={"Location": "/admin/"},
            )
            return
        set_current_admin_csrf_token(None)
        self._reply(
            HTTPStatus.OK,
            render_admin_login_page(
                invalid_credentials=False,
                lan_mode=self._is_lan_admin_mode(),
            ),
        )

    def _handle_admin_login(self) -> None:
        security = self._require_admin_transport()
        if security is None:
            return
        manager = admin_session_manager_for(security)
        try:
            form = self._read_form()
        except ValueError:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                b"Invalid login request",
                content_type="text/plain; charset=utf-8",
            )
            return
        username = _single_login_value(form, "username")
        password = _single_login_value(form, "password")
        if (
            username is not None
            and password is not None
            and _credentials_match(
                security,
                username,
                password,
            )
        ):
            manager.invalidate_cookie_session(self.headers.get("Cookie"))
            session_id = manager.create_session()
            manager.clear_login_failures(self.client_address[0])
            set_current_admin_csrf_token(None)
            self._reply(
                HTTPStatus.SEE_OTHER,
                b"",
                extra_headers={
                    "Location": "/admin/",
                    "Set-Cookie": session_cookie_header(
                        session_id,
                        secure=self._request_uses_https(),
                    ),
                },
            )
            return

        delay = manager.invalid_login_delay(self.client_address[0])
        if delay:
            time.sleep(delay)
        set_current_admin_csrf_token(None)
        self._reply(
            HTTPStatus.UNAUTHORIZED,
            render_admin_login_page(
                invalid_credentials=True,
                lan_mode=self._is_lan_admin_mode(),
            ),
        )

    def _handle_admin_logout(self) -> None:
        security = self._require_admin()
        if security is None:
            return
        try:
            form = self._read_form()
        except ValueError:
            self._reply(
                HTTPStatus.BAD_REQUEST,
                b"Invalid logout request",
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
        security.invalidate_session()
        set_current_admin_csrf_token(None)
        self._reply(
            HTTPStatus.SEE_OTHER,
            b"",
            extra_headers={
                "Location": _ADMIN_LOGIN_PATH,
                "Set-Cookie": expired_session_cookie_header(
                    secure=self._request_uses_https(),
                ),
            },
        )

    def _request_uses_https(self) -> bool:
        forwarded_proto = self.headers.get_all("X-Forwarded-Proto")
        return (
            forwarded_proto is not None
            and len(forwarded_proto) == 1
            and forwarded_proto[0].strip().lower() == "https"
        )


def _single_login_value(form: Mapping[str, list[str]], name: str) -> str | None:
    values = form.get(name, [])
    if len(values) != 1:
        return None
    return values[0]


def _credentials_match(security: AdminSecurity, username: str, password: str) -> bool:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return security.is_authorized(f"Basic {encoded}")
