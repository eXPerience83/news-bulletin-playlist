from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, cast

_API_BASE = "https://api.spotify.com/v1"
_SPOTIFY_COVER_MAX_PAYLOAD_BYTES = 256 * 1024


class SpotifyApiError(RuntimeError):
    def __init__(self, status: int, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(f"Spotify API {status}: {message}")
        self.status = status
        self.message = message
        self.retry_after = retry_after


class SpotifyTransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpotifyClient:
    access_token: str
    market: str | None = None
    api_base: str = _API_BASE

    def show_episodes(self, show_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("show episode limit must be between 1 and 50")
        if offset < 0:
            raise ValueError("show episode offset must not be negative")
        return self._request(
            "GET", f"/shows/{show_id}/episodes", query=self._market_query(limit, offset)
        )

    def search_shows(self, query: str, *, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 10:
            raise ValueError("search limit must be between 1 and 10")
        if offset < 0:
            raise ValueError("search offset must not be negative")
        params = {"q": query, "type": "show", "limit": str(limit), "offset": str(offset)}
        if self.market is not None:
            params["market"] = self.market
        return self._request("GET", "/search", query=params)

    def create_playlist(
        self,
        name: str,
        *,
        public: bool = True,
        description: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "public": public}
        if description:
            body["description"] = description
        return self._request("POST", "/me/playlists", json_body=body)

    def current_user(self) -> dict[str, Any]:
        return self._request("GET", "/me", query={"fields": "id"})

    def playlist_details(self, playlist_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/playlists/{playlist_id}",
            query={"fields": "id,owner(id)"},
        )

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/playlists/{playlist_id}",
            json_body={"name": name, "description": description},
        )

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        if not jpeg_bytes.startswith(b"\xff\xd8") or not jpeg_bytes.endswith(b"\xff\xd9"):
            raise ValueError("playlist cover must be a complete JPEG image")
        encoded = base64.b64encode(jpeg_bytes)
        if len(encoded) > _SPOTIFY_COVER_MAX_PAYLOAD_BYTES:
            raise ValueError("playlist cover exceeds Spotify's 256 KiB encoded payload limit")
        return self._request(
            "PUT",
            f"/playlists/{playlist_id}/images",
            raw_body=encoded,
            content_type="image/jpeg",
        )

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        if len(uris) > 100:
            raise ValueError("playlist replacement is limited to 100 items")
        return self._request("PUT", f"/playlists/{playlist_id}/items", json_body={"uris": uris})

    def playlist_items(
        self, playlist_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("playlist item limit must be between 1 and 50")
        if offset < 0:
            raise ValueError("playlist item offset must not be negative")
        return self._request(
            "GET",
            f"/playlists/{playlist_id}/items",
            query={
                "limit": str(limit),
                "offset": str(offset),
                "additional_types": "episode",
            },
        )

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        """Read only the current playlist version identifier."""
        return self._request(
            "GET",
            f"/playlists/{playlist_id}",
            query={"fields": "snapshot_id"},
        )

    def _market_query(self, limit: int, offset: int) -> dict[str, str]:
        params = {"limit": str(limit), "offset": str(offset)}
        if self.market is not None:
            params["market"] = self.market
        return params

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        if json_body is not None and raw_body is not None:
            raise ValueError("Spotify request cannot contain both JSON and raw bodies")
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else raw_body
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            if content_type is None:
                raise ValueError("raw Spotify request body requires a content type")
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                payload = bytes(response.read())
        except urllib.error.HTTPError as exc:
            raise SpotifyApiError(
                exc.code,
                _safe_http_message(exc.code),
                retry_after=_retry_after(exc.headers.get("Retry-After")),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SpotifyTransportError(
                "Spotify API request failed due to a network error"
            ) from exc
        if not payload:
            return {}
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SpotifyApiError(502, "invalid JSON response") from exc
        if not isinstance(decoded, dict):
            raise SpotifyApiError(502, "unexpected non-object JSON response")
        return cast(dict[str, Any], decoded)


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _safe_http_message(status: int) -> str:
    messages = {400: "bad request", 401: "unauthorized", 403: "forbidden", 429: "rate limited"}
    return messages.get(status, "request failed")
