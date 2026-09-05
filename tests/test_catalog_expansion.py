from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.models import EditorialScope, SourceId


def test_builtin_catalog_exposes_phase_one_playlist_templates() -> None:
    by_id = {str(template.id): template for template in BUILTIN_CATALOG.playlists}

    assert set(by_id) == {
        "spain_spanish_news",
        "international_spanish_news",
        "international_english_news",
        "international_french_news",
        "international_german_news",
        "international_polish_news",
    }
    assert all(template.duration_policy.default_max_seconds == 1800 for template in by_id.values())
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


def test_builtin_sources_expose_origin_scope_and_language() -> None:
    ser = BUILTIN_CATALOG.source("ser")
    cnn = BUILTIN_CATALOG.source("cnn")
    dlf = BUILTIN_CATALOG.source("dlf_news")

    assert tuple(map(str, ser.countries)) == ("ES",)
    assert ser.editorial_scope is EditorialScope.NATIONAL
    assert tuple(map(str, ser.languages)) == ("es-ES",)

    assert tuple(map(str, cnn.countries)) == ("US",)
    assert cnn.editorial_scope is EditorialScope.INTERNATIONAL
    assert tuple(map(str, cnn.languages)) == ("es-ES",)

    assert tuple(map(str, dlf.countries)) == ("DE",)
    assert dlf.editorial_scope is EditorialScope.MIXED
    assert tuple(map(str, dlf.languages)) == ("de",)


def test_expanded_sources_have_deterministic_spotify_show_references() -> None:
    source_ids = {str(source.id) for source in BUILTIN_CATALOG.sources}
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
    assert {
        "reuters_world",
        "cbc_world_report",
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
        source = BUILTIN_CATALOG.source(source_id)
        spotify_refs = tuple(
            ref
            for ref in source.external_references
            if ref.system == "spotify" and ref.resource_type == "show"
        )
        assert len(spotify_refs) == 1
        assert source.endpoint_url


def test_wave_one_source_identity_and_unresolved_spanish_classification() -> None:
    expected = {
        "reuters_world": ("GB", "INT", "en", "1alpjXkCUjn3Y9fR5xl8fZ"),
        "cbc_world_report": ("CA", "MIX", "en", "5qaYz2SRxlPUszXZQWNl1U"),
        "nplus_univision": ("US", "INT", "es", "7G8CEhjsTshZeGtPLcuW6T"),
        "dw_actualidad": ("DE", "INT", "es", "7CzHDusNXRICUXuefIXbxd"),
        "un_news_es": ("US", "INT", "es", "77hGWK2o0NYsdS8WuXiLo6"),
    }
    for source_id, (country, scope, language, show_id) in expected.items():
        source = BUILTIN_CATALOG.source(source_id)
        assert tuple(map(str, source.countries)) == (country,)
        assert source.editorial_scope.value == scope
        assert tuple(map(str, source.languages)) == (language,)
        assert source.parser_id == "release_date_title"
        assert any(reference.external_id == show_id for reference in source.external_references)

    assert BUILTIN_CATALOG.source("un_news_es").collection_filter_id == "un_news_es_minutes"
