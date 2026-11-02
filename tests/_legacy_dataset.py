"""Synthetic v6-v10 installation builder for migration and rollback tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.legacy_materialization import preview_legacy_materialization
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateStore,
    activate_template,
)


def legacy_root(tmp_path: Path, schema: int = 6):
    root = tmp_path / "legacy"
    root.mkdir()
    original = ManagedState(
        schema_version=schema,
        playlists=tuple(
            replace(
                activate_template(template, chr(65 + index) * 22),
                enabled=index != 2,
                source_ids=template.default_source_ids,
            )
            for index, template in enumerate(
                template
                for template in BUILTIN_CATALOG.playlists
                if str(template.id)
                in {
                    "spain_spanish_news",
                    "international_english_news",
                    "international_polish_news",
                    "international_spanish_long_news",
                }
            )
        ),
    )
    state = ManagedStateStore(root / "managed-state.json")
    state.save(original)
    (root / "oauth-private").write_bytes(b"secret-oauth")
    (root / "history.sqlite3").write_bytes(b"secret-history")
    (root / "pending-post.json").write_bytes(b"pending-post")
    preview = preview_legacy_materialization(BUILTIN_CATALOG, state.load_readonly())
    return root, preview
