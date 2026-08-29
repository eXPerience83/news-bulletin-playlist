from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, cast

_API_BASE = "https://api.spotify.com/v1"


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

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            "/me/playlists",
            json_body={"name": name, "description": description, "public": False},
        )

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        if len(uris) > 100:
            raise ValueError("playlist replacement is limited to 100 items")
        return self._request("PUT", f"/playlists/{playlist_id}/items", json_body={"uris": uris})

    def playlist_items(
        self, playlist_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("playlist item limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("playlist item offset must not be negative")
        return self._request(
            "GET",
            f"/playlists/{playlist_id}/items",
            query={"limit": str(limit), "offset": str(offset)},
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
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = None if json_body is None else json.dumps(json_body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
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
