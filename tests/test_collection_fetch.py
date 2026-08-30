from types import TracebackType
from typing import Self

import pytest

from news_bulletin_playlist import collection


class _OversizedResponse:
    requested_bytes: int | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.requested_bytes = size
        return b"x" * size


def test_fetch_feed_rejects_payload_over_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _OversizedResponse()
    monkeypatch.setattr(collection.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="exceeds 10 MiB limit"):
        collection.fetch_feed("https://example.test/feed.xml")

    assert response.requested_bytes == collection._MAX_FEED_BYTES + 1
