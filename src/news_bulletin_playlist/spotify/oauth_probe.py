from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar, cast

from news_bulletin_playlist.spotify.client import SpotifyClient
from news_bulletin_playlist.spotify.probe import run_catalog_probe, run_write_probe

_REDIRECT_URI = "http://127.0.0.1:8787/callback"
_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SCOPES = ("user-read-playback-position", "playlist-modify-private")


def create_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def create_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(client_id: str, *, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _REDIRECT_URI,
            "scope": " ".join(_SCOPES),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    return f"{_AUTHORIZE_URL}?{query}"


class _CallbackHandler(BaseHTTPRequestHandler):
    expected_state: ClassVar[str] = ""
    code: ClassVar[str | None] = None
    error: ClassVar[str | None] = None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        returned_state = (params.get("state") or [""])[0]
        error = (params.get("error") or [None])[0]
        code = (params.get("code") or [None])[0]

        if parsed.path != "/callback":
            self._reply(404, "Unexpected callback path. You can close this tab.")
            return
        if returned_state != self.expected_state:
            self.__class__.error = "OAuth state mismatch"
            self._reply(400, "Authorization state mismatch. You can close this tab.")
            return
        if error:
            self.__class__.error = error
            self._reply(400, "Spotify authorization was not granted. You can close this tab.")
            return
        if not code:
            self.__class__.error = "callback did not contain an authorization code"
            self._reply(400, "Missing authorization code. You can close this tab.")
            return

        self.__class__.code = code
        self._reply(200, "Authorization received. Return to the terminal; this tab can be closed.")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self, status: int, message: str) -> None:
        body = f"<!doctype html><meta charset='utf-8'><title>News Bulletin Playlist</title><p>{message}</p>"
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def receive_authorization_code(authorize_url: str, *, state: str) -> str:
    _CallbackHandler.expected_state = state
    _CallbackHandler.code = None
    _CallbackHandler.error = None

    with HTTPServer(("127.0.0.1", 8787), _CallbackHandler) as server:
        print("Opening Spotify authorization in your browser...")
        print(f"If it does not open automatically, visit:\n{authorize_url}\n")
        webbrowser.open(authorize_url)
        server.handle_request()

    if _CallbackHandler.error:
        raise RuntimeError(_CallbackHandler.error)
    if not _CallbackHandler.code:
        raise RuntimeError("authorization callback was not received")
    return _CallbackHandler.code


def exchange_code(client_id: str, code: str, verifier: str) -> dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30.0) as response:
        payload = bytes(response.read())
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise RuntimeError("unexpected Spotify token response")
    return cast(dict[str, Any], decoded)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the authenticated Spotify P0 probe via PKCE.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="also create and validate a temporary private playlist",
    )
    args = parser.parse_args()

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        parser.error("SPOTIFY_CLIENT_ID is required")

    verifier = create_code_verifier()
    state = secrets.token_urlsafe(32)
    authorize_url = build_authorize_url(
        client_id,
        state=state,
        challenge=create_code_challenge(verifier),
    )
    code = receive_authorization_code(authorize_url, state=state)
    token = exchange_code(client_id, code, verifier)
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Spotify token response did not contain an access token")

    client = SpotifyClient(access_token=access_token)
    result = run_catalog_probe(client)
    if result != 0 or not args.write:
        return result
    return run_write_probe(client)


if __name__ == "__main__":
    raise SystemExit(main())
