from pathlib import Path

import pytest

from news_bulletin_playlist.config import ConfigError, load_config, parse_config
from news_bulletin_playlist.models import OrderingPolicy, SourceId

EXAMPLE_CONFIG = Path(__file__).parents[1] / "config" / "news-bulletin-playlist.example.yaml"


def _source(source_id: str = "ser", *, enabled: bool = True) -> dict[str, object]:
    return {
        "id": source_id,
        "display_name": source_id.upper(),
        "countries": ["ES"],
        "languages": ["es"],
        "timezone": "Europe/Madrid",
        "enabled": enabled,
        "parser_id": source_id,
        "endpoint_url": "https://example.com/feed.xml",
    }


def _playlist(
    playlist_id: str = "news", *, enabled: bool = True, sources: list[str] | None = None
) -> dict[str, object]:
    return {
        "id": playlist_id,
        "display_name": "News",
        "description": "Bulletins",
        "countries": ["ES"],
        "languages": ["es"],
        "enabled": enabled,
        "source_selection": {"explicit": ["ser"] if sources is None else sources},
        "destination": {"adapter_id": "spotify", "external_id": f"playlist-{playlist_id}"},
    }


def _config(
    *,
    sources: list[dict[str, object]] | None = None,
    playlists: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sources": [_source()] if sources is None else sources,
        "playlists": [_playlist()] if playlists is None else playlists,
    }


def test_example_config_is_valid_and_uses_real_source_contracts() -> None:
    config = load_config(EXAMPLE_CONFIG)

    assert [source.id for source in config.sources] == [
        "ser",
        "rne",
        "ondacero",
        "abc",
        "cnn",
        "cope",
    ]
    assert config.sources[-1].enabled is False
    assert config.sources[-1].endpoint_url is None
    assert all("example.invalid" not in (source.endpoint_url or "") for source in config.sources)
    assert config.playlists[0].display_name == "Noticias en Español"
    assert config.playlists[0].source_selection.explicit == (
        "ser",
        "rne",
        "ondacero",
        "abc",
        "cnn",
    )
    assert config.playlists[0].duration_policy.default_max_seconds == 1800


def test_playlist_defaults_are_48_hours_100_episodes_and_newest_first() -> None:
    playlist = parse_config(_config()).playlists[0]

    assert playlist.retention_hours == 48
    assert playlist.max_episodes == 100
    assert playlist.ordering is OrderingPolicy.EDITION_AT_DESC


def test_country_and_language_are_independent_metadata() -> None:
    cnn = _source("cnn")
    cnn["countries"] = ["US"]
    playlist = _playlist(sources=["cnn"])
    playlist["countries"] = ["ES"]

    config = parse_config(_config(sources=[cnn], playlists=[playlist]))

    assert config.sources[0].countries == ("US",)
    assert config.playlists[0].countries == ("ES",)
    assert config.playlists[0].source_selection.explicit == (SourceId("cnn"),)


def test_many_playlists_can_share_one_source() -> None:
    config = parse_config(_config(playlists=[_playlist("morning"), _playlist("evening")]))

    assert len(config.sources) == 1
    assert all(item.source_selection.explicit == ("ser",) for item in config.playlists)


@pytest.mark.parametrize("kind", ["source", "playlist"])
def test_duplicate_ids_are_rejected(kind: str) -> None:
    payload = _config()
    if kind == "source":
        payload["sources"] = [_source(), _source()]
    else:
        payload["playlists"] = [_playlist(), _playlist()]

    with pytest.raises(ConfigError, match=f"duplicate {kind} id"):
        parse_config(payload)


def test_unknown_source_reference_is_always_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown source"):
        parse_config(_config(playlists=[_playlist(enabled=False, sources=["missing"])]))


def test_enabled_playlist_cannot_reference_disabled_source() -> None:
    with pytest.raises(ConfigError, match="enabled playlist references disabled source"):
        parse_config(_config(sources=[_source(enabled=False)]))


def test_disabled_playlist_can_reference_existing_disabled_source() -> None:
    config = parse_config(
        _config(sources=[_source(enabled=False)], playlists=[_playlist(enabled=False)])
    )

    assert config.playlists[0].source_selection.explicit == ("ser",)


def test_enabled_playlist_must_have_explicit_source() -> None:
    with pytest.raises(ConfigError, match="enabled playlist must select a source"):
        parse_config(_config(playlists=[_playlist(sources=[])]))


def test_enabled_playlists_cannot_share_one_destination() -> None:
    first = _playlist("first")
    second = _playlist("second")
    second["destination"] = first["destination"]

    with pytest.raises(ConfigError, match="duplicate enabled destination"):
        parse_config(_config(playlists=[first, second]))


def test_disabled_playlist_may_stage_an_enabled_playlist_destination() -> None:
    first = _playlist("first")
    second = _playlist("second", enabled=False)
    second["destination"] = first["destination"]

    config = parse_config(_config(playlists=[first, second]))

    assert config.playlists[0].destination == config.playlists[1].destination


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text("schema_version: 1\nschema_version: 1\nsources: []\nplaylists: []\n")

    with pytest.raises(ConfigError, match="duplicate key"):
        load_config(path)


def test_non_string_yaml_key_is_reported_as_config_error(tmp_path: Path) -> None:
    path = tmp_path / "non-string-key.yaml"
    path.write_text(
        "schema_version: 1\n? []\n: invalid\nsources: []\nplaylists: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="non-string key"):
        load_config(path)


def test_yaml_does_not_coerce_norway_country_or_language_to_boolean(tmp_path: Path) -> None:
    path = tmp_path / "norway.yaml"
    path.write_text(
        """schema_version: 1
sources:
  - id: ser
    display_name: Norwegian test source
    countries: [NO]
    languages: [no]
    timezone: Europe/Oslo
    enabled: true
    parser_id: ser
    endpoint_url: https://example.com/feed.xml
playlists:
  - id: norway_news
    display_name: Norway News
    description: Test
    countries: [NO]
    languages: [no]
    enabled: true
    source_selection:
      explicit: [ser]
    destination:
      adapter_id: spotify
      external_id: playlist-norway
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.sources[0].countries == ("NO",)
    assert config.sources[0].languages == ("no",)
    assert config.playlists[0].countries == ("NO",)
    assert config.playlists[0].languages == ("no",)


def test_unknown_config_key_is_rejected() -> None:
    payload = _config()
    payload["surprise"] = True

    with pytest.raises(ConfigError, match="unknown key 'surprise'"):
        parse_config(payload)


def test_invalid_timezone_is_rejected() -> None:
    source = _source()
    source["timezone"] = "Mars/Olympus"

    with pytest.raises(ConfigError, match="unknown IANA timezone"):
        parse_config(_config(sources=[source]))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://:443/feed.xml",
        "https://example.com:invalid/feed.xml",
        "https://[broken/feed.xml",
    ],
)
def test_malformed_endpoint_url_is_rejected(endpoint: str) -> None:
    source = _source()
    source["endpoint_url"] = endpoint

    with pytest.raises(ConfigError, match=r"HTTP\(S\) URL"):
        parse_config(_config(sources=[source]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("destination", {"adapter_id": "spotify"}),
        ("destination", {"adapter_id": "spotify", "external_id": ""}),
    ],
)
def test_invalid_destination_shape_is_rejected(field: str, value: object) -> None:
    playlist = _playlist()
    playlist[field] = value

    with pytest.raises(ConfigError, match="destination"):
        parse_config(_config(playlists=[playlist]))


def test_invalid_external_reference_shape_is_rejected() -> None:
    source = _source()
    source["external_references"] = [{"system": "spotify", "external_id": "show-id"}]

    with pytest.raises(ConfigError, match="resource_type"):
        parse_config(_config(sources=[source]))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("countries", ["es"], "uppercase ASCII"),
        ("languages", ["ES_419"], "BCP-47"),
    ],
)
def test_country_and_language_shapes_are_conservative(
    field: str, value: object, message: str
) -> None:
    source = _source()
    source[field] = value

    with pytest.raises(ConfigError, match=message):
        parse_config(_config(sources=[source]))
