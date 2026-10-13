from dataclasses import replace
from pathlib import Path

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_state import (
    ManagedState,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import (
    CountryCode,
    EditorialScope,
    SourceId,
    SourceOrigin,
    SourceOriginKind,
)


def test_builtin_catalog_exposes_phase_one_playlist_templates() -> None:
    by_id = {str(template.id): template for template in BUILTIN_CATALOG.playlists}

    assert set(by_id) == {
        "spain_spanish_news",
        "international_spanish_news",
        "international_english_news",
        "international_french_news",
        "international_german_news",
        "international_polish_news",
        "international_spanish_long_news",
    }
    assert all(
        template.duration_policy.default_max_seconds == 1800
        for template_id, template in by_id.items()
        if template_id != "international_spanish_long_news"
    )
    assert by_id["spain_spanish_news"].display_name == "Noticias en Español"
    assert by_id["spain_spanish_news"].default_source_ids == (
        SourceId("ser"),
        SourceId("rne"),
        SourceId("ondacero"),
        SourceId("abc"),
    )
    assert tuple(map(str, by_id["spain_spanish_news"].languages)) == ("es-ES",)
    assert by_id["international_spanish_news"].default_source_ids == (SourceId("cnn"),)
    assert tuple(map(str, by_id["international_spanish_news"].languages)) == ("es-ES",)
    assert by_id["international_english_news"].default_source_ids == (
        SourceId("un_news_en"),
        SourceId("reuters_world"),
    )


def test_long_news_template_has_reviewed_independent_policy_and_bundled_cover() -> None:
    template = BUILTIN_CATALOG.playlist("international_spanish_long_news")
    assert template.display_name == "Actualidad en profundidad · ES"
    assert template.countries == ()
    assert tuple(map(str, template.languages)) == ("es",)
    assert template.default_source_ids == (
        SourceId("dw_actualidad"),
        SourceId("nplus_univision"),
    )
    assert template.retention_hours == 96
    assert template.max_episodes == 100
    assert template.duration_policy.default_min_seconds == 600
    assert template.duration_policy.default_max_seconds == 2100
    assert template.duration_policy.exceptions == ()
    assert template.cover_id == "international_spanish_long_news"
    assert len(template.description) <= 190

    root = Path(__file__).resolve().parents[1] / "assets" / "covers"
    master = root / "master" / f"{template.cover_id}.svg"
    jpeg = root / "spotify" / f"{template.cover_id}.jpg"
    assert master.is_file()
    payload = jpeg.read_bytes()
    assert payload[:2] == bytes((0xFF, 0xD8)) and payload[-2:] == bytes((0xFF, 0xD9))
    assert len(payload) <= 256 * 1024

    activated = activate_template(template, "test-only-destination")
    assert activated.source_ids == template.default_source_ids
    assert activated.retention_hours == 96
    assert activated.min_duration_seconds == 600
    assert activated.max_duration_seconds == 2100
    compiled = compile_engine_config(BUILTIN_CATALOG, ManagedState(playlists=(activated,)))
    assert compiled.playlists[0].duration_policy == template.duration_policy


def test_builtin_sources_expose_origin_scope_and_language() -> None:
    sources = {str(source.id): source for source in BUILTIN_CATALOG.sources}
    ser = sources["ser"]
    cnn = sources["cnn"]
    dlf = sources["dlf_news"]

    assert ser.origin == SourceOrigin.country(CountryCode("ES"))
    assert ser.editorial_scope is EditorialScope.NATIONAL
    assert tuple(map(str, ser.languages)) == ("es-ES",)

    assert cnn.origin == SourceOrigin.country(CountryCode("US"))
    assert cnn.editorial_scope is EditorialScope.INTERNATIONAL
    assert tuple(map(str, cnn.languages)) == ("es-ES",)

    assert dlf.origin == SourceOrigin.country(CountryCode("DE"))
    assert dlf.editorial_scope is EditorialScope.MIXED
    assert tuple(map(str, dlf.languages)) == ("de",)


def test_all_builtin_origins_are_explicit_and_un_sources_have_no_fake_country() -> None:
    expected = {
        "ser": "ES",
        "rne": "ES",
        "ondacero": "ES",
        "abc": "ES",
        "cnn": "US",
        "un_news_en": "UN",
        "reuters_world": "GB",
        "nplus_univision": "US",
        "dw_actualidad": "DE",
        "un_news_es": "UN",
        "rfi_fr": "FR",
        "dlf_news": "DE",
        "rmf_fakty": "PL",
    }
    sources = {str(source.id): source for source in BUILTIN_CATALOG.sources}
    assert set(sources) == set(expected)
    for source_id, code in expected.items():
        source = sources[source_id]
        assert str(source.origin) == code
        if code == "UN":
            assert source.origin.kind is SourceOriginKind.INSTITUTION
            assert source.countries == ()
            assert source.editorial_scope is EditorialScope.INTERNATIONAL
        else:
            assert source.origin.kind is SourceOriginKind.COUNTRY
            assert tuple(map(str, source.countries)) == (code,)


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (SourceOriginKind.COUNTRY, "UNited"),
        (SourceOriginKind.COUNTRY, "us"),
        (SourceOriginKind.COUNTRY, "UN"),
        (SourceOriginKind.INSTITUTION, ""),
        (SourceOriginKind.INSTITUTION, "U N"),
        (SourceOriginKind.INSTITUTION, "éU"),
    ],
)
def test_source_origin_rejects_invalid_codes(kind: SourceOriginKind, code: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        SourceOrigin(kind, code)


def test_source_definition_rejects_missing_origin() -> None:
    with pytest.raises(ValueError, match="source origin"):
        replace(BUILTIN_CATALOG.sources[0], origin=None)


def test_builtin_runtime_identity_and_classification_remain_unchanged() -> None:
    expected = {
        "ser": ("ser", "Europe/Madrid", "es-ES", "NAT", "4EwwdoHHYmbt49UXODQMpi"),
        "rne": ("rne", "Europe/Madrid", "es-ES", "NAT", "0UgidTKsoaHiHDARuPQNW1"),
        "ondacero": ("ondacero", "Europe/Madrid", "es-ES", "NAT", "0tjEexypyczHXW9vE3SU3P"),
        "abc": ("release_date_title", "Europe/Madrid", "es-ES", "NAT", "0cLJl7pvrr1bkUaKiVRggf"),
        "cnn": ("cnn", "America/New_York", "es-ES", "INT", "0vDgnorbpBr65YZzFVVouE"),
        "un_news_en": ("release_date_title", "UTC", "en", "INT", "3bCQUj5TafN5ichR8gXpFI"),
        "reuters_world": ("release_date_title", "UTC", "en", "INT", "1alpjXkCUjn3Y9fR5xl8fZ"),
        "nplus_univision": ("release_date_title", "UTC", "es", "INT", "7G8CEhjsTshZeGtPLcuW6T"),
        "dw_actualidad": (
            "release_date_title",
            "Europe/Berlin",
            "es",
            "INT",
            "7CzHDusNXRICUXuefIXbxd",
        ),
        "un_news_es": (
            "release_date_title",
            "America/New_York",
            "es",
            "INT",
            "77hGWK2o0NYsdS8WuXiLo6",
        ),
        "rfi_fr": ("rfi_journal_monde", "UTC", "fr", "INT", "6y3v3GWUBwANr9hK9m1frF"),
        "dlf_news": ("release_date_title", "Europe/Berlin", "de", "MIX", "4eYPgoQH9VLTfgAxIbwHqs"),
        "rmf_fakty": ("rmf_fakty", "Europe/Warsaw", "pl", "MIX", "2BeZqsvzZUSldlZWvjeGe3"),
    }
    assert {
        str(source.id): (
            str(source.parser_id),
            source.timezone.key,
            ",".join(source.languages),
            source.editorial_scope.value,
            source.external_references[0].external_id,
        )
        for source in BUILTIN_CATALOG.sources
    } == expected
    filters = {
        str(source.id): source.collection_filter_id
        for source in BUILTIN_CATALOG.sources
        if source.collection_filter_id
    }
    assert filters == {"un_news_es": "un_news_es_minutes"}


def test_expanded_sources_have_deterministic_spotify_show_references() -> None:
    sources = {str(source.id): source for source in BUILTIN_CATALOG.sources}
    source_ids = set(sources)
    assert {
        "abc",
        "un_news_en",
        "rfi_fr",
        "dlf_news",
        "rmf_fakty",
    } <= source_ids
    assert "rfi_es" not in source_ids
    assert "bbc_world" not in source_ids
    assert "chequia_30_minutos" not in source_ids
    assert "cbc_world_report" not in source_ids
    assert {
        "reuters_world",
        "nplus_univision",
        "dw_actualidad",
        "un_news_es",
    } <= source_ids

    for source_id in (
        "abc",
        "un_news_en",
        "rfi_fr",
        "dlf_news",
        "rmf_fakty",
    ):
        source = sources[source_id]
        spotify_refs = tuple(
            ref
            for ref in source.external_references
            if ref.system == "spotify" and ref.resource_type == "show"
        )
        assert len(spotify_refs) == 1


def test_wave_one_source_identity_and_unresolved_spanish_classification() -> None:
    sources = {str(source.id): source for source in BUILTIN_CATALOG.sources}
    expected = {
        "reuters_world": ("GB", "INT", "en", "1alpjXkCUjn3Y9fR5xl8fZ"),
        "nplus_univision": ("US", "INT", "es", "7G8CEhjsTshZeGtPLcuW6T"),
        "dw_actualidad": ("DE", "INT", "es", "7CzHDusNXRICUXuefIXbxd"),
        "un_news_es": ("UN", "INT", "es", "77hGWK2o0NYsdS8WuXiLo6"),
    }
    for source_id, (origin, scope, language, show_id) in expected.items():
        source = sources[source_id]
        assert str(source.origin) == origin
        assert source.editorial_scope.value == scope
        assert tuple(map(str, source.languages)) == (language,)
        assert source.parser_id == "release_date_title"
        assert any(reference.external_id == show_id for reference in source.external_references)

    assert sources["un_news_es"].collection_filter_id == "un_news_es_minutes"
