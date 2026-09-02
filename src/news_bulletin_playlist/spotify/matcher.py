from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from news_bulletin_playlist.models import CanonicalEdition, SourceDefinition, SourceId
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.registry import get_title_parser

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 2
DEFAULT_RETRY_GRACE = timedelta(minutes=15)

_WHITESPACE = re.compile(r"\s+")
_RELEASE_PRECISIONS = {"year", "month", "day"}
_EPISODE_URI_PREFIX = "spotify:episode:"
_REJECTION_REASON_ORDER = (
    "title_parse_failed",
    "semantic_time_mismatch",
    "release_date_skew_rejected",
    "release_date_mismatch",
    "title_mismatch",
)


class SpotifyCatalogClient(Protocol):
    """Small catalogue surface required by the production matcher."""

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]: ...


class MatchConfigurationError(RuntimeError):
    """Raised when a source cannot be matched from its configured catalogue metadata."""


class MatchResponseError(RuntimeError):
    """Raised when Spotify returns a structurally invalid catalogue response."""


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    edition: CanonicalEdition
    status: MatchStatus
    spotify_episode_uri: str | None
    diagnostics: str
    from_cache: bool = False


@dataclass(frozen=True, slots=True)
class SourceMatchResult:
    source_id: SourceId
    outcomes: tuple[MatchOutcome, ...]
    catalogue_calls: int


@dataclass(frozen=True, slots=True)
class _SpotifyEpisodeCandidate:
    uri: str
    name: str
    release_date: str
    release_date_precision: str
    duration_seconds: int | None


@dataclass(frozen=True, slots=True)
class _CandidateEvaluation:
    candidate: _SpotifyEpisodeCandidate
    matched: bool
    reason: str
    release_delay_days: int = 0


def match_source_editions(
    client: SpotifyCatalogClient,
    store: SQLiteStore,
    source: SourceDefinition,
    editions: Sequence[CanonicalEdition],
    *,
    now: datetime,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    retry_grace: timedelta = DEFAULT_RETRY_GRACE,
) -> SourceMatchResult:
    """Match one source batch using persisted state before bounded Spotify catalogue reads."""
    if not 1 <= page_size <= 50:
        raise ValueError("page_size must be between 1 and 50")
    if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
        raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
    if retry_grace < timedelta(0):
        raise ValueError("retry_grace must not be negative")

    observed_at = _as_utc(now)
    outcomes: list[MatchOutcome | None] = [None] * len(editions)
    unresolved: list[tuple[int, CanonicalEdition]] = []

    for index, edition in enumerate(editions):
        if edition.source_id != source.id:
            raise ValueError(
                f"edition source_id {edition.source_id!s} does not match source {source.id!s}"
            )
        cached = _cached_outcome(store, edition, now=observed_at, retry_grace=retry_grace)
        if cached is None:
            unresolved.append((index, edition))
        else:
            outcomes[index] = cached

    if not unresolved:
        return SourceMatchResult(
            source_id=source.id,
            outcomes=tuple(_require_outcome(outcome) for outcome in outcomes),
            catalogue_calls=0,
        )

    show_id = _spotify_show_id(source)
    candidates, catalogue_calls = _fetch_candidates(
        client,
        show_id,
        page_size=page_size,
        max_pages=max_pages,
    )

    for index, edition in unresolved:
        evaluations = tuple(
            _evaluate_candidate(source, edition, candidate) for candidate in candidates
        )
        outcome = _resolve_outcome(
            edition,
            evaluations,
            candidate_count=len(candidates),
            catalogue_calls=catalogue_calls,
        )
        store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=outcome.status,
            spotify_episode_uri=outcome.spotify_episode_uri,
            diagnostics=outcome.diagnostics,
            updated_at=observed_at,
        )
        outcomes[index] = outcome

    return SourceMatchResult(
        source_id=source.id,
        outcomes=tuple(_require_outcome(outcome) for outcome in outcomes),
        catalogue_calls=catalogue_calls,
    )


def _cached_outcome(
    store: SQLiteStore,
    edition: CanonicalEdition,
    *,
    now: datetime,
    retry_grace: timedelta,
) -> MatchOutcome | None:
    state = store.get_match_state(edition.source_id, edition.source_native_id)
    if state is None:
        return None

    if state.status is MatchStatus.MATCHED and state.spotify_episode_uri:
        return MatchOutcome(
            edition=edition,
            status=state.status,
            spotify_episode_uri=state.spotify_episode_uri,
            diagnostics=state.diagnostics or "reused persisted Spotify mapping",
            from_cache=True,
        )

    if state.status in {MatchStatus.PENDING, MatchStatus.AMBIGUOUS}:
        age = now - state.updated_at
        if age < timedelta(0):
            return MatchOutcome(
                edition=edition,
                status=state.status,
                spotify_episode_uri=None,
                diagnostics=(
                    state.diagnostics
                    or "reused persisted state because its timestamp is ahead of matcher clock"
                ),
                from_cache=True,
            )
        if age <= retry_grace:
            return MatchOutcome(
                edition=edition,
                status=state.status,
                spotify_episode_uri=None,
                diagnostics=state.diagnostics or "reused persisted retry-grace state",
                from_cache=True,
            )
    return None


def _spotify_show_id(source: SourceDefinition) -> str:
    show_ids = {
        reference.external_id.strip()
        for reference in source.external_references
        if reference.system.casefold() == "spotify"
        and reference.resource_type.casefold() == "show"
        and reference.external_id.strip()
    }
    if len(show_ids) != 1:
        raise MatchConfigurationError(
            f"source {source.id!s} requires exactly one Spotify show reference"
        )
    return next(iter(show_ids))


def _fetch_candidates(
    client: SpotifyCatalogClient,
    show_id: str,
    *,
    page_size: int,
    max_pages: int,
) -> tuple[tuple[_SpotifyEpisodeCandidate, ...], int]:
    by_uri: dict[str, _SpotifyEpisodeCandidate] = {}
    calls = 0
    for page_number in range(max_pages):
        page = client.show_episodes(
            show_id,
            limit=page_size,
            offset=page_number * page_size,
        )
        calls += 1
        if not isinstance(page, dict):
            raise MatchResponseError("Spotify show episodes response was not an object")
        items = page.get("items")
        if not isinstance(items, list):
            raise MatchResponseError("Spotify show episodes response did not contain an item list")
        if "next" not in page:
            raise MatchResponseError("Spotify show episodes response did not contain next")
        next_page = page["next"]
        if next_page is not None and (
            not isinstance(next_page, str) or not next_page.strip()
        ):
            raise MatchResponseError("Spotify show episodes response contained invalid next")

        for item in items:
            candidate = _candidate_from_item(item)
            existing = by_uri.get(candidate.uri)
            if existing is None:
                by_uri[candidate.uri] = candidate
            elif existing != candidate:
                raise MatchResponseError(
                    "Spotify catalogue repeated an episode URI with conflicting metadata"
                )

        if not items or next_page is None:
            break
    return tuple(by_uri.values()), calls


def _candidate_from_item(item: object) -> _SpotifyEpisodeCandidate:
    if not isinstance(item, dict):
        raise MatchResponseError("Spotify episode item was not an object")

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise MatchResponseError("Spotify episode item contained invalid name")

    release_date = item.get("release_date")
    if not isinstance(release_date, str) or not release_date.strip():
        raise MatchResponseError("Spotify episode item contained invalid release_date")
    release_date = release_date.strip()

    precision = item.get("release_date_precision")
    if not isinstance(precision, str) or precision not in _RELEASE_PRECISIONS:
        raise MatchResponseError(
            "Spotify episode item contained invalid release_date_precision"
        )
    _validate_release_date(release_date, precision)

    uri = item.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise MatchResponseError("Spotify episode item contained invalid uri")
    uri = uri.strip()
    if not uri.startswith(_EPISODE_URI_PREFIX) or len(uri) == len(_EPISODE_URI_PREFIX):
        raise MatchResponseError("Spotify episode item contained a non-episode uri")

    duration_ms = item.get("duration_ms")
    duration_seconds = None
    if isinstance(duration_ms, int) and not isinstance(duration_ms, bool) and duration_ms >= 0:
        duration_seconds = round(duration_ms / 1000)

    return _SpotifyEpisodeCandidate(
        uri=uri,
        name=name.strip(),
        release_date=release_date,
        release_date_precision=precision,
        duration_seconds=duration_seconds,
    )


def _validate_release_date(value: str, precision: str) -> None:
    try:
        if precision == "year":
            if len(value) != 4:
                raise ValueError
            year = int(value)
            date(year, 1, 1)
            return
        if precision == "month":
            if len(value) != 7:
                raise ValueError
            year_s, month_s = value.split("-", 1)
            date(int(year_s), int(month_s), 1)
            return
        date.fromisoformat(value)
    except ValueError as exc:
        raise MatchResponseError(
            "Spotify episode item contained release_date incompatible with its precision"
        ) from exc


def _evaluate_candidate(
    source: SourceDefinition,
    edition: CanonicalEdition,
    candidate: _SpotifyEpisodeCandidate,
) -> _CandidateEvaluation:
    if edition.edition_at is None:
        if not _release_date_compatible(source, edition, candidate):
            return _CandidateEvaluation(candidate, False, "release_date_mismatch")
        if _normalize_title(candidate.name) != _normalize_title(edition.title):
            return _CandidateEvaluation(candidate, False, "title_mismatch")
        return _CandidateEvaluation(candidate, True, "matched")

    parser = get_title_parser(str(source.parser_id))
    parsed = parser.parse(candidate.name)
    if parsed is None:
        return _CandidateEvaluation(candidate, False, "title_parse_failed")

    candidate_edition_at = (
        parsed.edition_at.replace(tzinfo=None)
        .replace(tzinfo=source.timezone)
        .astimezone(UTC)
    )
    if candidate_edition_at != edition.edition_at:
        return _CandidateEvaluation(candidate, False, "semantic_time_mismatch")

    release_delay_days = _semantic_release_delay_days(source, edition, candidate)
    if release_delay_days is None:
        return _CandidateEvaluation(candidate, False, "release_date_skew_rejected")
    if release_delay_days:
        return _CandidateEvaluation(
            candidate,
            True,
            "delayed_release_matched",
            release_delay_days=release_delay_days,
        )
    return _CandidateEvaluation(candidate, True, "matched")


def _semantic_release_delay_days(
    source: SourceDefinition,
    edition: CanonicalEdition,
    candidate: _SpotifyEpisodeCandidate,
) -> int | None:
    if edition.edition_at is None:
        raise ValueError("semantic release delay requires edition_at")

    # Coarse month/year release dates cannot prove a bounded day-level publication delay.
    # Keep their existing strict calendar compatibility.
    if candidate.release_date_precision != "day":
        return 0 if _release_date_compatible(source, edition, candidate) else None

    target = edition.edition_at.astimezone(source.timezone).date()
    candidate_date = date.fromisoformat(candidate.release_date)
    delay_days = (candidate_date - target).days
    if 0 <= delay_days <= source.spotify_release_delay_days:
        return delay_days
    return None


def _release_date_compatible(
    source: SourceDefinition,
    edition: CanonicalEdition,
    candidate: _SpotifyEpisodeCandidate,
) -> bool:
    target = (edition.edition_at or edition.published_at).astimezone(source.timezone).date()
    value = candidate.release_date
    precision = candidate.release_date_precision

    if precision == "year":
        return int(value) == target.year
    if precision == "month":
        year_s, month_s = value.split("-", 1)
        return (int(year_s), int(month_s)) == (target.year, target.month)
    return date.fromisoformat(value) == target


def _resolve_outcome(
    edition: CanonicalEdition,
    evaluations: Sequence[_CandidateEvaluation],
    *,
    candidate_count: int,
    catalogue_calls: int,
) -> MatchOutcome:
    viable = tuple(evaluation for evaluation in evaluations if evaluation.matched)

    if len(viable) == 1:
        evaluation = viable[0]
        candidate = evaluation.candidate
        duration_detail = _duration_diagnostic(edition, candidate)
        release_detail = _release_delay_diagnostic(evaluation)
        return MatchOutcome(
            edition=edition,
            status=MatchStatus.MATCHED,
            spotify_episode_uri=candidate.uri,
            diagnostics=(
                f"matched 1 of {candidate_count} candidate(s) in {catalogue_calls} call(s)"
                f"{release_detail}{duration_detail}"
            ),
        )
    if len(viable) > 1:
        uris = ", ".join(evaluation.candidate.uri for evaluation in viable[:3])
        suffix = ", ..." if len(viable) > 3 else ""
        return MatchOutcome(
            edition=edition,
            status=MatchStatus.AMBIGUOUS,
            spotify_episode_uri=None,
            diagnostics=(
                f"{len(viable)} viable Spotify candidates in known show: {uris}{suffix}"
            ),
        )

    if candidate_count == 0:
        diagnostics = f"no Spotify candidates fetched after {catalogue_calls} call(s)"
    else:
        diagnostics = (
            f"no viable Spotify candidate among {candidate_count} episode(s) "
            f"after {catalogue_calls} call(s)"
        )
        rejection_detail = _rejection_diagnostic(evaluations)
        if rejection_detail:
            diagnostics = f"{diagnostics}; {rejection_detail}"

    return MatchOutcome(
        edition=edition,
        status=MatchStatus.PENDING,
        spotify_episode_uri=None,
        diagnostics=diagnostics,
    )


def _release_delay_diagnostic(evaluation: _CandidateEvaluation) -> str:
    if evaluation.reason != "delayed_release_matched":
        return ""
    return (
        f"; delayed Spotify release=+{evaluation.release_delay_days}d accepted by "
        "exact semantic edition time"
    )


def _rejection_diagnostic(evaluations: Sequence[_CandidateEvaluation]) -> str:
    counts = Counter(
        evaluation.reason for evaluation in evaluations if not evaluation.matched
    )
    parts = [
        f"{reason}={counts[reason]}"
        for reason in _REJECTION_REASON_ORDER
        if counts[reason]
    ]
    extras = sorted(reason for reason in counts if reason not in _REJECTION_REASON_ORDER)
    parts.extend(f"{reason}={counts[reason]}" for reason in extras)
    return f"rejections: {', '.join(parts)}" if parts else ""


def _duration_diagnostic(
    edition: CanonicalEdition,
    candidate: _SpotifyEpisodeCandidate,
) -> str:
    if edition.duration_seconds is None or candidate.duration_seconds is None:
        return ""
    delta = abs(edition.duration_seconds - candidate.duration_seconds)
    return f"; duration delta={delta}s (diagnostic only)"


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return _WHITESPACE.sub(" ", normalized)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _require_outcome(outcome: MatchOutcome | None) -> MatchOutcome:
    if outcome is None:
        raise RuntimeError("internal matcher outcome was not populated")
    return outcome
