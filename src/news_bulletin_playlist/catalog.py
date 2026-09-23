"""Built-in, read-only catalog shipped with each application image."""

from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    CountryCode,
    DurationPolicy,
    EditorialScope,
    ExternalReference,
    LanguageTag,
    OrderingPolicy,
    ParserId,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceOrigin,
)
from news_bulletin_playlist.registry import has_collection_filter, has_title_parser


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
    ordering: OrderingPolicy = OrderingPolicy.EDITION_AT_DESC
    duration_policy: DurationPolicy = field(default_factory=DurationPolicy)


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
            if not source.enabled:
                raise ValueError(f"built-in source {source.id} must be enabled")
            if not has_title_parser(str(source.parser_id)):
                raise ValueError(f"built-in source {source.id} has an unknown title parser")
            spotify_show_ids = {
                reference.external_id.strip()
                for reference in source.external_references
                if reference.system.casefold() == "spotify"
                and reference.resource_type.casefold() == "show"
                and reference.external_id.strip()
            }
            if len(spotify_show_ids) != 1:
                raise ValueError(
                    f"built-in source {source.id} requires exactly one Spotify show reference"
                )
            if source.collection_filter_id is not None and not has_collection_filter(
                source.collection_filter_id
            ):
                raise ValueError(f"built-in source {source.id} has an unknown collection filter")
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
            for exception in template.duration_policy.exceptions:
                if exception.source_id not in known_sources:
                    raise ValueError(
                        f"playlist template {template.id} duration exception references "
                        f"unknown source {exception.source_id}"
                    )
            if template.retention_hours <= 0 or template.max_episodes <= 0:
                raise ValueError(f"playlist template {template.id} has invalid retention policy")

    def playlist(self, template_id: PlaylistId | str) -> PlaylistTemplate:
        for playlist in self.playlists:
            if playlist.id == template_id:
                return playlist
        raise KeyError(f"unknown playlist template: {template_id}")


BUILTIN_SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        id=SourceId("ser"),
        origin=SourceOrigin.country(CountryCode("ES")),
        display_name="Cadena SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        editorial_scope=EditorialScope.NATIONAL,
        external_references=(ExternalReference("spotify", "show", "4EwwdoHHYmbt49UXODQMpi"),),
    ),
    SourceDefinition(
        id=SourceId("rne"),
        origin=SourceOrigin.country(CountryCode("ES")),
        display_name="Radio Nacional de España",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("rne"),
        editorial_scope=EditorialScope.NATIONAL,
        external_references=(ExternalReference("spotify", "show", "0UgidTKsoaHiHDARuPQNW1"),),
    ),
    SourceDefinition(
        id=SourceId("ondacero"),
        origin=SourceOrigin.country(CountryCode("ES")),
        display_name="Onda Cero",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ondacero"),
        editorial_scope=EditorialScope.NATIONAL,
        external_references=(ExternalReference("spotify", "show", "0tjEexypyczHXW9vE3SU3P"),),
    ),
    SourceDefinition(
        id=SourceId("abc"),
        origin=SourceOrigin.country(CountryCode("ES")),
        display_name="ABC — Las Noticias de ABC",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        editorial_scope=EditorialScope.NATIONAL,
        external_references=(ExternalReference("spotify", "show", "0cLJl7pvrr1bkUaKiVRggf"),),
    ),
    SourceDefinition(
        id=SourceId("cnn"),
        origin=SourceOrigin.country(CountryCode("US")),
        display_name="CNN 5 Cosas",
        countries=(CountryCode("US"),),
        languages=(LanguageTag("es-ES"),),
        timezone=ZoneInfo("America/New_York"),
        enabled=True,
        parser_id=ParserId("cnn"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        external_references=(ExternalReference("spotify", "show", "0vDgnorbpBr65YZzFVVouE"),),
    ),
    SourceDefinition(
        id=SourceId("un_news_en"),
        origin=SourceOrigin.institution("UN"),
        display_name="United Nations — UN News Today",
        countries=(),
        languages=(LanguageTag("en"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        external_references=(ExternalReference("spotify", "show", "3bCQUj5TafN5ichR8gXpFI"),),
    ),
    SourceDefinition(
        id=SourceId("reuters_world"),
        origin=SourceOrigin.country(CountryCode("GB")),
        display_name="Reuters — World News",
        countries=(CountryCode("GB"),),
        languages=(LanguageTag("en"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        external_references=(ExternalReference("spotify", "show", "1alpjXkCUjn3Y9fR5xl8fZ"),),
    ),
    SourceDefinition(
        id=SourceId("nplus_univision"),
        origin=SourceOrigin.country(CountryCode("US")),
        display_name="N+ Univision 24-7",
        countries=(CountryCode("US"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        external_references=(ExternalReference("spotify", "show", "7G8CEhjsTshZeGtPLcuW6T"),),
    ),
    SourceDefinition(
        id=SourceId("dw_actualidad"),
        origin=SourceOrigin.country(CountryCode("DE")),
        display_name="DW — Actualidad en análisis",
        countries=(CountryCode("DE"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Berlin"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        external_references=(ExternalReference("spotify", "show", "7CzHDusNXRICUXuefIXbxd"),),
    ),
    SourceDefinition(
        id=SourceId("un_news_es"),
        origin=SourceOrigin.institution("UN"),
        display_name="United Nations — ONU en minutos",
        countries=(),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("America/New_York"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        collection_filter_id="un_news_es_minutes",
        editorial_scope=EditorialScope.INTERNATIONAL,
        external_references=(ExternalReference("spotify", "show", "77hGWK2o0NYsdS8WuXiLo6"),),
    ),
    SourceDefinition(
        id=SourceId("rfi_fr"),
        origin=SourceOrigin.country(CountryCode("FR")),
        display_name="RFI — Journal Monde",
        countries=(CountryCode("FR"),),
        languages=(LanguageTag("fr"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("rfi_journal_monde"),
        editorial_scope=EditorialScope.INTERNATIONAL,
        external_references=(ExternalReference("spotify", "show", "6y3v3GWUBwANr9hK9m1frF"),),
    ),
    SourceDefinition(
        id=SourceId("dlf_news"),
        origin=SourceOrigin.country(CountryCode("DE")),
        display_name="Deutschlandfunk — Die Nachrichten",
        countries=(CountryCode("DE"),),
        languages=(LanguageTag("de"),),
        timezone=ZoneInfo("Europe/Berlin"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        editorial_scope=EditorialScope.MIXED,
        external_references=(ExternalReference("spotify", "show", "4eYPgoQH9VLTfgAxIbwHqs"),),
    ),
    SourceDefinition(
        id=SourceId("rmf_fakty"),
        origin=SourceOrigin.country(CountryCode("PL")),
        display_name="RMF FM — Fakty",
        countries=(CountryCode("PL"),),
        languages=(LanguageTag("pl"),),
        timezone=ZoneInfo("Europe/Warsaw"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        editorial_scope=EditorialScope.MIXED,
        external_references=(ExternalReference("spotify", "show", "2BeZqsvzZUSldlZWvjeGe3"),),
    ),
)

BUILTIN_PLAYLISTS: tuple[PlaylistTemplate, ...] = (
    PlaylistTemplate(
        id=PlaylistId("spain_spanish_news"),
        display_name="Noticias en Español",
        description=(
            "Últimos boletines y resúmenes informativos de España en español, "
            "actualizados automáticamente y ordenados del más reciente al más antiguo."
        ),
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        default_source_ids=(
            SourceId("ser"),
            SourceId("rne"),
            SourceId("ondacero"),
            SourceId("abc"),
        ),
        cover_id="spain_spanish_news",
    ),
    PlaylistTemplate(
        id=PlaylistId("international_spanish_news"),
        display_name="Noticias Internacional · ES",
        description=(
            "Boletines breves de actualidad internacional y alcance global en español, "
            "actualizados automáticamente y ordenados del más reciente al más antiguo."
        ),
        countries=(CountryCode("US"),),
        languages=(LanguageTag("es-ES"),),
        default_source_ids=(SourceId("cnn"),),
        cover_id="international_spanish_news",
    ),
    PlaylistTemplate(
        id=PlaylistId("international_english_news"),
        display_name="International News · EN",
        description=(
            "Concise international news bulletins with global scope, updated automatically "
            "and ordered newest first."
        ),
        countries=(CountryCode("US"),),
        languages=(LanguageTag("en"),),
        default_source_ids=(SourceId("un_news_en"), SourceId("reuters_world")),
        cover_id="international_english_news",
    ),
    PlaylistTemplate(
        id=PlaylistId("international_french_news"),
        display_name="Actualités internationales · FR",
        description=(
            "Journaux et bulletins concis d’actualité internationale, mis à jour "
            "automatiquement et classés du plus récent au plus ancien."
        ),
        countries=(CountryCode("FR"),),
        languages=(LanguageTag("fr"),),
        default_source_ids=(SourceId("rfi_fr"),),
        cover_id="international_french_news",
    ),
    PlaylistTemplate(
        id=PlaylistId("international_german_news"),
        display_name="Internationale Nachrichten · DE",
        description=(
            "Kompakte Nachrichten aus Deutschland und der Welt, automatisch aktualisiert "
            "und nach Aktualität sortiert."
        ),
        countries=(CountryCode("DE"),),
        languages=(LanguageTag("de"),),
        default_source_ids=(SourceId("dlf_news"),),
        cover_id="international_german_news",
    ),
    PlaylistTemplate(
        id=PlaylistId("international_polish_news"),
        display_name="Wiadomości międzynarodowe · PL",
        description=(
            "Krótkie serwisy informacyjne z wiadomościami z kraju i ze świata, automatycznie "
            "aktualizowane i uporządkowane od najnowszych."
        ),
        countries=(CountryCode("PL"),),
        languages=(LanguageTag("pl"),),
        default_source_ids=(SourceId("rmf_fakty"),),
        cover_id="international_polish_news",
    ),
)

BUILTIN_CATALOG = BuiltInCatalog(sources=BUILTIN_SOURCES, playlists=BUILTIN_PLAYLISTS)
