"""Reusable offline portable-playlist export/import fixtures and ZIP validators."""

from __future__ import annotations

import hashlib
import io
import json
import stat
from dataclasses import replace
from zipfile import ZipFile, ZipInfo

from _managed_fixtures import _state

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.portable_playlists import (
    PlaylistExportService,
    PortableCoverLoader,
    canonical_json,
)

UUID = "b96ab2b3-6568-459c-a391-3a993280e174"
OTHER_UUID = "abb9730d-d6eb-4235-b9b4-1ea1c127d67c"


def _service(tmp_path, state=None, *, catalog=BUILTIN_CATALOG):
    store = ManagedStateStore(tmp_path / "managed-state.json", catalog=catalog)
    if state is not None:
        store.save(state)
    return PlaylistExportService(
        store, ConfigurationSynchronization(), PortableCoverLoader(tmp_path)
    )


def _ready(state=None):
    state = state or _state()
    return replace(
        state,
        schema_version=9,
        playlists=tuple(replace(p, portable_id=UUID) for p in state.playlists),
    )


def _unpack(payload):
    with ZipFile(io.BytesIO(payload)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
        assert archive.namelist()[:3] == ["manifest.json", "manifest.sha256", "sources.json"]
        assert len(files) == 4
        assert all(
            i.date_time == (1980, 1, 1, 0, 0, 0)
            and not i.extra
            and not i.comment
            and i.compress_type == 0
            for i in archive.infolist()
        )
    manifest = json.loads(files["manifest.json"])
    assert (
        files["manifest.sha256"]
        == (hashlib.sha256(files["manifest.json"]).hexdigest() + "\n").encode()
    )
    for path, expected in manifest["files"].items():
        assert expected == {
            "size": len(files[path]),
            "sha256": hashlib.sha256(files[path]).hexdigest(),
        }
    return manifest["playlist"], files


def donor(
    tmp_path, *, template=None, playlist=None, draft=None, catalog=BUILTIN_CATALOG, state=None
):
    root = tmp_path / "donor"
    root.mkdir(exist_ok=True)
    if state is None:
        if draft is not None:
            state = ManagedState(schema_version=9, templates=(draft,))
        else:
            template = template or catalog.playlists[0]
            playlist = playlist or replace(
                activate_template(template, "PRIVATE-DESTINATION"), portable_id=UUID
            )
            state = ManagedState(schema_version=9, playlists=(playlist,))
    service = _service(root, state, catalog=catalog)
    kind = "draft" if draft is not None else "managed"
    id_ = str(draft.id if draft is not None else state.playlists[0].id)
    return service.export(kind, id_)[1]


def rewrite(document, edit=None, *, names=None, compression=0, duplicate=False, info_edit=None):
    with ZipFile(io.BytesIO(document)) as archive:
        entries = [(i, archive.read(i)) for i in archive.infolist()]
    data = {i.filename: b for i, b in entries}
    if edit:
        manifest = json.loads(data["manifest.json"])
        edit(manifest, data)
        data["manifest.json"] = canonical_json(manifest)
        data["manifest.sha256"] = (
            hashlib.sha256(data["manifest.json"]).hexdigest() + "\n"
        ).encode()
    output = io.BytesIO()
    with ZipFile(output, "w", compression=compression) as archive:
        for index, (old, _) in enumerate(entries):
            info = ZipInfo((names or {}).get(index, old.filename), date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = compression
            if info_edit:
                info_edit(index, info)
            archive.writestr(info, data[old.filename])
        if duplicate:
            archive.writestr(entries[0][0], data[entries[0][0].filename])
    return output.getvalue()
