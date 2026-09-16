from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from news_bulletin_playlist.models import CanonicalEdition, SourceDefinition, SourceId
from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore
from news_bulletin_playlist.registry import get_title_parser
from news_bulletin_playlist.spotify.matching_policy import (
    duration_disambiguation_tolerance_seconds,
)
from news_bulletin_playlist.spotify.uris import is_spotify_episode_uri

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 2
DEFAULT_RETRY_GRACE = timedelta(minutes=15)

_WHITESPACE = re.compile(r"\s+")
_RELEASE_PRECISIONS = {"year", "month", "day"}
_REJECTION_REASON_ORDER = (
    "title_parse_failed",
    "semantic_time_mismatch",
    "release_date_skew_rejected",
    "release_date_precision_insufficient",
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


class MatchEvaluationIntent(StrEnum):
    """Reason an existing mapping participates in catalogue evaluation."""

    SEMANTIC_REEVALUATION = "semantic_reevaluation"
    EVIDENCE_RECOVERY = "evidence_recovery"


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    edition: CanonicalEdition
    status: MatchStatus
    spotify_episode_uri: str | None
    diagnostics: str
    spotify_duration_seconds: int | None = None
    from_cache: bool = False
    # None means current evidence is inconclusive. False means the current catalogue actively
    # contradicts the persisted mapping and prior planner evidence must be cleared.
    planner_input_evidence_qualified: bool | None = None
    # True means a semantically-authoritative transition was attempted but SQLite's
    # monotonic updated_at guard rejected it. The returned mapping remains aligned with
    # durable state and Engine must keep planner evidence and destination writes fail-closed.
    persistence_rejected: bool = False


@dataclass(frozen=True, slots=True)
class SourceMatchResult:
    source_id: SourceId
    outcomes: tuple[MatchOutcome, ...]
    catalogue_calls: int
    catalogue_complete: bool = False
    catalogue_candidate_count: int = 0
    catalogue_inspected: bool = False

    @property
    def persistence_rejected(self) -> bool:
        return any(outcome.persistence_rejected for outcome in self.outcomes)


@dataclass(frozen=True, slots=True)
class SpotifyCatalogueResult:
    """One structurally valid bounded show-catalogue inspection."""

    candidates: tuple[_SpotifyEpisodeCandidate, ...]
    request_count: int
    complete: bool
    structurally_valid: bool = True

    @property
    def empty(self) -> bool:
        return not self.candidates

    @property
    def can_prove_absence(self) -> bool:
        return self.structurally_valid and self.complete and not self.empty

    @property
    def candidate_uris(self) -> frozenset[str]:
        return frozenset(candidate.uri for candidate in self.candidates)


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


@dataclass(frozen=True, slots=True)
class _CachedMappingAssessment:
    evaluated: MatchOutcome
    qualification: bool | None
    recovery_repair_allowed: bool


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
    catalogue: SpotifyCatalogueResult | None = None,
    evaluation_intents: Mapping[tuple[SourceId, str], MatchEvaluationIntent] | None = None,
    validate_cached: bool = True,
    catalogue_request_count: int | None = None,
    on_fresh_evaluation: Callable[[MatchOutcome], None] | None = None,
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
    selected = None if evaluation_intents is None else frozenset(evaluation_intents)
    notified_fresh_indices: set[int] = set()
    duration_tolerance = duration_disambiguation_tolerance_seconds(source)
    qualification_overrides: dict[int, bool | None] = {}

    for index, edition in enumerate(editions):
        if edition.source_id != source.id:
            raise ValueError(
                f"edition source_id {edition.source_id!s} does not match source {source.id!s}"
            )
        cached = _cached_outcome(store, edition, now=observed_at, retry_grace=retry_grace)
        if selected is not None and edition.identity not in selected and cached is None:
            cached = _persisted_outcome(store, edition, now=observed_at)
        if (
            selected is not None
            and edition.identity in selected
            and (cached is None or cached.status is not MatchStatus.MATCHED)
        ):
            cached = None
        if cached is None:
            unresolved.append((index, edition))
        else:
            outcomes[index] = cached

    cached_matched = [
        (index, outcome)
        for index, outcome in enumerate(outcomes)
        if outcome is not None and outcome.status is MatchStatus.MATCHED
    ]
    need_catalogue = bool(unresolved or (validate_cached and cached_matched))
    if not need_catalogue and catalogue is None:
        return SourceMatchResult(
            source_id=source.id,
            outcomes=tuple(_require_outcome(outcome) for outcome in outcomes),
            catalogue_calls=0,
            catalogue_complete=False,
            catalogue_candidate_count=0,
        )

    if catalogue is None:
        catalogue = fetch_show_catalogue(
            client,
            spotify_show_id(source),
            page_size=page_size,
            max_pages=max_pages,
        )
        catalogue_calls = catalogue.request_count
    else:
        catalogue_calls = 0 if catalogue_request_count is None else catalogue_request_count
    candidates = catalogue.candidates
    catalogue_complete = catalogue.complete
    candidate_uris = catalogue.candidate_uris

    if validate_cached and catalogue.can_prove_absence:
        for index, cached in cached_matched:
            uri = cached.spotify_episode_uri
            if uri is None or uri in candidate_uris:
                continue
            diagnostics = "persisted Spotify episode is absent from the complete show catalogue"
            applied = store.set_match_state(
                cached.edition.source_id,
                cached.edition.source_native_id,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
                updated_at=observed_at,
            )
            if not applied:
                # A complete catalogue positively contradicted the durable mapping, but a newer
                # durable row won the temporal ordering guard. Keep the public result aligned with
                # SQLite and make the rejection explicit so Engine can block writes and back off.
                outcomes[index] = replace(cached, persistence_rejected=True)
                qualification_overrides[index] = False
                continue
            outcomes[index] = MatchOutcome(
                edition=cached.edition,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
            )
            unresolved.append((index, cached.edition))

    for index, edition in unresolved:
        evaluations = tuple(
            _evaluate_candidate(source, edition, candidate) for candidate in candidates
        )
        outcome = _resolve_outcome(
            edition,
            evaluations,
            candidate_count=len(candidates),
            catalogue_calls=catalogue_calls,
            duration_tolerance_seconds=duration_tolerance,
        )
        _notify_fresh_evaluation(
            on_fresh_evaluation,
            outcome,
            index=index,
            notified_indices=notified_fresh_indices,
        )
        applied = store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=outcome.status,
            spotify_episode_uri=outcome.spotify_episode_uri,
            spotify_duration_seconds=outcome.spotify_duration_seconds,
            diagnostics=outcome.diagnostics,
            updated_at=observed_at,
        )
        if not applied:
            durable = _persisted_outcome(store, edition, now=observed_at)
            if durable is None:
                raise PersistenceError(
                    "Spotify match write was rejected by temporal ordering and durable state "
                    "cannot be safely reused"
                )
            outcomes[index] = replace(durable, persistence_rejected=True)
            qualification_overrides[index] = None
            continue
        outcomes[index] = outcome

    if evaluation_intents is not None:
        for index, value in enumerate(outcomes):
            outcome = _require_outcome(value)
            intent = evaluation_intents.get(outcome.edition.identity)
            if (
                not outcome.from_cache
                or outcome.status is not MatchStatus.MATCHED
                or intent
                not in {
                    MatchEvaluationIntent.SEMANTIC_REEVALUATION,
                    MatchEvaluationIntent.EVIDENCE_RECOVERY,
                }
                or outcome.spotify_episode_uri not in candidate_uris
            ):
                continue
            evaluations = tuple(
                _evaluate_candidate(source, outcome.edition, item) for item in candidates
            )
            assessment = _assess_cached_mapping(
                outcome,
                evaluations,
                candidate_count=len(candidates),
                catalogue_calls=catalogue_calls,
                duration_tolerance_seconds=duration_tolerance,
            )
            _notify_fresh_evaluation(
                on_fresh_evaluation,
                assessment.evaluated,
                index=index,
                notified_indices=notified_fresh_indices,
            )
            if assessment.qualification is True:
                qualification_overrides[index] = True
                continue
            if assessment.qualification is None and not (
                intent is MatchEvaluationIntent.EVIDENCE_RECOVERY
                and assessment.recovery_repair_allowed
            ):
                qualification_overrides[index] = None
                continue

            evaluated = assessment.evaluated
            applied = store.set_match_state(
                outcome.edition.source_id,
                outcome.edition.source_native_id,
                status=evaluated.status,
                spotify_episode_uri=evaluated.spotify_episode_uri,
                spotify_duration_seconds=evaluated.spotify_duration_seconds,
                diagnostics=evaluated.diagnostics,
                updated_at=observed_at,
            )
            if not applied:
                # The durable row is newer than this runner observation (for example after a
                # clock rollback). Never publish a fresh repaired outcome that SQLite rejected:
                # planner evidence must remain fail-closed against the mapping still on disk.
                qualification_overrides[index] = assessment.qualification
                outcomes[index] = replace(outcome, persistence_rejected=True)
                continue
            outcomes[index] = evaluated

    # Shadow-only semantic qualification. Reuse the already-fetched candidates and the
    # same pure evaluator; never issue another request. Any conclusive production repair from
    # an explicit recovery intent has already happened above using this same catalogue.
    for index, value in enumerate(outcomes):
        outcome = _require_outcome(value)
        qualified: bool | None = None
        if index in qualification_overrides:
            qualified = qualification_overrides[index]
        elif not outcome.from_cache:
            qualified = True
        elif outcome.status is MatchStatus.MATCHED and candidates:
            if not catalogue.complete and outcome.spotify_episode_uri not in candidate_uris:
                # Absence from a bounded/truncated catalogue is not negative evidence. Preserve
                # any durable qualification already held and let recovery retry on its own cadence.
                qualified = None
            else:
                try:
                    evaluations = tuple(
                        _evaluate_candidate(source, outcome.edition, item) for item in candidates
                    )
                    qualified = _qualify_cached_mapping(
                        outcome,
                        evaluations,
                        candidate_count=len(candidates),
                        catalogue_calls=catalogue_calls,
                        duration_tolerance_seconds=duration_tolerance,
                    )
                except Exception:
                    # Failure of optional shadow evaluation is inconclusive, not contradiction.
                    qualified = None
        outcomes[index] = replace(outcome, planner_input_evidence_qualified=qualified)

    return SourceMatchResult(
        source_id=source.id,
        outcomes=tuple(_require_outcome(outcome) for outcome in outcomes),
        catalogue_calls=catalogue_calls,
        catalogue_complete=catalogue_complete,
        catalogue_candidate_count=len(candidates),
        catalogue_inspected=True,
    )


def _assess_cached_mapping(
    outcome: MatchOutcome,
    evaluations: Sequence[_CandidateEvaluation],
    *,
    candidate_count: int,
    catalogue_calls: int,
    duration_tolerance_seconds: int | None,
) -> _CachedMappingAssessment:
    """Classify one cached mapping once for production repair and planner evidence."""
    evaluated = _resolve_outcome(
        outcome.edition,
        evaluations,
        candidate_count=candidate_count,
        catalogue_calls=catalogue_calls,
        duration_tolerance_seconds=duration_tolerance_seconds,
    )
    cached_uri = outcome.spotify_episode_uri
    if cached_uri is None:
        return _CachedMappingAssessment(evaluated, False, False)

    cached_evaluation = next(
        (evaluation for evaluation in evaluations if evaluation.candidate.uri == cached_uri),
        None,
    )
    if cached_evaluation is None:
        # A bounded catalogue may simply not contain the durable URI. Absence handling is
        # authoritative only in the existing complete/non-empty catalogue path above.
        return _CachedMappingAssessment(evaluated, None, False)
    if not cached_evaluation.matched:
        # Positive semantic contradiction of the durable mapping. The already-paid catalogue may
        # replace it with the best fresh state, including PENDING/AMBIGUOUS when no replacement is
        # uniquely established.
        return _CachedMappingAssessment(evaluated, False, True)
    if evaluated.status is not MatchStatus.MATCHED:
        # The durable URI is still viable, but uniqueness has been lost. Preserve it.
        return _CachedMappingAssessment(evaluated, None, False)
    if evaluated.spotify_episode_uri != cached_uri:
        # Current matching rules uniquely prefer another candidate.
        return _CachedMappingAssessment(evaluated, False, True)
    if evaluated.spotify_duration_seconds == outcome.spotify_duration_seconds:
        return _CachedMappingAssessment(evaluated, True, False)
    if evaluated.spotify_duration_seconds is None:
        # Missing provider metadata is not a conclusive metadata update and must never erase a
        # known durable duration.
        return _CachedMappingAssessment(evaluated, None, False)
    # Same uniquely-valid URI with a newly known duration. Only explicit evidence recovery may
    # refresh the durable metadata in-place; ordinary semantic reevaluation remains inconclusive.
    return _CachedMappingAssessment(evaluated, None, True)


def _qualify_cached_mapping(
    outcome: MatchOutcome,
    evaluations: Sequence[_CandidateEvaluation],
    *,
    candidate_count: int,
    catalogue_calls: int,
    duration_tolerance_seconds: int | None,
) -> bool | None:
    """Return planner qualification from the shared cached-mapping assessment."""
    return _assess_cached_mapping(
        outcome,
        evaluations,
        candidate_count=candidate_count,
        catalogue_calls=catalogue_calls,
        duration_tolerance_seconds=duration_tolerance_seconds,
    ).qualification


def _notify_fresh_evaluation(
    callback: Callable[[MatchOutcome], None] | None,
    outcome: MatchOutcome,
    *,
    index: int,
    notified_indices: set[int],
) -> None:
    if index in notified_indices:
        return
    notified_indices.add(index)
    if callback is None:
        return
    # Observability must never change matching or persistence semantics.
    with suppress(Exception):
        callback(outcome)


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
        if not is_spotify_episode_uri(state.spotify_episode_uri):
            diagnostics = "persisted Spotify mapping contained an invalid episode URI"
            applied = store.set_match_state(
                edition.source_id,
                edition.source_native_id,
                status=MatchStatus.PENDING,
                spotify_episode_uri=None,
                spotify_duration_seconds=None,
                diagnostics=diagnostics,
                updated_at=now,
            )
            if not applied:
                raise PersistenceError(
                    "invalid persisted Spotify mapping could not be durably invalidated"
                )
            return None
        # Schema-v2 matches predate durable destination duration. Refresh those
        # mappings once instead of treating unknown duration as eligibility proof.
        if state.spotify_duration_seconds is None:
            return None
        return MatchOutcome(
            edition=edition,
            status=state.status,
            spotify_episode_uri=state.spotify_episode_uri,
            diagnostics=state.diagnostics or "reused persisted Spotify mapping",
            spotify_duration_seconds=state.spotify_duration_seconds,
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


def _persisted_outcome(
    store: SQLiteStore,
    edition: CanonicalEdition,
    *,
    now: datetime,
) -> MatchOutcome | None:
    """Return durable state without applying the legacy fixed retry grace."""
    state = store.get_match_state(edition.source_id, edition.source_native_id)
    if state is None:
        return None
    if state.status is MatchStatus.MATCHED:
        if (
            state.spotify_episode_uri is None
            or not is_spotify_episode_uri(state.spotify_episode_uri)
            or state.spotify_duration_seconds is None
        ):
            return None
        return MatchOutcome(
            edition=edition,
            status=state.status,
            spotify_episode_uri=state.spotify_episode_uri,
            spotify_duration_seconds=state.spotify_duration_seconds,
            diagnostics=state.diagnostics or "reused persisted Spotify mapping",
            from_cache=True,
        )
    if state.status in {MatchStatus.PENDING, MatchStatus.AMBIGUOUS}:
        return MatchOutcome(
            edition=edition,
            status=state.status,
            spotify_episode_uri=None,
            diagnostics=state.diagnostics or "reused persisted unresolved state",
            from_cache=True,
        )
    return None


def spotify_show_id(source: SourceDefinition) -> str:
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


def fetch_show_catalogue(
    client: SpotifyCatalogClient,
    show_id: str,
    *,
    page_size: int,
    max_pages: int,
    required_uris: Collection[str] | None = None,
) -> SpotifyCatalogueResult:
    by_uri: dict[str, _SpotifyEpisodeCandidate] = {}
    calls = 0
    complete = False
    expected_total: int | None = None
    required = None if required_uris is None else frozenset(required_uris)
    if required is not None and not required:
        raise ValueError("required_uris must not be empty")
    for page_number in range(max_pages):
        requested_offset = page_number * page_size
        page = client.show_episodes(
            show_id,
            limit=page_size,
            offset=requested_offset,
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
        if next_page is not None and (not isinstance(next_page, str) or not next_page.strip()):
            raise MatchResponseError("Spotify show episodes response contained invalid next")
        response_limit = page.get("limit")
        response_offset = page.get("offset")
        response_total = page.get("total")
        if isinstance(response_limit, bool) or not isinstance(response_limit, int):
            raise MatchResponseError("Spotify show episodes response contained invalid limit")
        if response_limit != page_size:
            raise MatchResponseError("Spotify show episodes response limit did not match request")
        if (
            isinstance(response_offset, bool)
            or not isinstance(response_offset, int)
            or response_offset < 0
            or response_offset != requested_offset
        ):
            raise MatchResponseError("Spotify show episodes response contained invalid offset")
        if (
            isinstance(response_total, bool)
            or not isinstance(response_total, int)
            or response_total < 0
        ):
            raise MatchResponseError("Spotify show episodes response contained invalid total")
        if expected_total is None:
            expected_total = response_total
        elif response_total != expected_total:
            raise MatchResponseError("Spotify show episodes response total changed between pages")
        if len(items) > response_limit or response_offset + len(items) > response_total:
            raise MatchResponseError("Spotify show episodes response contained impossible paging")
        has_more = response_offset + len(items) < response_total
        if (next_page is not None) != has_more:
            raise MatchResponseError("Spotify show episodes response paging contradicted next")

        for item in items:
            candidate = _candidate_from_item(item)
            existing = by_uri.get(candidate.uri)
            if existing is None:
                by_uri[candidate.uri] = candidate
            elif existing != candidate:
                raise MatchResponseError(
                    "Spotify catalogue repeated an episode URI with conflicting metadata"
                )

        if required is not None and required.issubset(by_uri):
            if next_page is None:
                complete = True
            break
        if next_page is None:
            complete = True
            break
    return SpotifyCatalogueResult(tuple(by_uri.values()), calls, complete)


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
        raise MatchResponseError("Spotify episode item contained invalid release_date_precision")
    _validate_release_date(release_date, precision)

    uri = item.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise MatchResponseError("Spotify episode item contained invalid uri")
    uri = uri.strip()
    if not uri.startswith("spotify:episode:"):
        raise MatchResponseError("Spotify episode item contained a non-episode uri")
    if not is_spotify_episode_uri(uri):
        raise MatchResponseError("Spotify episode item contained an invalid episode uri")

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
        # A release-date/title strategy identifies a specific daily edition. Month/year
        # precision cannot establish that day even when the recurring title is identical.
        if candidate.release_date_precision != "day":
            return _CandidateEvaluation(
                candidate,
                False,
                "release_date_precision_insufficient",
            )
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
        parsed.edition_at.replace(tzinfo=None).replace(tzinfo=source.timezone).astimezone(UTC)
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
    duration_tolerance_seconds: int | None,
) -> MatchOutcome:
    viable = tuple(evaluation for evaluation in evaluations if evaluation.matched)
    original_viable_count = len(viable)
    duration_disambiguated = False

    if len(viable) > 1:
        duration_choice = _duration_disambiguation_choice(
            edition,
            viable,
            duration_tolerance_seconds=duration_tolerance_seconds,
        )
        if duration_choice is not None:
            viable = (duration_choice,)
            duration_disambiguated = True

    if len(viable) == 1:
        evaluation = viable[0]
        candidate = evaluation.candidate
        duration_detail = _duration_diagnostic(
            edition,
            candidate,
            authoritative=duration_disambiguated,
        )
        release_detail = _release_delay_diagnostic(evaluation)
        disambiguation_detail = (
            f"; duration disambiguated 1 of {original_viable_count} viable candidate(s) "
            f"within ±{duration_tolerance_seconds}s"
            if duration_disambiguated and duration_tolerance_seconds is not None
            else ""
        )
        return MatchOutcome(
            edition=edition,
            status=MatchStatus.MATCHED,
            spotify_episode_uri=candidate.uri,
            spotify_duration_seconds=candidate.duration_seconds,
            diagnostics=(
                f"matched 1 of {candidate_count} candidate(s) in {catalogue_calls} call(s)"
                f"{release_detail}{disambiguation_detail}{duration_detail}"
            ),
        )
    if len(viable) > 1:
        uris = ", ".join(evaluation.candidate.uri for evaluation in viable[:3])
        suffix = ", ..." if len(viable) > 3 else ""
        return MatchOutcome(
            edition=edition,
            status=MatchStatus.AMBIGUOUS,
            spotify_episode_uri=None,
            diagnostics=(f"{len(viable)} viable Spotify candidates in known show: {uris}{suffix}"),
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


def _duration_disambiguation_choice(
    edition: CanonicalEdition,
    viable: Sequence[_CandidateEvaluation],
    *,
    duration_tolerance_seconds: int | None,
) -> _CandidateEvaluation | None:
    if duration_tolerance_seconds is None or edition.duration_seconds is None:
        return None
    if any(evaluation.candidate.duration_seconds is None for evaluation in viable):
        return None
    within_tolerance = tuple(
        evaluation
        for evaluation in viable
        if evaluation.candidate.duration_seconds is not None
        and abs(edition.duration_seconds - evaluation.candidate.duration_seconds)
        <= duration_tolerance_seconds
    )
    if len(within_tolerance) != 1:
        return None
    return within_tolerance[0]


def _release_delay_diagnostic(evaluation: _CandidateEvaluation) -> str:
    if evaluation.reason != "delayed_release_matched":
        return ""
    return (
        f"; delayed Spotify release=+{evaluation.release_delay_days}d accepted by "
        "exact semantic edition time"
    )


def _rejection_diagnostic(evaluations: Sequence[_CandidateEvaluation]) -> str:
    counts = Counter(evaluation.reason for evaluation in evaluations if not evaluation.matched)
    parts = [f"{reason}={counts[reason]}" for reason in _REJECTION_REASON_ORDER if counts[reason]]
    extras = sorted(reason for reason in counts if reason not in _REJECTION_REASON_ORDER)
    parts.extend(f"{reason}={counts[reason]}" for reason in extras)
    return f"rejections: {', '.join(parts)}" if parts else ""


def _duration_diagnostic(
    edition: CanonicalEdition,
    candidate: _SpotifyEpisodeCandidate,
    *,
    authoritative: bool = False,
) -> str:
    if edition.duration_seconds is None or candidate.duration_seconds is None:
        return ""
    delta = abs(edition.duration_seconds - candidate.duration_seconds)
    qualifier = "" if authoritative else " (diagnostic only)"
    return f"; duration delta={delta}s{qualifier}"


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
