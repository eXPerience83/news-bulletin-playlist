from __future__ import annotations

from news_bulletin_playlist.reconciliation_diagnostics import (
    classify_reconciliation_failure,
)


def test_classifies_safe_spotify_api_and_transport_failures() -> None:
    api = classify_reconciliation_failure("Spotify API 503: request failed")
    assert api.details() == {
        "failure_class": "api_error",
        "phase": "reconciliation",
        "http_status": 503,
        "verification_outcome": "failed",
    }

    transport = classify_reconciliation_failure("Spotify API request failed due to a network error")
    assert transport.details() == {
        "failure_class": "transport_error",
        "phase": "reconciliation",
        "verification_outcome": "failed",
    }


def test_classifies_exact_spotify_operation_phase_without_provider_text() -> None:
    prewrite = classify_reconciliation_failure(
        "Spotify playlist prewrite playlist_items API failure (http_status=503)"
    )
    assert prewrite.details() == {
        "failure_class": "api_error",
        "phase": "prewrite",
        "operation": "playlist_items",
        "http_status": 503,
        "verification_outcome": "failed",
        "write_decision": "blocked",
    }

    write = classify_reconciliation_failure(
        "Spotify playlist write replace_items transport failure"
    )
    assert write.details() == {
        "failure_class": "transport_error",
        "phase": "write",
        "operation": "replace_items",
        "verification_outcome": "failed",
    }

    snapshot = classify_reconciliation_failure(
        "Spotify playlist readback snapshot API failure (http_status=502)"
    )
    assert snapshot.details() == {
        "failure_class": "api_error",
        "phase": "readback",
        "operation": "snapshot",
        "http_status": 502,
        "verification_outcome": "failed",
    }


def test_classifies_pagination_context_without_retaining_next_url() -> None:
    diagnostic = classify_reconciliation_failure(
        "Spotify playlist prewrite response pagination truncated before total "
        "(offset=50 returned=24 total=80 next=null)"
    )

    assert diagnostic.details() == {
        "failure_class": "pagination_error",
        "phase": "prewrite",
        "operation": "playlist_items",
        "offset": 50,
        "returned_count": 24,
        "total": 80,
        "next_state": "null",
        "verification_outcome": "failed",
        "write_decision": "blocked",
    }


def test_classifies_readback_mismatch_and_unavailable_media() -> None:
    mismatch = classify_reconciliation_failure(
        "Spotify playlist readback did not match desired order/count/content "
        "(desired=74 returned=73)"
    )
    assert mismatch.details() == {
        "failure_class": "verification_mismatch",
        "phase": "readback",
        "operation": "playlist_items",
        "returned_count": 73,
        "verification_outcome": "mismatch",
    }

    unavailable = classify_reconciliation_failure(
        "Spotify playlist readback response (offset=50) contained an unavailable media item "
        "(item_index=3)"
    )
    assert unavailable.details() == {
        "failure_class": "unavailable_media",
        "phase": "readback",
        "operation": "playlist_items",
        "offset": 50,
        "unavailable_count": 1,
        "verification_outcome": "unavailable",
    }


def test_unknown_error_collapses_to_generic_classification_without_secret() -> None:
    sentinel = "access-token-sentinel-never-persist"
    diagnostic = classify_reconciliation_failure(f"completely unknown provider failure {sentinel}")

    assert diagnostic.details() == {
        "failure_class": "reconciliation_error",
        "phase": "reconciliation",
        "verification_outcome": "failed",
    }
    assert sentinel not in repr(diagnostic)
    assert sentinel not in repr(diagnostic.details())
