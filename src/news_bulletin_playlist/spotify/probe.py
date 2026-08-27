from __future__ import annotations

import os
import sys
from typing import Any

from news_bulletin_playlist.registry import CORE_PROVIDERS
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyClient

_RNE_DUPLICATE_DATE = "2026-08-25"
_RNE_DUPLICATE_TIME_TOKENS = ("18.00", "18:00")


def _episode_summary(item: dict[str, Any]) -> str:
    return " | ".join(
        [
            str(item.get("release_date", "?")),
            str(item.get("name", "?")),
            str(item.get("id", "?")),
        ]
    )


def run_catalog_probe(client: SpotifyClient) -> int:
    failures = 0
    for provider in CORE_PROVIDERS:
        try:
            page = client.show_episodes(provider.spotify_show_id, limit=50)
        except SpotifyApiError as exc:
            print(f"FAIL {provider.provider_id}: {exc}")
            failures += 1
            continue
        items = page.get("items") or []
        print(f"\n[{provider.provider_id}] {len(items)} episode(s) returned")
        for item in items[:5]:
            if isinstance(item, dict):
                print(f"  {_episode_summary(item)}")

        if provider.provider_id == "rne":
            matches = [
                item
                for item in items
                if isinstance(item, dict)
                and item.get("release_date") == _RNE_DUPLICATE_DATE
                and any(
                    token in str(item.get("name", ""))
                    for token in _RNE_DUPLICATE_TIME_TOKENS
                )
            ]
            print(f"  RNE duplicate probe: {len(matches)} matching Spotify episode(s)")
            for item in matches:
                print(f"    {_episode_summary(item)}")

    print("\n[cope-search]")
    try:
        result = client.search_shows("Boletines COPE", limit=10)
    except SpotifyApiError as exc:
        print(f"FAIL cope search: {exc}")
        failures += 1
    else:
        shows = (result.get("shows") or {}).get("items") or []
        for show in shows:
            if isinstance(show, dict):
                print(f"  {show.get('name')} | {show.get('publisher')} | {show.get('id')}")
        if not shows:
            print("  no show results")

    return 0 if failures == 0 else 1


def run_write_probe(client: SpotifyClient) -> int:
    latest_uris: list[str] = []
    for provider in CORE_PROVIDERS:
        page = client.show_episodes(provider.spotify_show_id, limit=1)
        items = page.get("items") or []
        if not items or not isinstance(items[0], dict) or not items[0].get("uri"):
            print(f"FAIL write probe: no latest URI for {provider.provider_id}")
            return 1
        latest_uris.append(str(items[0]["uri"]))

    playlist = client.create_private_playlist(
        "News Bulletin Playlist P0 Probe",
        description="Temporary private integration probe. Safe to delete.",
    )
    playlist_id = str(playlist["id"])
    print(f"Created private probe playlist: {playlist_id}")

    client.replace_playlist_items(playlist_id, latest_uris)
    readback = client.playlist_items(playlist_id, limit=100)
    returned = readback.get("items") or []
    print(f"Read back {len(returned)} playlist item(s)")
    if len(returned) != len(latest_uris):
        print("FAIL write probe: readback count differs")
        return 1

    print("Write/readback probe passed. Playlist intentionally left private for inspection.")
    return 0


def main() -> int:
    access_token = os.environ.get("SPOTIFY_ACCESS_TOKEN")
    if not access_token:
        print("SPOTIFY_ACCESS_TOKEN is required", file=sys.stderr)
        return 2

    client = SpotifyClient(access_token=access_token)
    result = run_catalog_probe(client)
    if result != 0:
        return result

    if os.environ.get("SPOTIFY_PROBE_WRITE") == "1":
        return run_write_probe(client)

    print("\nWrite probe skipped (set SPOTIFY_PROBE_WRITE=1 to enable it explicitly).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
