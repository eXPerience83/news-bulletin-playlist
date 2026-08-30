"""Built-in, read-only catalog shipped with each application image."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    CountryCode,
    ExternalReference,
    LanguageTag,
    OrderingPolicy,
    ParserId,
    PlaylistId,
    SourceDefinition,
    SourceId,
)


@dataclass(frozen=True, slots=True)
class PlaylistTemplate:
    """Defaults offered when activating a managed playlist for the first time."""

    id: PlaylistId
    display_name: str
    description: str
    countries: tuple[CountryCode, ...]
    languages: tuple[LanguageTag, ...]
    default_source_ids: tuple[SourceId, ...]
    cover_id: str
    retention_hours: int = 48
    max_episodes: int = 100
    ordering: OrderingPolicy = OrderingPolicy.PUBLISHED_AT_DESC


@dataclass(frozen=True, slots=True)
class BuiltInCatalog:
    """Application-owned source definitions and managed-playlist templates."""

    sources: tuple[SourceDefinition, ...]
    playlists: tuple[PlaylistTemplate, ...]

    def __post_init__(self) -> None:
        source_ids = [source.id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("built-in catalog contains duplicate source ids")
        template_ids = [template.id for template in self.playlists]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("built-in catalog contains duplicate playlist template ids")
        known_sources = set(source_ids)
        for source in self.sources:
            if not source.enabled or source.endpoint_url is None:
                raise ValueError(
                    f"built-in source {source.id} must be operational and have an endpoint"
                )
        for template in self.playlists:
            if not template.display_name.strip() or not template.cover_id.strip():
                raise ValueError(f"playlist template {template.id} has incomplete metadata")
            if not template.default_source_ids:
                raise ValueError(f"playlist template {template.id} must select a default source")
            if len(template.default_source_ids) != len(set(template.default_source_ids)):
                raise ValueError(f"playlist template {template.id} contains duplicate sources")
            for source_id in template.default_source_ids:
                if source_id not in known_sources:
                    raise ValueError(
                        f"playlist template {template.id} references unknown source {source_id}"
                    )
            if template.retention_hours <= 0 or template.max_episodes <= 0:
                raise ValueError(f"playlist template {template.id} has invalid retention policy")

    def source(self, source_id: SourceId | str) -> SourceDefinition:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(f"unknown catalog source: {source_id}")

    def playlist(self, template_id: PlaylistId | str) -> PlaylistTemplate:
        for playlist in self.playlists:
            if playlist.id == template_id:
                return playlist
        raise KeyError(f"unknown playlist template: {template_id}")


BUILTIN_SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        id=SourceId("ser"),
        display_name="Cadena SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        endpoint_url="https://fapi-top.prisasd.com/podcast/playser/boletines.xml",
        external_references=(
            ExternalReference("spotify", "show", "4EwwdoHHYmbt49UXODQMpi"),
        ),
    ),
    SourceDefinition(
        id=SourceId("rne"),
        display_name="Radio Nacional de España",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("rne"),
        endpoint_url="https://api.rtve.es/api/adapter/programas/1750/audios.rss",
        external_references=(
            ExternalReference("spotify", "show", "0UgidTKsoaHiHDARuPQNW1"),
        ),
    ),
    SourceDefinition(
        id=SourceId("ondacero"),
        display_name="Onda Cero",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ondacero"),
        endpoint_url=(
            "https://www.ondacero.es/rss/podcast/mount/"
            "ATRESMEDIA_LAS_NOTICIAS_EN_ONDA_CERO_P/fastly"
        ),
        external_references=(
            ExternalReference("spotify", "show", "0tjEexypyczHXW9vE3SU3P"),
        ),
    ),
    SourceDefinition(
        id=SourceId("cnn"),
        display_name="CNN 5 Cosas",
        countries=(CountryCode("US"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("America/New_York"),
        enabled=True,
        parser_id=ParserId("cnn"),
        endpoint_url="https://feeds.megaphone.fm/WMHY5696831164",
        external_references=(
            ExternalReference("spotify", "show", "0vDgnorbpBr65YZzFVVouE"),
        ),
    ),
)

BUILTIN_PLAYLISTS: tuple[PlaylistTemplate, ...] = (
    PlaylistTemplate(
        id=PlaylistId("spain_spanish_news"),
        display_name="Noticias España",
        description=(
            "Últimos boletines de actualidad de las fuentes seleccionadas, actualizados "
            "automáticamente y ordenados de más reciente a más antigua."
        ),
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        default_source_ids=(
            SourceId("ser"),
            SourceId("rne"),
            SourceId("ondacero"),
            SourceId("cnn"),
        ),
        cover_id="spain_spanish_news",
    ),
)

BUILTIN_CATALOG = BuiltInCatalog(sources=BUILTIN_SOURCES, playlists=BUILTIN_PLAYLISTS)
