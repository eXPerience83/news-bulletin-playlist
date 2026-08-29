from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse

import pytest

from news_bulletin_playlist.spotify import oauth_probe


def callback(query: str) -> str:
    return f"http://127.0.0.1:8787/callback?{query}"


def test_verifier_is_valid_pkce_length_and_ascii() -> None:
    verifier = oauth_probe.create_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert verifier.isascii()


def test_challenge_uses_s256_known_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    expected = expected.rstrip("=")
    assert oauth_probe.create_code_challenge(verifier) == expected


def test_read_and_write_scopes_are_separate() -> None:
    assert oauth_probe.scopes_for_mode(False) == (
        "user-read-playback-position",
        "user-read-private",
    )
    assert oauth_probe.scopes_for_mode(True) == (
        "user-read-playback-position",
        "user-read-private",
        "playlist-modify-private",
        "playlist-read-private",
    )


def test_authorize_url_uses_registered_redirect_and_requested_scopes() -> None:
    url = oauth_probe.build_authorize_url(
        "client", state="state", challenge="challenge", scopes=oauth_probe.scopes_for_mode(False)
    )
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert params["redirect_uri"] == [oauth_probe.REDIRECT_URI]
    assert params["scope"] == ["user-read-playback-position user-read-private"]


@pytest.mark.parametrize(
    "query, message",
    [
        ("code=code", "state"),
        ("code=code&state=wrong", "state validation"),
        ("code=code&state=one&state=two", "duplicate state"),
        ("code=one&code=two&state=expected", "duplicate code"),
        ("error=access_denied&error=other&state=expected", "duplicate error"),
        ("code=code&error=access_denied&state=expected", "exactly one"),
        ("state=expected", "exactly one"),
        ("error=access_denied&state=expected", "not granted"),
    ],
)
def test_callback_rejects_sensitive_parameter_errors(query: str, message: str) -> None:
    with pytest.raises(oauth_probe.OAuthCallbackError, match=message):
        oauth_probe.parse_callback_url(callback(query), expected_state="expected")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8787/callback?code=code&state=expected",
        "http://localhost:8787/callback?code=code&state=expected",
        "http://127.0.0.1:9999/callback?code=code&state=expected",
        "http://127.0.0.1:8787/wrong?code=code&state=expected",
        "http://user@127.0.0.1:8787/callback?code=code&state=expected",
        "http://127.0.0.1:8787/callback?code=code&state=expected#fragment",
        "not a URL",
    ],
)
def test_callback_rejects_wrong_redirect_shape(url: str) -> None:
    with pytest.raises(oauth_probe.OAuthCallbackError):
        oauth_probe.parse_callback_url(url, expected_state="expected")


def test_callback_accepts_exactly_one_valid_code() -> None:
    parsed = oauth_probe.parse_callback_url(
        callback("code=code&state=expected"), expected_state="expected"
    )
    assert parsed == "code"


def test_authorization_denial_has_distinct_safe_error() -> None:
    with pytest.raises(oauth_probe.OAuthAuthorizationDenied, match="not granted"):
        oauth_probe.parse_callback_url(
            callback("error=access_denied&state=expected"), expected_state="expected"
        )


def test_manual_callback_does_not_echo_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_code = "test-authorization-code"
    monkeypatch.setattr(
        oauth_probe.getpass, "getpass", lambda _: callback(f"code={secret_code}&state=expected")
    )
    assert oauth_probe.receive_manual_authorization_code(state="expected") == secret_code
    captured = capsys.readouterr()
    assert "OAuth callback received." in captured.out
    assert secret_code not in captured.out + captured.err


class _FakeLocalServer:
    def __init__(self, *_: object, **__: object) -> None:
        self.timeout = 0.0

    def __enter__(self) -> _FakeLocalServer:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def handle_request(self) -> None:
        oauth_probe._LocalCallbackHandler.error = "Spotify authorization was not granted"


def test_local_callback_stops_immediately_on_authorization_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth_probe, "HTTPServer", _FakeLocalServer)
    with pytest.raises(oauth_probe.OAuthAuthorizationDenied, match="not granted"):
        oauth_probe.receive_local_authorization_code(state="expected", timeout=30.0)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_exchange_uses_same_redirect_uri_and_never_prints_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured_request: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        captured_request.append(request)
        return _Response(
            {
                "access_token": "test-access-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "user-read-private user-read-playback-position",
            }
        )

    monkeypatch.setattr(oauth_probe.urllib.request, "urlopen", fake_urlopen)
    token = oauth_probe.exchange_code("client", "test-code", "test-verifier")
    request = captured_request[0]
    assert isinstance(request, urllib.request.Request)
    body = urllib.parse.parse_qs(request.data.decode())
    assert body == {
        "client_id": ["client"],
        "grant_type": ["authorization_code"],
        "code": ["test-code"],
        "redirect_uri": [oauth_probe.REDIRECT_URI],
        "code_verifier": ["test-verifier"],
    }
    assert token.granted_scopes == ("user-read-private", "user-read-playback-position")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "test-access-token" not in combined
    assert "test-code" not in combined
    assert "test-verifier" not in combined


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"access_token": "x", "token_type": "not-bearer", "expires_in": 1, "scope": "x"},
        {"access_token": "x", "token_type": "Bearer", "expires_in": 0, "scope": "x"},
        {"access_token": "x", "token_type": "Bearer", "expires_in": True, "scope": "x"},
        {"access_token": "x", "token_type": "Bearer", "expires_in": 1},
    ],
)
def test_token_response_requires_safe_expected_fields(payload: dict[str, object]) -> None:
    with pytest.raises(RuntimeError):
        oauth_probe.validate_token_response(payload)


def test_required_scopes_fail_closed_when_spotify_omits_one() -> None:
    oauth_probe.require_granted_scopes(
        ("user-read-private", "user-read-playback-position"),
        ("user-read-playback-position", "user-read-private"),
    )
    with pytest.raises(RuntimeError, match="playlist-read-private"):
        oauth_probe.require_granted_scopes(
            ("user-read-private", "user-read-playback-position", "playlist-modify-private"),
            oauth_probe.scopes_for_mode(True),
        )
