"""In-memory v11 projection must preserve existing destinations on old schemas."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root

from news_bulletin_playlist.legacy_catalog_snapshot import (
    FrozenLegacySnapshotError,
    build_legacy_snapshot,
)
from news_bulletin_playlist.legacy_materialization import LegacyProvenance
from news_bulletin_playlist.v11_projection import project_v11_legacy


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_projection_preserves_runtime_and_all_destinations(tmp_path: Path, schema: int) -> None:
    root, preview = _root(tmp_path, schema)
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    state_bytes = before["managed-state.json"]
    frozen_bytes = build_legacy_snapshot(preview, state_bytes)
    actual = project_v11_legacy(frozen_bytes, state_bytes)
    assert actual.original_state == preview.original_state
    assert actual.projected_runtime.playlists == preview.original_runtime.playlists
    assert {s.id: s for s in actual.projected_runtime.sources} == {
        s.id: s for s in preview.original_runtime.sources
    }
    # Frozen v1 exports sources in Spotify-show order, not original image order:
    # no deployment may use this projection to replace runtime config yet.
    assert actual.source_order_verified is False
    assert actual.managed_destinations == preview.original_state.playlists
    assert len(actual.managed_destinations) == 4
    assert len(actual.installed_sources) == 13
    assert len(actual.playlist_examples) == 7
    assert set(actual.source_provenance) == {LegacyProvenance.IMAGE_PRESET}
    assert actual.local_drafts == preview.original_state.templates
    assert actual.quarantined_drafts == preview.original_state.quarantined_templates
    with pytest.raises(FrozenLegacySnapshotError, match="does not match"):
        project_v11_legacy(frozen_bytes, state_bytes + b" ")
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before
