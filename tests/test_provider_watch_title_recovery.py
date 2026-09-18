from news_bulletin_playlist.provider_watch import evaluate_titles, extract_fallback_titles
from news_bulletin_playlist.providers.ondacero import OndaCeroParser


def test_ondacero_fallback_returns_complete_canonical_title_unit() -> None:
    title = "Las noticias de Onda Cero de las 10:00h (17/09/2026)"
    payload = f"<html><body><h3>{title}</h3></body></html>".encode()

    assert extract_fallback_titles("ondacero", payload) == [title]


def test_ondacero_fallback_complete_recoverable_variant_reaches_recovery() -> None:
    title = "Las noticias de Onda Cero de las 9.30 H 17-09-26."
    payload = f"<html><body><a><span>{title}</span></a></body></html>".encode()

    titles = extract_fallback_titles("ondacero", payload)
    result = evaluate_titles("ondacero", OndaCeroParser(), titles)

    assert titles == [title]
    assert result.ok is True
    assert result.parsed == 1
    assert "recovered 1/1" in result.detail


def test_ondacero_fallback_does_not_detach_valid_substring_from_promo() -> None:
    title = "Promo: Las noticias de Onda Cero de las 10:00h (17/09/2026) - DEPORTES"
    payload = f"<html><body><h3>{title}</h3></body></html>".encode()

    assert extract_fallback_titles("ondacero", payload) == []


def test_ondacero_fallback_keeps_invalid_suffix_for_parser_to_reject() -> None:
    title = "Las noticias de Onda Cero de las 10:00h (17/09/2026) - DEPORTES"
    payload = f"<html><body><h3>{title}</h3></body></html>".encode()

    titles = extract_fallback_titles("ondacero", payload)
    result = evaluate_titles("ondacero", OndaCeroParser(), titles)

    assert titles == [title]
    assert result.ok is False
    assert result.parsed == 0
    assert "unresolved 1/1" in result.detail


def test_ondacero_fallback_does_not_emit_nested_fragment_as_separate_title() -> None:
    canonical_fragment = "Las noticias de Onda Cero de las 10:00h (17/09/2026)"
    complete_title = f"{canonical_fragment} - DEPORTES"
    payload = (
        f"<html><body><h3><span>{canonical_fragment}</span> - DEPORTES</h3></body></html>"
    ).encode()

    titles = extract_fallback_titles("ondacero", payload)
    result = evaluate_titles("ondacero", OndaCeroParser(), titles)

    assert titles == [complete_title]
    assert result.ok is False
    assert result.parsed == 0
    assert "unresolved 1/1" in result.detail


def test_ondacero_fallback_does_not_promote_link_from_semantic_title_unit() -> None:
    canonical_fragment = "Las noticias de Onda Cero de las 09:30h (17/09/2026)"
    payload = (
        "<html><body><h3>Promo: "
        f'<a href="/promo">{canonical_fragment}</a>'
        " - DEPORTES</h3></body></html>"
    ).encode()

    assert extract_fallback_titles("ondacero", payload) == []


def test_ondacero_fallback_does_not_promote_span_from_inline_wrapper() -> None:
    canonical_fragment = "Las noticias de Onda Cero de las 09:30h (17/09/2026)"
    payload = (
        "<html><body><strong>Promo: "
        f"<span>{canonical_fragment}</span>"
        " - DEPORTES</strong></body></html>"
    ).encode()

    assert extract_fallback_titles("ondacero", payload) == []


def test_ondacero_fallback_uses_complete_heading_instead_of_nested_link() -> None:
    canonical_fragment = "Las noticias de Onda Cero de las 09:30h (17/09/2026)"
    complete_title = f"{canonical_fragment} - DEPORTES"
    payload = (
        f'<html><body><h3><a href="/promo">{canonical_fragment}</a> - DEPORTES</h3></body></html>'
    ).encode()

    titles = extract_fallback_titles("ondacero", payload)
    result = evaluate_titles("ondacero", OndaCeroParser(), titles)

    assert titles == [complete_title]
    assert result.ok is False
    assert result.parsed == 0
    assert "unresolved 1/1" in result.detail
