from __future__ import annotations

import os
import sys
from typing import Any

from news_bulletin_playlist.registry import CORE_PROVIDERS
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)

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


def _format_error(exc: SpotifyApiError | SpotifyTransportError) -> str:
    if isinstance(exc, SpotifyTransportError):
        return "network error"
    suffix = f"; retry after {exc.retry_after}s" if exc.retry_after is not None else ""
    return f"HTTP {exc.status} ({exc.message}){suffix}"


def _require_items(container: object, *, context: str) -> list[object]:
    if not isinstance(container, dict):
        raise RuntimeError(f"{context} was not an object")
    items = container.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"{context} did not contain an item list")
    return items


def _show_episodes_for_investigation(
    client: SpotifyClient, show_id: str, *, max_pages: int = 4
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    for page_number in range(max_pages):
        page = client.show_episodes(show_id, limit=50, offset=page_number * 50)
        items = _require_items(page, context="Spotify show episodes response")
        episodes.extend(item for item in items if isinstance(item, dict))
        if not page.get("next") or not items:
            break
    return episodes


def _first_show_page(client: SpotifyClient, show_id: str) -> list[dict[str, Any]]:
    page = client.show_episodes(show_id, limit=50, offset=0)
    items = _require_items(page, context="Spotify show episodes response")
    return [item for item in items if isinstance(item, dict)]


def run_catalog_probe(client: SpotifyClient) -> int:
    failures = 0
    for provider in CORE_PROVIDERS:
        try:
            if provider.provider_id == "rne":
                items = _show_episodes_for_investigation(client, provider.spotify_show_id)
            else:
                items = _first_show_page(client, provider.spotify_show_id)
        except (SpotifyApiError, SpotifyTransportError) as exc:
            print(f"FAIL {provider.provider_id}: {_format_error(exc)}")
            failures += 1
            continue
        except RuntimeError as exc:
            print(f"FAIL {provider.provider_id}: invalid Spotify response ({exc})")
            failures += 1
            continue
        print(f"\n[{provider.provider_id}] {len(items)} episode(s) returned")
        for item in items[:5]:
            print(f"  {_episode_summary(item)}")

        if provider.provider_id == "rne":
            matches = [
                item
                for item in items
                if item.get("release_date") == _RNE_DUPLICATE_DATE
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
        shows = result.get("shows")
        show_items = _require_items(shows, context="Spotify search response")
    except (SpotifyApiError, SpotifyTransportError) as exc:
        print(f"FAIL cope search: {_format_error(exc)}")
        failures += 1
    except RuntimeError as exc:
        print(f"FAIL cope search: invalid Spotify response ({exc})")
        failures += 1
    else:
        for show in show_items:
            if isinstance(show, dict):
                print(f"  {show.get('name')} | {show.get('publisher')} | {show.get('id')}")
        if not show_items:
            print("  no show results")

    return 0 if failures == 0 else 1


def _extract_playlist_uris(items: object) -> list[str]:
    if not isinstance(items, list):
        raise RuntimeError("Spotify playlist response did not contain an item list")
    uris: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Spotify playlist response contained an invalid item")
        value = item.get("item") or item.get("track")
        if not isinstance(value, dict) or not isinstance(value.get("uri"), str):
            raise RuntimeError("Spotify playlist response contained an item without a URI")
        uris.append(value["uri"])
    return uris


def _read_playlist_uris(client: SpotifyClient, playlist_id: str) -> list[str]:
    page = client.playlist_items(playlist_id, limit=100, offset=0)
    items = _require_items(page, context="Spotify playlist response")
    uris = _extract_playlist_uris(items)
    if page.get("next"):
        overflow = client.playlist_items(playlist_id, limit=1, offset=len(items))
        overflow_items = _require_items(overflow, context="Spotify playlist overflow response")
        overflow_uris = _extract_playlist_uris(overflow_items)
        if not overflow_uris:
            raise RuntimeError("Spotify playlist pagination reported an item that was not returned")
        uris.append(overflow_uris[0])
    return uris


def reconcile_playlist_items(
    client: SpotifyClient, playlist_id: str, desired_uris: list[str]
) -> bool:
    """Replace only when the complete desired playlist differs; return whether it wrote."""
    if len(desired_uris) > 100:
        raise ValueError("playlist reconciliation is limited to 100 items")
    current = _read_playlist_uris(client, playlist_id)
    if current == desired_uris:
        return False
    client.replace_playlist_items(playlist_id, desired_uris)
    return True


def run_write_probe(client: SpotifyClient) -> int:
    latest_uris: list[str] = []
    for provider in CORE_PROVIDERS:
        page = client.show_episodes(provider.spotify_show_id, limit=1)
        try:
            items = _require_items(page, context="Spotify show episodes response")
        except RuntimeError as exc:
            print(f"FAIL write probe: invalid response for {provider.provider_id} ({exc})")
            return 1
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

    reconcile_playlist_items(client, playlist_id, latest_uris)
    returned_uris = _read_playlist_uris(client, playlist_id)
    print(f"Read back {len(returned_uris)} playlist item(s)")
    if returned_uris != latest_uris:
        print("FAIL write probe: readback URIs or order differ")
        return 1
    if reconcile_playlist_items(client, playlist_id, latest_uris):
        print("FAIL write probe: unchanged desired state caused a second write")
        return 1
    external_url = (playlist.get("external_urls") or {}).get("spotify")
    print("Write/readback probe passed; second reconciliation performed 0 writes.")
    print(f"Private probe playlist: {playlist_id}")
    if isinstance(external_url, str):
        print(f"Playlist URL: {external_url}")
    print("Playlist intentionally left private for inspection.")
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
