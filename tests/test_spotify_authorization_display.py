from __future__ import annotations

from news_bulletin_playlist.spotify.authorization_display import authorization_state_label
from news_bulletin_playlist.spotify.auth import AuthorizationState


_CASES: tuple[tuple[AuthorizationState | None, str], ...] = (
    (None, "Not configured"),
    (AuthorizationState.DISCONNECTED, "Not connected"),
    (AuthorizationState.CONNECTED, "Connected"),
    (AuthorizationState.REAUTH_REQUIRED, "Reauthorization required"),
    (AuthorizationState.ERROR, "Authorization state error"),
)


def test_authorization_state_labels_cover_every_supported_state() -> None:
    assert {state for state, _ in _CASES if state is not None} == set(AuthorizationState)
    assert [(state, authorization_state_label(state)) for state, _ in _CASES] == list(_CASES)
