from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

from news_bulletin_playlist.spotify.client import SpotifyClient
from news_bulletin_playlist.spotify.probe import run_catalog_probe, run_write_probe

REDIRECT_URI = "http://127.0.0.1:8787/callback"
_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_LOCAL_CALLBACK_TIMEOUT_SECONDS = 180.0


class OAuthCallbackError(ValueError):
    """A safe, user-facing OAuth callback validation error."""


class OAuthAuthorizationDenied(OAuthCallbackError):
    """Spotify returned an OAuth authorization error for the expected state."""


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    expires_in: int
    granted_scopes: tuple[str, ...]


def scopes_for_mode(write: bool) -> tuple[str, ...]:
    read_scopes = ("user-read-playback-position", "user-read-private")
    if not write:
        return read_scopes
    return (*read_scopes, "playlist-modify-private", "playlist-read-private")


def create_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def create_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(
    client_id: str, *, state: str, challenge: str, scopes: tuple[str, ...]
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    return f"{_AUTHORIZE_URL}?{query}"


def _single_parameter(params: dict[str, list[str]], name: str, *, required: bool) -> str | None:
    values = params.get(name, [])
    if len(values) > 1:
        raise OAuthCallbackError(f"OAuth callback contained duplicate {name} parameters")
    if not values:
        if required:
            raise OAuthCallbackError(f"OAuth callback did not contain {name}")
        return None
    if not values[0]:
        raise OAuthCallbackError(f"OAuth callback contained an empty {name} parameter")
    return values[0]


def parse_callback_url(callback_url: str, *, expected_state: str) -> str:
    """Validate a pasted/local callback and return its authorization code only."""
    try:
        parsed = urllib.parse.urlsplit(callback_url)
        port = parsed.port
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise OAuthCallbackError("OAuth callback URL is malformed") from exc

    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port != 8787
        or parsed.path != "/callback"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OAuthCallbackError("OAuth callback URL does not match the registered redirect URI")

    state = _single_parameter(params, "state", required=True)
    if state is None or not hmac.compare_digest(state, expected_state):
        raise OAuthCallbackError("OAuth callback state validation failed")
    code = _single_parameter(params, "code", required=False)
    error = _single_parameter(params, "error", required=False)
    if (code is None) == (error is None):
        raise OAuthCallbackError("OAuth callback must contain exactly one of code or error")
    if error is not None:
        raise OAuthAuthorizationDenied("Spotify authorization was not granted")
    if code is None:
        raise OAuthCallbackError("OAuth callback did not contain an authorization code")
    return code


def receive_manual_authorization_code(*, state: str) -> str:
    callback_url = getpass.getpass("Paste the complete callback URL: ")
    code = parse_callback_url(callback_url, expected_state=state)
    print("OAuth callback received.")
    return code


class _LocalCallbackHandler(BaseHTTPRequestHandler):
    timeout = 1.0
    expected_state = ""
    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        candidate = f"http://127.0.0.1:8787{self.path}"
        try:
            code = parse_callback_url(candidate, expected_state=self.expected_state)
        except OAuthAuthorizationDenied as exc:
            self.__class__.error = str(exc)
            self._reply(400, str(exc))
            return
        except OAuthCallbackError as exc:
            self._reply(400, str(exc))
            return
        self.__class__.code = code
        self._reply(200, "Authorization received. Return to the terminal; this tab can be closed.")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self, status: int, message: str) -> None:
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>News Bulletin Playlist</title>"
            f"<p>{message}</p>"
        )
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def receive_local_authorization_code(
    *,
    state: str,
    timeout: float = _LOCAL_CALLBACK_TIMEOUT_SECONDS,
    on_ready: Callable[[], None] | None = None,
) -> str:
    _LocalCallbackHandler.expected_state = state
    _LocalCallbackHandler.code = None
    _LocalCallbackHandler.error = None
    deadline = time.monotonic() + timeout
    with HTTPServer(("127.0.0.1", 8787), _LocalCallbackHandler) as server:
        if on_ready is not None:
            on_ready()
        while (
            _LocalCallbackHandler.code is None
            and _LocalCallbackHandler.error is None
            and time.monotonic() < deadline
        ):
            server.timeout = max(0.1, min(1.0, deadline - time.monotonic()))
            server.handle_request()
    if _LocalCallbackHandler.error is not None:
        raise OAuthAuthorizationDenied(_LocalCallbackHandler.error)
    if _LocalCallbackHandler.code is None:
        raise OAuthCallbackError("OAuth callback was not received before the timeout")
    return _LocalCallbackHandler.code


def validate_token_response(payload: dict[str, Any]) -> TokenResponse:
    access_token = payload.get("access_token")
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    scope = payload.get("scope")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Spotify token response did not contain a valid access token")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise RuntimeError("Spotify token response did not contain a Bearer token type")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise RuntimeError("Spotify token response did not contain a valid expiry")
    if not isinstance(scope, str):
        raise RuntimeError("Spotify token response did not contain granted scopes")
    return TokenResponse(access_token, expires_in, tuple(scope.split()))


def require_granted_scopes(
    granted_scopes: tuple[str, ...], required_scopes: tuple[str, ...]
) -> None:
    granted = set(granted_scopes)
    missing = [scope for scope in required_scopes if scope not in granted]
    if missing:
        raise RuntimeError("Spotify did not grant all requested scopes: " + ", ".join(missing))


def exchange_code(client_id: str, code: str, verifier: str) -> TokenResponse:
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            payload = bytes(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Spotify token exchange failed (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Spotify token exchange failed due to a network error") from exc
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Spotify token exchange returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Spotify token exchange returned an unexpected response")
    return validate_token_response(cast(dict[str, Any], decoded))


def write_authorization_url_file(path: str, authorize_url: str) -> None:
    """Write the one-time OAuth URL to a new private file instead of application logs."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(authorize_url)
            stream.write("\n")
    except BaseException:
        with suppress(OSError):
            os.unlink(path)
        raise


def present_authorization_url(
    authorize_url: str,
    *,
    authorization_url_file: str | None,
) -> None:
    """Hand off the sensitive one-time URL without echoing it to stdout/stderr."""
    if authorization_url_file is not None:
        write_authorization_url_file(authorization_url_file, authorize_url)
        print(f"Spotify authorization URL written to private file: {authorization_url_file}")
        return
    try:
        opened = webbrowser.open(authorize_url, new=2, autoraise=True)
    except webbrowser.Error as exc:
        raise RuntimeError(
            "Could not open the Spotify authorization browser; rerun with "
            "--authorization-url-file PATH"
        ) from exc
    if not opened:
        raise RuntimeError(
            "Could not open the Spotify authorization browser; rerun with "
            "--authorization-url-file PATH"
        )
    print("Spotify authorization opened in the default browser.")


def _print_manual_instructions() -> None:
    print(
        "After authorizing, Spotify will try http://127.0.0.1:8787/callback. "
        "A browser connection error is expected when this helper runs in a remote container."
    )
    print("Copy the complete callback URL from the browser address bar and paste it below.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the authenticated Spotify P0 probe via PKCE.")
    parser.add_argument("--write", action="store_true", help="also run the playlist write probe")
    parser.add_argument("--callback-mode", choices=("manual", "local"), default="manual")
    parser.add_argument(
        "--authorization-url-file",
        help=(
            "write the one-time authorization URL to a new private file instead of opening "
            "a browser"
        ),
    )
    parser.add_argument(
        "--market", default="ES", help="Spotify ISO 3166-1 alpha-2 market for this P0 probe"
    )
    args = parser.parse_args()
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        parser.error("SPOTIFY_CLIENT_ID is required")
    verifier = create_code_verifier()
    state = secrets.token_urlsafe(32)
    requested_scopes = scopes_for_mode(args.write)
    authorize_url = build_authorize_url(
        client_id,
        state=state,
        challenge=create_code_challenge(verifier),
        scopes=requested_scopes,
    )
    if args.callback_mode == "manual":
        present_authorization_url(
            authorize_url,
            authorization_url_file=args.authorization_url_file,
        )
        _print_manual_instructions()
        code = receive_manual_authorization_code(state=state)
    else:
        code = receive_local_authorization_code(
            state=state,
            on_ready=lambda: present_authorization_url(
                authorize_url,
                authorization_url_file=args.authorization_url_file,
            ),
        )
        print("OAuth callback received.")
    token = exchange_code(client_id, code, verifier)
    require_granted_scopes(token.granted_scopes, requested_scopes)
    print("Granted scopes:")
    for scope in token.granted_scopes:
        print(f"- {scope}")
    client = SpotifyClient(access_token=token.access_token, market=args.market)
    result = run_catalog_probe(client)
    if result != 0 or not args.write:
        return result
    return run_write_probe(client)


if __name__ == "__main__":
    raise SystemExit(main())
