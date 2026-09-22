"""Operator-facing Spotify authorization-state presentation helpers."""

from __future__ import annotations

from news_bulletin_playlist.spotify.auth import AuthorizationState


def authorization_state_label(state: AuthorizationState | None) -> str:
    """Return the operator-facing label for Spotify authorization state."""
    if state is None:
        return "Not configured"
    return {
        AuthorizationState.DISCONNECTED: "Not connected",
        AuthorizationState.CONNECTED: "Connected",
        AuthorizationState.REAUTH_REQUIRED: "Reauthorization required",
        AuthorizationState.ERROR: "Authorization state error",
    }[state]
