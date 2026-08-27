from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, cast

_API_BASE = "https://api.spotify.com/v1"


class SpotifyApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Spotify API {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class SpotifyClient:
    access_token: str
    market: str = "ES"
    api_base: str = _API_BASE

    def show_episodes(self, show_id: str, *, limit: int = 50) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("show episode limit must be between 1 and 50")
        return self._request(
            "GET",
            f"/shows/{show_id}/episodes",
            query={"market": self.market, "limit": str(limit)},
        )

    def search_shows(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        if not 1 <= limit <= 10:
            raise ValueError("search limit must be between 1 and 10")
        return self._request(
            "GET",
            "/search",
            query={"q": query, "type": "show", "market": self.market, "limit": str(limit)},
        )

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            "/me/playlists",
            json_body={"name": name, "description": description, "public": False},
        )

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        if len(uris) > 100:
            raise ValueError("playlist replacement is limited to 100 items")
        return self._request(
            "PUT",
            f"/playlists/{playlist_id}/items",
            json_body={"uris": uris},
        )

    def playlist_items(self, playlist_id: str, *, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("playlist item limit must be between 1 and 100")
        return self._request(
            "GET",
            f"/playlists/{playlist_id}/items",
            query={"limit": str(limit)},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        data = None if json_body is None else json.dumps(json_body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                payload = bytes(response.read())
        except urllib.error.HTTPError as exc:
            body = bytes(exc.read()).decode("utf-8", errors="replace")
            raise SpotifyApiError(exc.code, body) from exc

        if not payload:
            return {}
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise SpotifyApiError(500, "unexpected non-object JSON response")
        return cast(dict[str, Any], decoded)
