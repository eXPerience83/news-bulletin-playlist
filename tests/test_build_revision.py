from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime

from news_bulletin_playlist.build_info import build_revision
from news_bulletin_playlist.diagnostics_runtime import _safe_operational_status_page
from news_bulletin_playlist.diagnostics_web import build_diagnostic_bundle


def test_build_revision_shortens_only_valid_full_git_sha() -> None:
    full = "0123456789abcdef0123456789abcdef01234567"
    assert build_revision({"NEWS_PLAYLIST_BUILD_REVISION": full}) == "0123456789ab"
    assert build_revision({"NEWS_PLAYLIST_BUILD_REVISION": full.upper()}) == "0123456789ab"
    assert build_revision({"NEWS_PLAYLIST_BUILD_REVISION": "0123456789ab"}) == "dev"
    assert build_revision({"NEWS_PLAYLIST_BUILD_REVISION": "<script>"}) == "dev"
    assert build_revision({}) == "dev"


def test_public_status_page_exposes_safe_build_revision() -> None:
    body = _safe_operational_status_page(
        ready=True,
        spotify_state=None,
        status=None,
        build_revision="0123456789ab",
    ).decode()
    assert "<dt>Build</dt><dd><code>0123456789ab</code></dd>" in body


def test_diagnostic_bundle_includes_build_revision() -> None:
    payload = build_diagnostic_bundle(
        events=(),
        generated_at=datetime(2026, 9, 3, tzinfo=UTC),
        last_cycle=None,
        retention_days=30,
        max_events=10_000,
        build_revision="0123456789ab",
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        runtime = json.loads(archive.read("runtime.json"))
    assert runtime["build_revision"] == "0123456789ab"
