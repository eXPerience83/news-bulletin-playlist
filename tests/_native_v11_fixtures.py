"""Reproduce the previous synthetic empty-v10 operational witness in tests only."""

import json

from news_bulletin_playlist.catalog import BuiltInCatalog
from news_bulletin_playlist.legacy_catalog_snapshot import build_legacy_snapshot_v2
from news_bulletin_playlist.legacy_materialization import preview_legacy_materialization
from news_bulletin_playlist.managed_state import ManagedState, serialize_managed_state
from news_bulletin_playlist.v11_candidate import build_v11_candidate
from news_bulletin_playlist.v11_operational_codec import build_v11_operational_from_candidate


def old_empty_v11_bytes() -> bytes:
    catalog = BuiltInCatalog((), ())
    empty = ManagedState(schema_version=10)
    original = (
        json.dumps(
            serialize_managed_state(empty, catalog=catalog),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()
    frozen = build_legacy_snapshot_v2(preview_legacy_materialization(catalog, empty), original)
    return build_v11_operational_from_candidate(build_v11_candidate(frozen, original))
