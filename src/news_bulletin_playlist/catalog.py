"""Built-in, read-only catalog shipped with each application image."""

from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    CountryCode,
    DurationPolicy,
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
            if not source.enabled or source.endpoint_url is None or not source.endpoint_url.strip():
                raise ValueError(
                    f"built-in source {source.id} must be operational and have an endpoint"
                )
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
        external_references=(ExternalReference("spotify", "show", "4EwwdoHHYmbt49UXODQMpi"),),
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
        external_references=(ExternalReference("spotify", "show", "0UgidTKsoaHiHDARuPQNW1"),),
        spotify_release_delay_days=1,
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
        external_references=(ExternalReference("spotify", "show", "0tjEexypyczHXW9vE3SU3P"),),
    ),
    SourceDefinition(
        id=SourceId("abc"),
        display_name="ABC — Las Noticias de ABC",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://omny.fm/shows/las-noticias-de-abc-1/playlists/podcast.rss",
        external_references=(ExternalReference("spotify", "show", "0cLJl7pvrr1bkUaKiVRggf"),),
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
        external_references=(ExternalReference("spotify", "show", "0vDgnorbpBr65YZzFVVouE"),),
    ),
    SourceDefinition(
        id=SourceId("rfi_es"),
        display_name="RFI Español — Noticias de América",
        countries=(CountryCode("FR"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url=(
            "https://apis.rfi.fr/products/get_product/rfi_getpodcast_by_nid?"
            "token_application=975d23b8-7a07-11e8-9f62-005056a90194&"
            "program.entrepriseId=WB82694-RFI-ES-20110404"
        ),
        external_references=(ExternalReference("spotify", "show", "05TcT18Dh30a0O7oxRZ3e3"),),
    ),
    SourceDefinition(
        id=SourceId("bbc_world"),
        display_name="BBC World Service — Global News Podcast",
        countries=(CountryCode("GB"),),
        languages=(LanguageTag("en"),),
        timezone=ZoneInfo("Europe/London"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://podcasts.files.bbci.co.uk/p02nq0gn.rss",
        external_references=(ExternalReference("spotify", "show", "3wBfqov60qDZbEVjPHo0a8"),),
    ),
    SourceDefinition(
        id=SourceId("rfi_fr"),
        display_name="RFI — Journal Monde",
        countries=(CountryCode("FR"),),
        languages=(LanguageTag("fr"),),
        timezone=ZoneInfo("UTC"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url=(
            "https://apis.rfi.fr/products/get_product/fov-rfi-fr-get-journaux-podcast-v2-monde?"
            "token_application=975d23b8-7a07-11e8-9f62-005056a90194"
        ),
        external_references=(ExternalReference("spotify", "show", "6y3v3GWUBwANr9hK9m1frF"),),
    ),
    SourceDefinition(
        id=SourceId("dlf_news"),
        display_name="Deutschlandfunk — Die Nachrichten",
        countries=(CountryCode("DE"),),
        languages=(LanguageTag("de"),),
        timezone=ZoneInfo("Europe/Berlin"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://www.deutschlandfunk.de/nachrichten-108.xml",
        external_references=(ExternalReference("spotify", "show", "4eYPgoQH9VLTfgAxIbwHqs"),),
    ),
    SourceDefinition(
        id=SourceId("rmf_fakty"),
        display_name="RMF FM — Fakty",
        countries=(CountryCode("PL"),),
        languages=(LanguageTag("pl"),),
        timezone=ZoneInfo("Europe/Warsaw"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://www.rmf24.pl/podcast/fakty/feed",
        external_references=(ExternalReference("spotify", "show", "2BeZqsvzZUSldlZWvjeGe3"),),
    ),
)

BUILTIN_PLAYLISTS: tuple[PlaylistTemplate, ...] = (
    PlaylistTemplate(
        id=PlaylistId("spain_spanish_news"),
        display_name="Noticias en Español",
        description=(
            "Últimos boletines y resúmenes informativos en español, con fuentes de España "
            "y una selección internacional, actualizados automáticamente y ordenados "
            "del más reciente al más antiguo."
        ),
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        default_source_ids=(
            SourceId("ser"),
            SourceId("rne"),
            SourceId("ondacero"),
            SourceId("abc"),
            SourceId("cnn"),
        ),
        cover_id="spain_spanish_news",
    ),
    PlaylistTemplate(
        id=PlaylistId("international_spanish_news"),
        display_name="Noticias Internacional · ES",
        description=(
            "Últimos boletines y resúmenes de actualidad internacional en español, "
            "actualizados automáticamente y ordenados del más reciente al más antiguo."
        ),
        countries=(CountryCode("US"), CountryCode("FR")),
        languages=(LanguageTag("es"),),
        default_source_ids=(SourceId("cnn"), SourceId("rfi_es")),
        cover_id="international_spanish_news",
    ),
    PlaylistTemplate(
        id=PlaylistId("international_english_news"),
        display_name="International News · EN",
        description=(
            "Latest international news bulletins and briefings, updated automatically "
            "and ordered newest first."
        ),
        countries=(CountryCode("GB"),),
        languages=(LanguageTag("en"),),
        default_source_ids=(SourceId("bbc_world"),),
        cover_id="international_english_news",
    ),
    PlaylistTemplate(
        id=PlaylistId("international_french_news"),
        display_name="Actualités internationales · FR",
        description=(
            "Derniers journaux et bulletins d’actualité internationale, mis à jour "
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
            "Aktuelle internationale Nachrichten und Bulletins, automatisch aktualisiert "
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
            "Najnowsze serwisy informacyjne i wiadomości międzynarodowe, automatycznie "
            "aktualizowane i uporządkowane od najnowszych."
        ),
        countries=(CountryCode("PL"),),
        languages=(LanguageTag("pl"),),
        default_source_ids=(SourceId("rmf_fakty"),),
        cover_id="international_polish_news",
    ),
)

BUILTIN_CATALOG = BuiltInCatalog(sources=BUILTIN_SOURCES, playlists=BUILTIN_PLAYLISTS)
