from __future__ import annotations

from news_bulletin_playlist.spotify.identifiers import is_spotify_id


def test_spotify_id_accepts_only_canonical_ascii_base62() -> None:
    assert is_spotify_id("AbCdEf0123456789GhIjKl")
    for invalid in (
        "",
        "A" * 21,
        "A" * 23,
        "A" * 21 + "-",
        "é" * 22,
        "１２３４５６７８９０１２３４５６７８９０１２",
    ):
        assert not is_spotify_id(invalid)
