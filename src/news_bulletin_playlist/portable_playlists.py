"""Read-only, self-contained playlist configuration export. Not a backup or importer."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Protocol
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from news_bulletin_playlist.custom_cover_assets import (
    MAX_JPEG_BYTES,
    CustomCoverAssetError,
    CustomCoverAssetStore,
    is_uploaded_cover_id,
    validate_canonical_jpeg,
)
from news_bulletin_playlist.custom_playlist_templates import (
    LocalPlaylistTemplate,
    effective_playlist_templates,
)
from news_bulletin_playlist.managed_state import (
    PORTABLE_PLAYLIST_SCHEMA_VERSION,
    ManagedPlaylist,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
    allocate_portable_id,
    effective_sources,
    same_managed_configuration,
    serialize_managed_state,
    template_suspended_by_quarantine,
)
from news_bulletin_playlist.portable_identity import portable_id
from news_bulletin_playlist.portable_sources import export_sources

MAX_MANIFEST_BYTES = 256 * 1024
MAX_PLAYLIST_ZIP_BYTES = 1024 * 1024
_BUNDLED = frozenset(
    {
        "spain_spanish_news",
        "international_english_news",
        "international_spanish_news",
        "international_spanish_long_news",
        "international_french_news",
        "international_german_news",
        "international_polish_news",
        "custom_default",
    }
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class PlaylistExportError(ValueError):
    """Safe operator-facing configuration error."""


class Synchronization(Protocol):
    def hold(self) -> AbstractContextManager[None]: ...


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _descriptor(data: bytes) -> dict[str, object]:
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _identifier(value: str) -> str:
    if _ID.fullmatch(value) is None:
        raise PlaylistExportError("A logical identifier cannot be represented portably")
    return value


def _number(value: int, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise PlaylistExportError("A policy value exceeds the portable format limits")
    return value


def _text(value: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise PlaylistExportError("Playlist text exceeds the portable format limits")
    return value


def _tags(values: tuple[str, ...], pattern: str) -> list[str]:
    if (
        len(values) > 100
        or len(set(values)) != len(values)
        or any(re.fullmatch(pattern, value) is None for value in values)
    ):
        raise PlaylistExportError("Playlist regions or languages cannot be represented portably")
    return list(values)


class PortableCoverLoader:
    """Read allowlisted local JPEGs without recompression, path traversal or network I/O."""

    def __init__(self, data_dir: Path, *, bundled_roots: tuple[Path, ...] | None = None) -> None:
        self.store = CustomCoverAssetStore(data_dir / "custom-covers")
        self.roots = (
            bundled_roots
            if bundled_roots is not None
            else (
                Path("/opt/news-bulletin-playlist/covers"),
                Path(__file__).resolve().parents[2] / "assets/covers/spotify",
            )
        )

    def load(self, cover_id: str) -> tuple[bytes, dict[str, object]]:
        uploaded = is_uploaded_cover_id(cover_id)
        if uploaded:
            data = self.store.load(cover_id)
        elif cover_id in _BUNDLED:
            data = self._bundled(cover_id)
        else:
            raise PlaylistExportError("Selected cover is not a supported local asset")
        try:
            validate_canonical_jpeg(data)
        except CustomCoverAssetError:
            raise PlaylistExportError("Selected cover cannot be decoded safely") from None
        curated = not uploaded and cover_id != "custom_default"
        return data, {
            "credit": None,
            "license": None,
            "provenance": "Owner-confirmed ChatGPT-generated SVG master; bundled JPEG derivative"
            if curated
            else (
                "User-uploaded artwork; rights not recorded"
                if uploaded
                else "Bundled default artwork; rights not recorded"
            ),
            "reference": "https://github.com/eXPerience83/news-bulletin-playlist/blob/main/docs/PLAYLIST_COVERS.md"
            if curated
            else None,
        }

    def _bundled(self, cover_id: str) -> bytes:
        for root in self.roots:
            try:
                directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            try:
                try:
                    fd = os.open(
                        f"{cover_id}.jpg",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                except FileNotFoundError:
                    continue
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise PlaylistExportError("Selected cover is not a regular file")
                    with os.fdopen(fd, "rb", closefd=False) as source:
                        return source.read(MAX_JPEG_BYTES + 1)
                finally:
                    os.close(fd)
            finally:
                os.close(directory)
        raise PlaylistExportError("Selected bundled cover is missing")


def playlist_bundle(
    state: ManagedState,
    store: ManagedStateStore,
    kind: str,
    object_id: str,
    covers: PortableCoverLoader,
) -> tuple[str, bytes]:
    """Build everything before returning bytes; caller holds the configuration lock."""
    target = _target(state, store, kind, object_id)
    identity = portable_id(target.portable_id)
    if identity is None:
        raise PlaylistExportError("Prepare a portable identity before downloading this playlist")
    templates = effective_playlist_templates(
        store.catalog, state.templates, sources=effective_sources(store.catalog, state)
    )
    if isinstance(target, ManagedPlaylist):
        template = next((item for item in templates if item.id == target.template_id), None)
        if template is None:
            raise PlaylistExportError("The playlist template is unavailable")
        source_ids = target.source_ids
        exceptions = tuple(
            e
            for e in template.duration_policy.exceptions
            if e.source_id in source_ids and e.max_seconds > target.max_duration_seconds
        )
        countries, languages, ordering = template.countries, template.languages, template.ordering
        configuration = {"enabled": target.enabled, "hidden": None}
    else:
        source_ids = target.default_source_ids
        exceptions = target.duration_exceptions
        countries, languages, ordering = target.countries, target.languages, target.ordering
        configuration = {"enabled": None, "hidden": target.hidden}
    if not source_ids or len(source_ids) > 100 or len(set(source_ids)) != len(source_ids):
        raise PlaylistExportError("Playlist assignments exceed portable source limits")
    sources = {source.id: source for source in effective_sources(store.catalog, state)}
    if any(source_id not in sources for source_id in source_ids):
        raise PlaylistExportError("An assigned source is unavailable")
    source_bytes = export_sources(
        tuple(sources[source_id] for source_id in source_ids),
        built_in_ids={source.id for source in store.catalog.sources},
    )
    cover_bytes, attribution = covers.load(target.cover_id)
    if not isinstance(target, ManagedPlaylist) and target.portable_metadata is not None:
        metadata = json.loads(target.portable_metadata)
        if metadata["cover_sha256"] == hashlib.sha256(cover_bytes).hexdigest():
            attribution = metadata["attribution"]
    cover_descriptor = _descriptor(cover_bytes)
    cover_path = f"covers/{cover_descriptor['sha256']}.jpg"
    if len(exceptions) > 100:
        raise PlaylistExportError("Too many duration exceptions")
    minimum, maximum = (
        _number(target.min_duration_seconds, minimum=0),
        _number(target.max_duration_seconds),
    )
    if minimum > maximum:
        raise PlaylistExportError("Invalid playlist duration range")
    manifest = {
        "format": "news-bulletin-playlist.playlist",
        "version": 1,
        "playlist": {
            "portable_id": identity,
            "original_id": _identifier(str(target.id)),
            "kind": kind,
            "display_name": _text(target.display_name, 4096),
            "description": _text(target.description, 65536),
            "configuration": configuration,
            "countries": _tags(countries, r"[A-Z]{2}"),
            "languages": _tags(languages, r"[a-z]{2,3}(?:-(?:[A-Z]{2}|[0-9]{3}))?"),
            "source_ids": [_identifier(str(item)) for item in source_ids],
            "ordering": ordering.value,
            "retention_hours": _number(target.retention_hours),
            "max_episodes": _number(target.max_episodes),
            "duration_policy": {
                "min_seconds": minimum,
                "max_seconds": maximum,
                "exceptions": [
                    {
                        "id": _identifier(e.id),
                        "source_id": str(e.source_id),
                        "edition_local_time": e.edition_local_time.isoformat(
                            timespec="microseconds"
                        ),
                        "edition_fold": e.edition_local_time.fold,
                        "max_seconds": _number(e.max_seconds),
                    }
                    for e in exceptions
                ],
            },
            "cover": {
                "original_id": target.cover_id,
                "origin": "uploaded" if is_uploaded_cover_id(target.cover_id) else "bundled",
                "path": cover_path,
                "media_type": "image/jpeg",
                **cover_descriptor,
                "attribution": attribution,
            },
        },
        "files": {"sources.json": _descriptor(source_bytes), cover_path: cover_descriptor},
    }
    manifest_bytes = canonical_json(manifest)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise PlaylistExportError("Playlist manifest exceeds the portable size limit")
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=False) as archive:
        for path, data in (
            ("manifest.json", manifest_bytes),
            (
                "manifest.sha256",
                (hashlib.sha256(manifest_bytes).hexdigest() + "\n").encode("ascii"),
            ),
            ("sources.json", source_bytes),
            (cover_path, cover_bytes),
        ):
            entry = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, data)
    payload = output.getvalue()
    if len(payload) > MAX_PLAYLIST_ZIP_BYTES:
        raise PlaylistExportError("Playlist ZIP exceeds the portable size limit")
    return identity, payload


def _target(
    state: ManagedState, store: ManagedStateStore, kind: str, object_id: str
) -> ManagedPlaylist | LocalPlaylistTemplate:
    _identifier(object_id)
    if kind not in {"managed", "draft"}:
        raise PlaylistExportError("Invalid playlist selection")
    objects: tuple[ManagedPlaylist | LocalPlaylistTemplate, ...] = (
        state.playlists if kind == "managed" else state.templates
    )
    target = next((item for item in objects if item.id == object_id), None)
    if target is None:
        raise PlaylistExportError("Playlist not found")
    if isinstance(target, ManagedPlaylist) and template_suspended_by_quarantine(
        store.catalog, state, target.template_id
    ):
        raise PlaylistExportError("Repair the quarantined template before exporting")
    return target


class PlaylistExportService:
    def __init__(
        self,
        store: ManagedStateStore,
        synchronization: Synchronization,
        covers: PortableCoverLoader,
    ) -> None:
        self.store, self.synchronization, self.covers = store, synchronization, covers

    def export(self, kind: str, object_id: str) -> tuple[str, bytes]:
        with self.synchronization.hold():
            return playlist_bundle(
                self.store.load_readonly(), self.store, kind, object_id, self.covers
            )

    def preparation(self, kind: str, object_id: str) -> tuple[str, int, str | None]:
        with self.synchronization.hold():
            state = self.store.load_readonly()
            target = _target(state, self.store, kind, object_id)
            return (
                self._fingerprint(state, kind, object_id),
                state.schema_version,
                target.portable_id,
            )

    def _fingerprint(self, state: ManagedState, kind: str, object_id: str) -> str:
        return hashlib.sha256(
            repr((state, self.store.catalog, kind, object_id)).encode("utf-8")
        ).hexdigest()

    def prepare(self, kind: str, object_id: str, fingerprint: str, *, acknowledge_v9: bool) -> str:
        with self.synchronization.hold():
            state = self.store.load_readonly()
            target = _target(state, self.store, kind, object_id)
            if self._fingerprint(state, kind, object_id) != fingerprint:
                raise PlaylistExportError("Configuration changed; open a new identity confirmation")
            if target.portable_id is not None:
                return target.portable_id
            if state.schema_version < PORTABLE_PLAYLIST_SCHEMA_VERSION and not acknowledge_v9:
                raise PlaylistExportError(
                    "Explicit schema v9 compatibility acknowledgement is required"
                )
            identity = allocate_portable_id(state)
            if isinstance(target, ManagedPlaylist):
                prepared = replace(target, portable_id=identity)
                candidate = replace(
                    state,
                    schema_version=max(state.schema_version, PORTABLE_PLAYLIST_SCHEMA_VERSION),
                    playlists=tuple(
                        prepared if item.id == object_id else item for item in state.playlists
                    ),
                )
            else:
                draft = replace(target, portable_id=identity)
                candidate = replace(
                    state,
                    schema_version=max(state.schema_version, PORTABLE_PLAYLIST_SCHEMA_VERSION),
                    templates=tuple(
                        draft if item.id == object_id else item for item in state.templates
                    ),
                )
            serialize_managed_state(candidate, catalog=self.store.catalog)
            try:
                self.store.save(candidate)
            except OSError, ManagedStateError:
                try:
                    written = self.store.load_readonly()
                except OSError, ManagedStateError:
                    raise PlaylistExportError(
                        "Identity save outcome is uncertain; inspect state before retrying"
                    ) from None
                if same_managed_configuration(written, candidate, catalog=self.store.catalog):
                    return identity
                if same_managed_configuration(written, state, catalog=self.store.catalog):
                    raise PlaylistExportError(
                        "Identity was not saved; previous configuration is unchanged"
                    ) from None
                raise PlaylistExportError(
                    "Identity save outcome is uncertain; inspect state before retrying"
                ) from None
            return identity
