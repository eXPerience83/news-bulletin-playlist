from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from news_bulletin_playlist.reconciliation import _reconcile_playlist_items


def strict_reconcile_playlist_items(
    client: Any,
    playlist_id: str,
    desired_uris: Sequence[str],
) -> bool:
    """Exercise the strict internal reconciliation primitive without durable attestation state."""
    return _reconcile_playlist_items(
        client,
        playlist_id,
        desired_uris,
        store=None,
        logical_playlist_id=None,
        attestation_updated_at=None,
        mutation_guard=None,
    ).wrote
