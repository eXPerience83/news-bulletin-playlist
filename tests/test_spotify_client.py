import pytest

from news_bulletin_playlist.spotify.client import SpotifyClient


def test_show_episode_limit_guard() -> None:
    client = SpotifyClient("token")
    with pytest.raises(ValueError):
        client.show_episodes("show", limit=51)


def test_search_limit_guard() -> None:
    client = SpotifyClient("token")
    with pytest.raises(ValueError):
        client.search_shows("query", limit=11)


def test_replace_playlist_hard_limit() -> None:
    client = SpotifyClient("token")
    with pytest.raises(ValueError):
        client.replace_playlist_items("playlist", [f"spotify:episode:{i}" for i in range(101)])


def test_playlist_read_limit_guard() -> None:
    client = SpotifyClient("token")
    with pytest.raises(ValueError):
        client.playlist_items("playlist", limit=101)
