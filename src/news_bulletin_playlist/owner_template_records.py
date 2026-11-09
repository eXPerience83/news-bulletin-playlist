"""Exact, versioned owner template records for historical v11 materialization.

Pure data only. The original image's default playlist IDs, source ordering,
retention, duration exceptions (including dormant selectors), edit limits and
artwork identities are preserved; dynamic code and provider state are excluded.

A later v11 authority migration must validate cover assets and publish sources,
templates, source order and managed destinations together under the v11 lock.
This codec does not by itself enable live edits or Spotify writes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from news_bulletin_playlist.catalog import PlaylistTemplate
from news_bulletin_playlist.models import (
    CountryCode,
    DurationPolicy,
    LanguageTag,
    OrderingPolicy,
    PlaylistId,
    SourceId,
)
from news_bulletin_playlist.portable_playlist_metadata import (
    exact_object,
    exception_payload,
    parse_exception,
    strict_json,
)

_OWNER_FORMAT = "news-bulletin-playlist.owner-templates"
_OWNER_VERSION = 1
_MAX_BYTES = 2 * 1024 * 1024
_MAX_TEMPLATES = 256
_MAX_TEXT = 65_536
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-(?:[A-Z]{2}|[0-9]{3}))?\Z")


class OwnerTemplateError(ValueError):
    """Historical playlist owner data is ambiguous, unsupported or incomplete."""


def _identity(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise OwnerTemplateError("Invalid owned template or source identity")
    return value


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_TEXT
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise OwnerTemplateError("Invalid owned template text")
    return value


def _positive(value: object, *, zero: bool = False) -> int:
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**63 - 1:
        raise OwnerTemplateError("Invalid owned template policy integer")
    return value


def _tags(value: object, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 100
        or any(not isinstance(item, str) or pattern.fullmatch(item) is None for item in value)
        or len(value) != len(set(value))
    ):
        raise OwnerTemplateError("Invalid owned template language/country classification")
    return tuple(value)


def _template_record(template: PlaylistTemplate) -> dict[str, object]:
    policy = template.duration_policy
    return {
        "id": _identity(template.id),
        "display_name": _text(template.display_name),
        "description": _text(template.description),
        "countries": list(template.countries),
        "languages": list(template.languages),
        "source_ids": [_identity(source) for source in template.default_source_ids],
        "cover_id": _identity(template.cover_id),
        "retention_hours": _positive(template.retention_hours),
        "max_episodes": _positive(template.max_episodes),
        "ordering": template.ordering.value,
        "duration_policy": {
            "min_seconds": _positive(policy.default_min_seconds, zero=True),
            "max_seconds": _positive(policy.default_max_seconds),
            "exceptions": [exception_payload(item) for item in policy.exceptions],
        },
    }


def _decode_template(value: object) -> PlaylistTemplate:
    fields = exact_object(
        value,
        {
            "id",
            "display_name",
            "description",
            "countries",
            "languages",
            "source_ids",
            "cover_id",
            "retention_hours",
            "max_episodes",
            "ordering",
            "duration_policy",
        },
    )
    if fields["ordering"] != OrderingPolicy.EDITION_AT_DESC.value:
        raise OwnerTemplateError("Unsupported owner template ordering policy")
    ids = fields["source_ids"]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 100
        or any(not isinstance(item, str) for item in ids)
        or len(ids) != len(set(ids))
    ):
        raise OwnerTemplateError("Invalid owned template source assignments")
    policy = exact_object(
        fields["duration_policy"], {"min_seconds", "max_seconds", "exceptions"}
    )
    exceptions = policy["exceptions"]
    if not isinstance(exceptions, list) or len(exceptions) > 100:
        raise OwnerTemplateError("Invalid owned template duration exceptions")
    return PlaylistTemplate(
        id=PlaylistId(_identity(fields["id"])),
        display_name=_text(fields["display_name"]),
        description=_text(fields["description"]),
        countries=tuple(CountryCode(x) for x in _tags(fields["countries"], _COUNTRY)),
        languages=tuple(LanguageTag(x) for x in _tags(fields["languages"], _LANGUAGE)),
        default_source_ids=tuple(SourceId(_identity(item)) for item in ids),
        cover_id=_identity(fields["cover_id"]),
        retention_hours=_positive(fields["retention_hours"]),
        max_episodes=_positive(fields["max_episodes"]),
        ordering=OrderingPolicy.EDITION_AT_DESC,
        duration_policy=DurationPolicy(
            default_min_seconds=_positive(policy["min_seconds"], zero=True),
            default_max_seconds=_positive(policy["max_seconds"]),
            exceptions=tuple(parse_exception(item) for item in exceptions),
        ),
    )


@dataclass(frozen=True, slots=True)
class OwnerTemplateSet:
    """A canonical persisted owner template inventory, not immutable image presets."""

    records: tuple[PlaylistTemplate, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.records, tuple)
            or len(self.records) > _MAX_TEMPLATES
            or any(not isinstance(item, PlaylistTemplate) for item in self.records)
        ):
            raise OwnerTemplateError("Invalid owner template inventory")
        identifiers = [item.id for item in self.records]
        if len(identifiers) != len(set(identifiers)):
            raise OwnerTemplateError("Duplicate owner template ID")
        for item in self.records:
            _template_record(item)

    def validate_sources(self, source_ids: tuple[SourceId, ...]) -> None:
        """Inspect every definition and exception; do not drop dormant references."""
        all_ids = set(source_ids)
        if len(all_ids) != len(source_ids):
            raise OwnerTemplateError("Duplicate owner source in template closure")
        for item in self.records:
            if not item.default_source_ids or any(
                source not in all_ids for source in item.default_source_ids
            ):
                raise OwnerTemplateError("Owned template references a missing source")
            if any(
                exception.source_id not in all_ids
                for exception in item.duration_policy.exceptions
            ):
                raise OwnerTemplateError("Owned template exception references a missing source")
            # Strict roundtrip validates timezone fold, microseconds, integer
            # bounds, identifiers and supported DurationPolicy contracts.
            if _decode_template(_template_record(item)) != item:
                raise OwnerTemplateError("Owner template cannot roundtrip without loss")

    def serialize(self) -> bytes:
        document = {
            "format": _OWNER_FORMAT,
            "version": _OWNER_VERSION,
            "templates": [_template_record(item) for item in self.records],
        }
        payload = (
            json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(payload) > _MAX_BYTES:
            raise OwnerTemplateError("Owner template inventory exceeds size limit")
        return payload

    @classmethod
    def parse(cls, payload: bytes) -> OwnerTemplateSet:
        if not isinstance(payload, bytes) or not 0 < len(payload) <= _MAX_BYTES:
            raise OwnerTemplateError("Invalid owner template document size")
        try:
            data = exact_object(strict_json(payload), {"format", "version", "templates"})
            records = data["templates"]
            if (
                data["format"] != _OWNER_FORMAT
                or type(data["version"]) is not int
                or data["version"] != _OWNER_VERSION
                or not isinstance(records, list)
                or len(records) > _MAX_TEMPLATES
            ):
                raise OwnerTemplateError("Unsupported owner template document")
            candidate = cls(tuple(_decode_template(item) for item in records))
            if candidate.serialize() != payload:
                raise OwnerTemplateError("Noncanonical owner template document")
            return candidate
        except (UnicodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise OwnerTemplateError("Invalid or noncanonical owner template inventory") from exc

    def replace_one(
        self,
        old_id: PlaylistId,
        edited: PlaylistTemplate,
        *,
        sources: tuple[SourceId, ...],
    ) -> OwnerTemplateSet:
        """Pure owner metadata/policy edit; original ID cannot be changed implicitly."""
        if edited.id != old_id or old_id not in {item.id for item in self.records}:
            raise OwnerTemplateError("Cannot change an owned template ID via edit")
        updated = OwnerTemplateSet(
            tuple(edited if item.id == old_id else item for item in self.records)
        )
        updated.validate_sources(sources)
        return updated

    def remove_unreferenced(
        self,
        template_id: PlaylistId,
        *,
        managed_template_ids: tuple[PlaylistId, ...],
        pending_template_ids: tuple[PlaylistId, ...] = (),
    ) -> OwnerTemplateSet:
        """No deletion of template still bound to a destination or provider intent."""
        if template_id in managed_template_ids or template_id in pending_template_ids:
            raise OwnerTemplateError("Unassign or resolve referenced template before deletion")
        if template_id not in {item.id for item in self.records}:
            raise OwnerTemplateError("Owner template not found")
        return OwnerTemplateSet(tuple(item for item in self.records if item.id != template_id))


def materialize_owner_templates(
    templates: tuple[PlaylistTemplate, ...],
    *,
    sources: tuple[SourceId, ...],
) -> OwnerTemplateSet:
    """Build canonical, editable owner records without any provider or image I/O."""
    catalog = OwnerTemplateSet(templates)
    catalog.validate_sources(sources)
    try:
        if OwnerTemplateSet.parse(catalog.serialize()) != catalog:
            raise OwnerTemplateError("Historical template representation changed")
    except (ValueError, TypeError) as exc:
        raise OwnerTemplateError("Historical templates cannot be owned losslessly") from exc
    return catalog
