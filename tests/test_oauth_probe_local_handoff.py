from __future__ import annotations

import pytest

from news_bulletin_playlist.spotify import oauth_probe


class _ReadyServer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.timeout = 0.0
        self.events.append("bound")

    def __enter__(self) -> _ReadyServer:
        self.events.append("entered")
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def handle_request(self) -> None:
        self.events.append("request")
        oauth_probe._LocalCallbackHandler.code = "authorization-code"


def test_local_callback_listener_is_ready_before_browser_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_server(*_: object, **__: object) -> _ReadyServer:
        return _ReadyServer(events)

    monkeypatch.setattr(oauth_probe, "HTTPServer", fake_server)

    code = oauth_probe.receive_local_authorization_code(
        state="expected",
        timeout=30.0,
        on_ready=lambda: events.append("handoff"),
    )

    assert code == "authorization-code"
    assert events == ["bound", "entered", "handoff", "request"]
