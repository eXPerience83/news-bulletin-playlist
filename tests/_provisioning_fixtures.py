"""Reusable isolated provisioning-intent factory for offline tests."""

from __future__ import annotations

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState


def _request_intent() -> ProvisioningIntent:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    return ProvisioningIntent(
        state=ProvisioningState.REQUEST_STARTED,
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        retention_hours=template.retention_hours,
        max_episodes=template.max_episodes,
        max_duration_seconds=1800,
    )
