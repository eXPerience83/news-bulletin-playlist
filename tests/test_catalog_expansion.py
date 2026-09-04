from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.models import SourceId


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
    assert by_id["spain_spanish_news"].default_source_ids == (
        SourceId("ser"),
        SourceId("rne"),
        SourceId("ondacero"),
        SourceId("abc"),
        SourceId("cnn"),
    )
    assert by_id["international_spanish_news"].default_source_ids == (
        SourceId("cnn"),
        SourceId("rfi_es"),
    )


def test_expanded_sources_have_deterministic_spotify_show_references() -> None:
    source_ids = {str(source.id) for source in BUILTIN_CATALOG.sources}
    assert {
        "abc",
        "rfi_es",
        "bbc_world",
        "rfi_fr",
        "dlf_news",
        "rmf_fakty",
    } <= source_ids

    for source_id in (
        "abc",
        "rfi_es",
        "bbc_world",
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
